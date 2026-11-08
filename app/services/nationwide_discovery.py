"""TRACK B: nationwide discovery of fully remote development vacancies.

TRACK A (the local commute search, `run_bundesagentur`) searches around Heusenstamm. This module
searches the whole of Germany with development-role terms and no `wo`/radius, so a Junior Python
job in Berlin, Hamburg or Munich is collected whatever the employer's location is. It changes
nothing about how a collected vacancy is judged:

    collect -> detail -> dedup (fingerprint / identity key) -> score -> fit assessment
            -> triage category -> shortlist gate

It only supplies more postings. Whether one is FULLY remote, full time and workable from Germany
is decided afterwards from the posting by the existing assessment; the BA home-office flag is a
helper (an explicit "no home office" drops a posting, an unset flag does not). The distance from
Heusenstamm skips only the commute check, and only for a confirmed fully remote job.

Both tracks write to the same tables and share one identity, so a posting both find is one row;
the history of a row (applications, events, review packages, approvals) is never touched here.
This module sends nothing; any vacancy card or alert comes from the existing, flag-gated
`_publish_persisted_job` tail, exactly as for the local run.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.job_fit_signals import classify_remote
from app.collectors.bundesagentur import BundesagenturAPIError, RequestGate, is_api_key_configured
from app.core.config import reveal
from app.db.models import AutomationRunRecord
from app.models.job import Job
from app.services.collector_runner import (
    CollectorNotConfiguredError,
    TouchedJob,
    _fetch_ba_work,
    _process_ba_work,
)
from app.services.search_strategy import nationwide_remote_queries, nationwide_window_days

logger = logging.getLogger(__name__)

STEP_NAME = "bundesagentur_nationwide"
_HISTORY = 50  # finished runs inspected for the last successful nationwide run
# Entry-level titles are processed first when the per-run cap on new vacancies bites.
_ENTRY_TITLE = re.compile(
    r"junior|berufseinsteig|einsteiger|trainee|entry|absolvent|quereinsteig|werkstudent",
    re.IGNORECASE,
)


def _covered_everything(step: dict) -> bool:
    """A step that reports ok but left work behind (deferred postings, a search cut short by
    the request ceiling or the page limit, a failed search) has NOT covered its period."""
    counters = step.get("counters") or {}
    return step.get("status") == "ok" and not any(
        counters.get(key)
        for key in ("deferred", "truncated_searches", "failed_queries", "detail_unavailable")
    )


def last_success_at(db: Session, account_key: str) -> datetime | None:
    """When this account's last COMPLETE nationwide step finished (None = never). A failed or
    incomplete step never advances the watermark, so the next run covers the same period
    again; only runs that contain a nationwide step are looked at, however many unrelated
    automation runs happened since."""
    rows = db.scalars(
        select(AutomationRunRecord)
        .where(AutomationRunRecord.account_key == account_key)
        .where(AutomationRunRecord.finished_at.is_not(None))
        .where(AutomationRunRecord.results_json.contains(f'"{STEP_NAME}"'))
        .order_by(AutomationRunRecord.started_at.desc(), AutomationRunRecord.id.desc())
        .limit(_HISTORY)
    ).all()
    for row in rows:
        try:
            step = json.loads(row.results_json).get(STEP_NAME)
        except ValueError:
            continue
        if isinstance(step, dict) and _covered_everything(step):
            finished = row.finished_at
            return finished if finished.tzinfo else finished.replace(tzinfo=UTC)
    return None


def nationwide_due(
    db: Session, account_key: str, settings, now: datetime | None = None
) -> tuple[bool, int | None]:
    """(is a nationwide run due, BA freshness window in days for it). Due when the feature is
    enabled and the last successful run is older than the configured interval."""
    if not settings.nationwide_remote_discovery_enabled:
        return False, None
    now = now or datetime.now(UTC)
    last = last_success_at(db, account_key)
    if last is None:
        return True, None
    elapsed = (now - last).total_seconds()
    if elapsed < settings.nationwide_remote_discovery_interval_seconds:
        return False, None
    return True, nationwide_window_days(elapsed / 86_400)


def is_remote_candidate(job: Job) -> bool:
    """Can a posting that is new to the database still qualify as TRACK B? On-site and hybrid
    work far from home cannot (the local track covers what is within reach); a posting that
    mentions remote work, or says nothing but carries the BA home-office flag, may. The real
    decision (fully remote, full time, workable from Germany) stays with the fit assessment."""
    mode = classify_remote(job.title + chr(10) + job.description).mode
    if mode in {"FULLY_REMOTE", "PARTIAL_REMOTE"}:
        return True  # the BA flag, whatever it says, never overrides what the text states
    return mode == "UNKNOWN" and job.home_office_possible is True


_ENTRY_WEIGHT = 3.0


def _entry_first(
    work: list[tuple[object, Job]], today: date | None = None
) -> list[tuple[object, Job]]:
    """A weighted daily shuffle: entry-level titles are three times as likely to come early, but
    no posting is always first or always last. Which postings the per-run caps leave out
    therefore changes from day to day, and postings that are rejected again and again cannot
    starve the others for good (a persistent rejection ledger would need a table)."""
    day = (today or datetime.now(UTC).date()).isoformat()

    def key(pair: tuple[object, Job]) -> float:
        job = pair[1]
        digest = hashlib.sha256(f"{day}:{job.source_reference or job.url}".encode()).digest()
        fraction = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)  # in (0, 1)
        weight = _ENTRY_WEIGHT if _ENTRY_TITLE.search(job.title) else 1.0
        return -(fraction ** (1.0 / weight))

    return sorted(work, key=key)


async def run_nationwide_remote_discovery(
    db: Session,
    settings,
    *,
    touched_jobs: list[TouchedJob] | None = None,
    is_lease_lost: Callable[[], bool] | None = None,
    window_days: int | None = None,
) -> dict[str, int]:
    """Fetch, score and persist one nationwide run. Raises `CollectorNotConfiguredError` without
    a BA key and `BundesagenturAPIError` when EVERY search failed (so the step is recorded as
    failed and the watermark does not advance); a single failing search is isolated and counted.
    Counters are integers (the automation step result shape)."""
    if not is_api_key_configured(reveal(settings.bundesagentur_api_key)):
        raise CollectorNotConfiguredError(
            "Bundesagentur collector is not configured: set BUNDESAGENTUR_API_KEY."
        )
    queries = nationwide_remote_queries(window_days)
    gate = RequestGate(
        settings.nationwide_remote_request_delay_seconds,
        settings.nationwide_remote_max_requests_per_run,
        settings.nationwide_remote_max_detail_per_run,
    )
    stats: dict[str, int] = {}
    work, failed_queries, skipped_invalid = await _fetch_ba_work(
        db,
        settings,
        queries,
        gate=gate,
        isolate_first=True,
        stats=stats,
    )
    if failed_queries >= len(queries):
        raise BundesagenturAPIError("every nationwide Bundesagentur search failed")
    counters = await _process_ba_work(
        db,
        settings,
        _entry_first(work),
        failed_queries,
        skipped_invalid,
        touched_jobs=touched_jobs,
        is_lease_lost=is_lease_lost,
        max_new=settings.nationwide_remote_max_new_per_run,
        gate=gate,
        accept_new=is_remote_candidate,
        log_name="bundesagentur_nationwide_run",
    )
    result = {**counters, **stats}
    logger.info("bundesagentur_nationwide_summary %s", result)
    return result
