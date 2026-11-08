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

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.bundesagentur import BundesagenturAPIError, is_api_key_configured
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


def last_success_at(db: Session, account_key: str) -> datetime | None:
    """When this account's last SUCCESSFUL nationwide step finished (None = never). A failed or
    absent step never advances the watermark, so the next run covers the same period again."""
    rows = db.scalars(
        select(AutomationRunRecord)
        .where(AutomationRunRecord.account_key == account_key)
        .where(AutomationRunRecord.finished_at.is_not(None))
        .order_by(AutomationRunRecord.started_at.desc(), AutomationRunRecord.id.desc())
        .limit(_HISTORY)
    ).all()
    for row in rows:
        try:
            step = json.loads(row.results_json).get(STEP_NAME)
        except ValueError:
            continue
        if isinstance(step, dict) and step.get("status") == "ok":
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


def _entry_first(work: list[tuple[object, Job]]) -> list[tuple[object, Job]]:
    return sorted(work, key=lambda pair: 0 if _ENTRY_TITLE.search(pair[1].title) else 1)


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
    queries = nationwide_remote_queries(settings, window_days=window_days)
    stats: dict[str, int] = {}
    work, failed_queries, skipped_invalid = await _fetch_ba_work(
        db,
        settings,
        queries,
        request_budget=settings.nationwide_remote_max_requests_per_run,
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
        log_name="bundesagentur_nationwide_run",
    )
    result = {**counters, **stats}
    logger.info("bundesagentur_nationwide_summary %s", result)
    return result
