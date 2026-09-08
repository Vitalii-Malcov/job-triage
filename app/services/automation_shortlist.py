"""Stage 8C: automatic shortlist + CV/Bewerbung draft preparation,
triggered from `app.services.automation.run_automation_cycle` after the
existing Stage 8A collector steps finish — opt-in via
`Settings.automation_auto_prepare_enabled` (off by default; independent
of `automation_scheduler_enabled`, see `app.core.config.Settings`).

**Reuse, not duplication.** Match/CV/Bewerbung computation is the SAME
service-layer logic the manual endpoints already use
(`app.services.candidate_preparation.prepare_candidate_job_match`/
`prepare_candidate_cv_draft`/`prepare_bewerbung_draft`, themselves
extracted from `app.api.routes` unchanged) — this module adds ONLY:
(1) selecting which jobs from the CURRENT cycle are eligible
(`app.db.repositories.get_current_cycle_candidate_jobs`), (2)
deterministic ranking + a score threshold to decide which of them get
drafts prepared at all, and (3) an automation-level Bewerbung reuse
policy, so an unchanged shortlisted job does not get a brand-new
`BewerbungDraftRecord` every scheduled run. `BewerbungService.generate()`
itself always inserts a new row on every direct call — that
manual-regeneration contract is unchanged; this reuse decision is made
HERE, before `generate()` is ever called.

**Drafts only.** This module creates ONLY `CandidateJobMatch`/
`CandidateCVDraft`/`BewerbungDraft` rows (all pre-existing tables) plus
the technical metadata returned to the caller for persistence in
`AutomationRun.results`. It never sends an application/email, never
approves/auto-approves anything, and never transitions a `JobRecord`'s
status — a shortlist is not equivalent to SAVED; human approval
boundaries from Stage 6E/7D/7E are entirely untouched.

**Per-job failure isolation (spec section 10).** Every eligible job's
match computation, and every shortlisted job's CV/Bewerbung preparation,
runs in its own try/except: one bad job's exception is rolled back,
recorded as `type(exc).__name__` only — never `str(exc)`/`repr(exc)`/a
traceback (this project's GMAIL-003/S8A-004 sanitized-logging
convention) — and the run continues with the next job. A job whose MATCH
computation fails never becomes shortlist-eligible (there is no score to
rank it by) and is counted only in the aggregate `failed` counter, with
no `ShortlistDraftItem` of its own — bounding `items` to at most
`automation_shortlist_max_per_run` entries regardless of how large the
eligible pool is. A SHORTLISTED job whose CV/Bewerbung preparation fails
DOES get a `ShortlistDraftItem` (`status="failed"`), since it already
consumed a shortlist slot.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.bewerbung_generator import BEWERBUNG_GENERATOR_VERSION
from app.agents.cv_adapter import CV_ADAPTER_VERSION
from app.db.bewerbung_repository import get_latest_bewerbung_draft
from app.db.candidate_cv_draft_repository import get_cached_draft, get_draft_by_id
from app.db.models import CandidateCVDraftRecord
from app.db.repositories import get_current_cycle_candidate_jobs
from app.models.automation import ShortlistDraftItem
from app.providers.bewerbung.deterministic import PROVIDER_NAME as DETERMINISTIC_BEWERBUNG_PROVIDER
from app.services.candidate_preparation import (
    prepare_bewerbung_draft,
    prepare_candidate_cv_draft,
    prepare_candidate_job_match,
)

logger = logging.getLogger(__name__)

__all__ = ["prepare_shortlist_drafts"]


def _bewerbung_is_current(existing, cv_draft_record: CandidateCVDraftRecord) -> bool:
    """Stage 8C automation-level reuse identity (spec section 8) — ALL of
    these must match the just-prepared CV draft's own pins for an
    existing `BewerbungDraftRecord` to be reused instead of generating a
    new one. `provider` is pinned to the deterministic default
    (`DETERMINISTIC_BEWERBUNG_PROVIDER`) because automation always calls
    `BewerbungService()` uninjected — the exact same default the manual
    endpoint uses.
    """
    return (
        existing is not None
        and existing.cv_draft_id == cv_draft_record.id
        and existing.match_id == cv_draft_record.match_id
        and existing.candidate_profile_version == cv_draft_record.candidate_profile_version
        and existing.job_snapshot_fingerprint == cv_draft_record.job_snapshot_fingerprint
        and existing.match_algorithm_version == cv_draft_record.match_algorithm_version
        and existing.cv_adapter_version == cv_draft_record.cv_adapter_version
        and existing.bewerbung_generator_version == BEWERBUNG_GENERATOR_VERSION
        and existing.provider == DETERMINISTIC_BEWERBUNG_PROVIDER
    )


async def prepare_shortlist_drafts(db: Session, *, run_started_at: datetime, settings) -> dict:
    """Runs Stage 8C for one automation cycle. Returns an
    AutomationRunStepResult-shaped dict: `{"status", "counters", "items",
    "error_type"}`. `error_type` here is always `None` — a step-wide
    aggregate of independently-sanitized per-job failures has no single
    representative exception type of its own (each failure's own type is
    already on its `ShortlistDraftItem.error_type`); a genuinely
    unexpected STEP-level failure (e.g. the candidate-pool query itself
    raising) is caught by the caller
    (`app.services.automation._run_shortlist_drafts_step`), mirroring
    `_run_step`'s own two-layer contract for the Stage 8A collector
    steps.
    """
    eligible_jobs = get_current_cycle_candidate_jobs(db, since=run_started_at)
    candidate_jobs = len(eligible_jobs)

    matched: list[tuple] = []
    failed = 0

    for job in eligible_jobs:
        try:
            match = prepare_candidate_job_match(db, job.id, force_recompute=False)
        except Exception as exc:
            db.rollback()
            logger.warning(
                "automation_shortlist_match_failed job_id=%s error_type=%s",
                job.id,
                type(exc).__name__,
            )
            failed += 1
            continue
        if match is not None:
            matched.append((job, match))

    shortlist_candidates = [
        (job, match)
        for job, match in matched
        if match.overall_score >= settings.automation_shortlist_min_match_score
    ]
    # Deterministic ranking (spec section 6): match score DESC, then
    # JobRecord.score DESC, then job id ASC as the final, always-unique
    # tie-break. Never randomness.
    shortlist_candidates.sort(key=lambda pair: (-pair[1].overall_score, -pair[0].score, pair[0].id))
    shortlisted = shortlist_candidates[: settings.automation_shortlist_max_per_run]

    items: list[ShortlistDraftItem] = []
    cv_created = cv_reused = bewerbung_created = bewerbung_reused = 0

    for job, match in shortlisted:
        try:
            cv_was_cached = (
                get_cached_draft(db, match_id=match.id, cv_adapter_version=CV_ADAPTER_VERSION)
                is not None
            )
            cv_draft = prepare_candidate_cv_draft(db, job.id, match.id, force_recompute=False)
            cv_draft_record = get_draft_by_id(db, cv_draft.id)

            existing_bewerbung = get_latest_bewerbung_draft(db, job.id)
            if _bewerbung_is_current(existing_bewerbung, cv_draft_record):
                bewerbung_draft_id = existing_bewerbung.id
                bewerbung_was_reused = True
                bewerbung_reused += 1
            else:
                bewerbung = await prepare_bewerbung_draft(db, job.id, cv_draft.id)
                bewerbung_draft_id = bewerbung.id
                bewerbung_was_reused = False
                bewerbung_created += 1

            if cv_was_cached:
                cv_reused += 1
            else:
                cv_created += 1

            items.append(
                ShortlistDraftItem(
                    job_id=job.id,
                    match_id=match.id,
                    match_score=match.overall_score,
                    cv_draft_id=cv_draft.id,
                    bewerbung_draft_id=bewerbung_draft_id,
                    cv_reused=cv_was_cached,
                    bewerbung_reused=bewerbung_was_reused,
                    status="ok",
                    error_type=None,
                )
            )
        except Exception as exc:
            db.rollback()
            logger.warning(
                "automation_shortlist_draft_prep_failed job_id=%s error_type=%s",
                job.id,
                type(exc).__name__,
            )
            failed += 1
            items.append(
                ShortlistDraftItem(
                    job_id=job.id,
                    match_id=match.id,
                    match_score=match.overall_score,
                    cv_draft_id=None,
                    bewerbung_draft_id=None,
                    cv_reused=False,
                    bewerbung_reused=False,
                    status="failed",
                    error_type=type(exc).__name__,
                )
            )

    if candidate_jobs == 0 or failed == 0:
        status = "ok"
    elif failed == candidate_jobs:
        status = "failed"
    else:
        status = "partial"

    counters = {
        "candidate_jobs": candidate_jobs,
        "matched": len(matched),
        "shortlisted": len(shortlisted),
        "cv_created": cv_created,
        "cv_reused": cv_reused,
        "bewerbung_created": bewerbung_created,
        "bewerbung_reused": bewerbung_reused,
        "failed": failed,
    }

    # AutomationRun.results_json is persisted via a plain json.dumps() of
    # the whole step_results dict (app.db.automation_repository.finish_run)
    # -- exactly like the Stage 8A collector steps' own `counters`, `items`
    # must already be plain JSON-serializable dicts here, never live
    # ShortlistDraftItem/pydantic instances.
    plain_items = [item.model_dump() for item in items]

    # Privacy-safe: technical metadata/counters only, never candidate
    # name/CV/Bewerbung content/job description (mirrors Stage 6B/6C/6D's
    # own logging convention).
    logger.info(
        "automation_shortlist_drafts_finished candidate_jobs=%s matched=%s shortlisted=%s "
        "cv_created=%s cv_reused=%s bewerbung_created=%s bewerbung_reused=%s failed=%s status=%s",
        candidate_jobs,
        len(matched),
        len(shortlisted),
        cv_created,
        cv_reused,
        bewerbung_created,
        bewerbung_reused,
        failed,
        status,
    )

    return {"status": status, "counters": counters, "items": plain_items, "error_type": None}
