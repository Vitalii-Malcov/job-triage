"""Read-only query behind `/ui/review`: one globally sorted, cursor-paged manual review queue.

Where the work goes, and what it never does:

* The queue is built from the Stage 21 assessment cache. A cache row counts only while its stored
  input key equals the current one (`fit_cache.current_keys`: SQL and hashing, no assessment). A
  FRESH row is turned into a queue entry straight from the stored criteria -- no live assessment.
  A fresh confirmed barrier or an already applied vacancy is dropped before anything else is read.
* A STALE or missing row is never trusted. At most `STALE_LIVE_BUDGET` of them are assessed live
  per request (Junior Python/Backend titles first, then newest first); the rest are only counted
  (`stale_unassessed`), so a cold cache costs a bounded, known amount instead of a full recompute.
  Refilling the cache stays an explicit offline step (`scripts/stage21_assess_vacancies.py`).
* The whole sortable set is ordered by `review_queue.sort_key` (Junior Python/Backend, developer,
  adjacent, other, hybrid admin last) BEFORE paging, and pages are keyset-based on that sort key:
  no duplicates, stable under inserts, no scan "budget" that hides older rows.
* Nothing is written, sent or changed.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobFitAssessmentRecord, JobRecord
from app.services import fit_cache, review_queue
from app.services.job_fit_assessment import Criterion, FitAssessment, assess_job
from app.ui.url_safety import safe_external_url

PAGE_SIZE = 25
# live assessments one request may run for rows whose cache entry is stale or missing
STALE_LIVE_BUDGET = 60
_CURSOR = re.compile(r"^-?\d+(\.-?\d+){4}$")
_LOAD_CHUNK = 500


@dataclass
class CachedAssessment(FitAssessment):
    """A stored assessment of a FRESH cache row. The tier is the stored one: the facts that
    derive it (stated German level, required years ...) are not stored, so it is not recomputed."""

    cached_tier: str = "NEEDS_OPERATOR"
    cached_reasons: list[str] = field(default_factory=list)

    @property
    def fit_tier(self):  # type: ignore[override]
        return self.cached_tier

    @property
    def tier_reasons(self) -> list[str]:  # type: ignore[override]
        return self.cached_reasons


@dataclass(frozen=True)
class ReviewFilters:
    reason: str = ""
    source: str = ""
    profession: str = ""  # one of review_queue.PROFESSIONS, or "" for all
    cursor: str = ""  # sort key of the last item of the previous page


@dataclass(frozen=True)
class ReviewPage:
    items: list[review_queue.ReviewItem]
    next_cursor: str | None
    total: int  # queue members matching the filters among the assessed vacancies
    fresh: int  # members read from a fresh cache row
    live_assessed: int  # members that needed a live assessment (stale or missing cache row)
    stale_unassessed: int  # vacancies whose assessment is stale and was NOT recomputed here
    from_cache: bool = False


def encode_cursor(item: review_queue.ReviewItem) -> str:
    return ".".join(str(int(part)) for part in review_queue.sort_key(item))


def parse_cursor(value: str) -> tuple[int, ...] | None:
    """A malformed cursor is ignored (first page), never trusted or passed to SQL."""
    if not value or len(value) > 60 or not _CURSOR.match(value):
        return None
    return tuple(int(part) for part in value.split("."))


def _cached_assessment(job: JobRecord, row: JobFitAssessmentRecord) -> CachedAssessment:
    return CachedAssessment(
        job_id=job.id,
        company=job.company,
        title=job.title,
        criteria=[Criterion(**c) for c in json.loads(row.criteria_json)],
        cached_tier=row.fit_tier,
        cached_reasons=json.loads(row.tier_reasons_json),
    )


def _light_rows(db: Session, source: str, cached: bool):
    if cached:
        query = select(
            JobRecord.id,
            JobRecord.title,
            JobFitAssessmentRecord.input_key,
            JobFitAssessmentRecord.fit_tier,
            JobFitAssessmentRecord.applied,
        ).outerjoin(JobFitAssessmentRecord, JobFitAssessmentRecord.job_id == JobRecord.id)
    else:
        query = select(JobRecord.id, JobRecord.title)
    if source:
        query = query.where(JobRecord.source == source)
    return db.execute(query.order_by(JobRecord.id.desc())).all()


def _items_from_cache(db: Session, ids: Sequence[int]) -> list[review_queue.ReviewItem]:
    items = []
    for start in range(0, len(ids), _LOAD_CHUNK):
        chunk = ids[start : start + _LOAD_CHUNK]
        pairs = db.execute(
            select(JobRecord, JobFitAssessmentRecord)
            .join(JobFitAssessmentRecord, JobFitAssessmentRecord.job_id == JobRecord.id)
            .where(JobRecord.id.in_(chunk))
        ).all()
        for job, row in pairs:
            item = review_queue.review_item(
                job, _cached_assessment(job, row), safe_external_url(job.url)
            )
            if item is not None:
                items.append(item)
    return items


def review_page(
    db: Session,
    filters: ReviewFilters,
    *,
    assess: Callable[[Session, int], FitAssessment] = assess_job,
    page_size: int = PAGE_SIZE,
    stale_budget: int = STALE_LIVE_BUDGET,
    use_cache: bool = True,
) -> ReviewPage:
    cached = use_cache and fit_cache.cache_in_use(db)
    inputs = fit_cache.global_inputs(db) if cached else None
    rows = _light_rows(db, filters.source, cached)
    current = fit_cache.current_keys(db, [r[0] for r in rows], inputs) if cached else {}

    fresh_ids: list[int] = []
    stale: list[tuple[int, str]] = []
    for row in rows:
        if cached and row[2] is not None and row[2] == current.get(row[0]):
            if row[3] != "NOT_SUITABLE" and not row[4]:
                fresh_ids.append(row[0])
        else:
            stale.append((row[0], row[1]))

    items = _items_from_cache(db, fresh_ids)
    # junior titles first, then newest; `rows` is already newest first and sort is stable
    stale.sort(key=lambda r: 0 if review_queue.is_junior_python_title(r[1]) else 1)
    live_ids: set[int] = set()
    for job_id, _title in stale[:stale_budget]:
        job = db.get(JobRecord, job_id)
        item = review_queue.review_item(job, assess(db, job_id), safe_external_url(job.url))
        if item is not None:
            items.append(item)
            live_ids.add(item.job_id)
    stale_unassessed = max(len(stale) - stale_budget, 0)

    if cached and fit_cache.global_inputs(db) != inputs:
        # profile, rules or applications changed while the page was built: the cached rows may
        # have been compared with outdated inputs, so build the page again without the cache
        return review_page(
            db,
            filters,
            assess=assess,
            page_size=page_size,
            stale_budget=stale_budget,
            use_cache=False,
        )

    items = [
        i
        for i in items
        if (not filters.reason or filters.reason in (i.reason_code, *i.also))
        and review_queue.in_profession(i, filters.profession)
    ]
    live = sum(1 for i in items if i.job_id in live_ids)
    items.sort(key=review_queue.sort_key)
    after = parse_cursor(filters.cursor)
    visible = [i for i in items if after is None or review_queue.sort_key(i) > after]
    page = visible[:page_size]
    return ReviewPage(
        items=page,
        next_cursor=encode_cursor(page[-1]) if len(visible) > page_size else None,
        total=len(items),
        fresh=len(items) - live,
        live_assessed=live,
        stale_unassessed=stale_unassessed,
        from_cache=cached,
    )
