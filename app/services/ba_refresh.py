"""Stage 22 T6: refresh Bundesagentur vacancies (plan + apply), no outbound action.

`fetch_postings` runs the configured BA searches through the existing collector;
`plan_refresh` compares the result with the stored vacancies (read-only);
`apply_updates` / `create_new` write. Rules:

* An existing vacancy (same fingerprint) only gets the observation's structured
  facts and a still empty `source_reference` (`refresh_job_structured_facts`).
  No score, recommendation, status, description or `last_seen_at` changes.
* A posting whose BA reference already belongs to a stored row with another
  fingerprint is NEVER created or merged: it is reported as a conflict.
* A stored vacancy missing from today's results is only counted. A search
  result is a window (pagination, query terms), never proof that a posting is
  closed; nothing is marked, hidden or deleted because of it.
* Creating new vacancies reuses `enrich_and_persist_ba_job` (the collector run's
  own per-job path) and is opt-in.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.ba_reference import ba_reference_from_url, storable_reference
from app.collectors.bundesagentur import SOURCE_NAME, BundesagenturCollector
from app.db.models import ApplicationRecord, JobRecord
from app.db.repositories import (
    STRUCTURED_JOB_FACTS,
    _fingerprint,
    refresh_job_structured_facts,
)
from app.models.job import Job
from app.services.search_strategy import BundesagenturQuery

logger = logging.getLogger(__name__)

MAX_REPORTED = 50
CollectorFactory = Callable[[BundesagenturQuery], BundesagenturCollector]

CONFLICT_REFERENCE_OTHER_FINGERPRINT = "REFERENCE_EXISTS_WITH_OTHER_FINGERPRINT"
CONFLICT_STORED_REFERENCE_DIFFERS = "STORED_REFERENCE_DIFFERS"
CONFLICT_DUPLICATE_IN_RESULTS = "SAME_REFERENCE_TWICE_IN_RESULTS"


@dataclass
class QueryReport:
    label: str
    keywords: str
    fetched: int = 0
    skipped_invalid: int = 0
    skipped_not_home_office: int = 0
    truncated: bool = False
    total_results: int | None = None
    left_out: int | None = None  # results beyond max_pages (None = unknown)
    error: str | None = None


@dataclass
class FetchResult:
    postings: list[Job] = field(default_factory=list)  # one per posting
    queries: list[QueryReport] = field(default_factory=list)

    @property
    def failed_queries(self) -> int:
        return sum(1 for query in self.queries if query.error)


@dataclass
class Conflict:
    kind: str
    reference: str | None
    title: str
    company: str
    existing_job_ids: list[int]


@dataclass
class PlannedUpdate:
    job_id: int
    job: Job
    added: list[str]  # column was empty
    changed: list[str]  # column held another value


@dataclass
class RefreshPlan:
    fetched: int = 0
    existing: int = 0
    unchanged: int = 0
    updates: list[PlannedUpdate] = field(default_factory=list)
    creates: list[Job] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    not_returned: int = 0
    not_returned_with_application: int = 0
    stored_ba_rows: int = 0
    facts_added: Counter = field(default_factory=Counter)
    facts_changed: Counter = field(default_factory=Counter)
    limited_to: int | None = None
    deferred_updates: int = 0
    deferred_creates: int = 0


def identity(job: Job) -> str:
    """Same identity the collector run uses to process one posting once."""
    return job.source_reference or str(job.url)


async def fetch_postings(
    queries: Iterable[BundesagenturQuery], make_collector: CollectorFactory
) -> FetchResult:
    """Run every search (search pages only). A failed search is reported and
    the others still run; the caller decides what a failure means."""
    result = FetchResult()
    seen: set[str] = set()
    for query in queries:
        report = QueryReport(label=query.label, keywords=query.keywords)
        result.queries.append(report)
        collector = make_collector(query)
        try:
            jobs = await collector.fetch()
        except Exception as exc:  # noqa: BLE001 -- isolated, reported search
            report.error = type(exc).__name__
            logger.warning("ba_refresh_query_failed label=%s error=%s", query.label, report.error)
            continue
        report.fetched = len(jobs)
        report.skipped_invalid = collector.skipped_invalid_count
        report.skipped_not_home_office = collector.skipped_not_home_office
        report.truncated = collector.truncated
        report.total_results = collector.total_results
        if collector.truncated and collector.total_results is not None:
            report.left_out = max(
                0, collector.total_results - collector.max_pages * collector.page_size
            )
        for job in jobs:
            # one entry per posting AS STORED: the same posting found by several
            # searches collapses, two different postings sharing a BA reference
            # both stay so that `plan_refresh` can report the conflict
            key = _fingerprint(job)
            if key not in seen:
                seen.add(key)
                result.postings.append(job)
    return result


def _stored_reference(row) -> str | None:
    return row.source_reference or ba_reference_from_url(row.url)


def plan_refresh(
    session: Session, fetched: FetchResult, *, limit: int | None = None
) -> RefreshPlan:
    """Read-only comparison of the fetched postings with the stored vacancies."""
    rows = session.execute(
        select(
            JobRecord.id, JobRecord.fingerprint, JobRecord.url, JobRecord.source_reference
        ).where(JobRecord.source == SOURCE_NAME)
    ).all()
    by_fingerprint = {row.fingerprint: row for row in rows}
    ids_by_reference: dict[str, list[int]] = {}
    for row in rows:
        reference = _stored_reference(row)
        if reference:
            ids_by_reference.setdefault(reference, []).append(row.id)
    records = {
        record.id: record
        for record in session.scalars(select(JobRecord).where(JobRecord.source == SOURCE_NAME))
    }
    with_application = set(
        session.scalars(
            select(ApplicationRecord.job_id).where(ApplicationRecord.job_id.isnot(None))
        )
    )

    plan = RefreshPlan(fetched=len(fetched.postings), stored_ba_rows=len(rows))
    fetched_fingerprints: set[str] = set()
    # a reference carried by two different fingerprints in today's results
    prints_by_reference: dict[str, set[str]] = {}
    for job in fetched.postings:
        reference = storable_reference(job.source_reference)
        if reference:
            prints_by_reference.setdefault(reference, set()).add(_fingerprint(job))
    reported_duplicates: set[str] = set()

    for job in fetched.postings:
        fingerprint = _fingerprint(job)
        fetched_fingerprints.add(fingerprint)
        reference = storable_reference(job.source_reference)
        row = by_fingerprint.get(fingerprint)
        if row is not None:
            plan.existing += 1
            stored = row.source_reference
            if stored and reference and stored != reference:
                plan.conflicts.append(
                    Conflict(
                        CONFLICT_STORED_REFERENCE_DIFFERS,
                        reference,
                        job.title,
                        job.company,
                        [row.id],
                    )
                )
                continue
            owners = [i for i in ids_by_reference.get(reference or "", []) if i != row.id]
            if owners and not stored:
                # this row would take a reference another stored row owns
                plan.conflicts.append(
                    Conflict(
                        CONFLICT_REFERENCE_OTHER_FINGERPRINT,
                        reference,
                        job.title,
                        job.company,
                        sorted(owners),
                    )
                )
                continue
            record = records[row.id]
            added: list[str] = []
            changed: list[str] = []
            for name in STRUCTURED_JOB_FACTS:
                value = getattr(job, name)
                current = getattr(record, name)
                if value is None or current == value:
                    continue
                (added if current is None else changed).append(name)
            if reference and record.source_reference is None:
                added.append("source_reference")
            if added or changed:
                plan.updates.append(PlannedUpdate(row.id, job, added, changed))
                plan.facts_added.update(added)
                plan.facts_changed.update(changed)
            else:
                plan.unchanged += 1
            continue
        if reference and reference in ids_by_reference:
            plan.conflicts.append(
                Conflict(
                    CONFLICT_REFERENCE_OTHER_FINGERPRINT,
                    reference,
                    job.title,
                    job.company,
                    sorted(ids_by_reference[reference]),
                )
            )
            continue
        if reference and len(prints_by_reference.get(reference, ())) > 1:
            if reference not in reported_duplicates:
                reported_duplicates.add(reference)
                plan.conflicts.append(
                    Conflict(CONFLICT_DUPLICATE_IN_RESULTS, reference, job.title, job.company, [])
                )
            continue
        plan.creates.append(job)

    plan.updates.sort(key=lambda update: update.job_id)
    plan.creates.sort(key=identity)
    missing = [row for row in rows if row.fingerprint not in fetched_fingerprints]
    plan.not_returned = len(missing)
    plan.not_returned_with_application = sum(1 for row in missing if row.id in with_application)

    if limit is not None:
        plan.limited_to = limit
        kept_updates = plan.updates[:limit]
        kept_creates = plan.creates[: max(0, limit - len(kept_updates))]
        plan.deferred_updates = len(plan.updates) - len(kept_updates)
        plan.deferred_creates = len(plan.creates) - len(kept_creates)
        plan.updates, plan.creates = kept_updates, kept_creates
    return plan


def apply_updates(session: Session, updates: list[PlannedUpdate]) -> int:
    """Write one batch in the caller's transaction (it must see one database
    state). Each row is re-read; one whose fingerprint changed meanwhile is
    left alone. Returns the number of rows that changed."""
    written = 0
    for planned in updates:
        record = session.get(JobRecord, planned.job_id)
        if record is None or record.source != SOURCE_NAME:
            continue
        if record.fingerprint != _fingerprint(planned.job):
            continue
        reference = storable_reference(planned.job.source_reference)
        if reference is not None and record.source_reference not in (None, reference):
            continue  # the row got another reference since the plan was made
        if (
            reference is not None
            and record.source_reference is None
            and reference_owner_ids(session, reference, exclude_id=record.id)
        ):
            continue  # another row took this reference since the plan was made
        if refresh_job_structured_facts(session, record, planned.job):
            written += 1
    return written


def reference_owner_ids(session: Session, reference: str, *, exclude_id: int | None = None):
    """Ids of stored BA rows that own `reference`, by the stored column or by
    their public BA URL (rows not yet backfilled)."""
    return [
        row.id
        for row in session.execute(
            select(JobRecord.id, JobRecord.url, JobRecord.source_reference).where(
                JobRecord.source == SOURCE_NAME
            )
        )
        if row.id != exclude_id and _stored_reference(row) == reference
    ]


def reference_taken(session: Session, job: Job) -> bool:
    """Re-check right before a create, inside the write transaction: a row with
    this fingerprint or BA reference exists."""
    if (
        session.scalar(select(JobRecord.id).where(JobRecord.fingerprint == _fingerprint(job)))
        is not None
    ):
        return True
    reference = storable_reference(job.source_reference)
    return reference is not None and bool(reference_owner_ids(session, reference))
