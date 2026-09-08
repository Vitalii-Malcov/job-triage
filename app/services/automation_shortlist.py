"""Stage 8C: automatic shortlist + CV/Bewerbung draft preparation,
triggered from `app.services.automation.run_automation_cycle` after the
existing Stage 8A collector steps finish — opt-in via
`Settings.automation_auto_prepare_enabled` (off by default; independent
of `automation_scheduler_enabled`, see `app.core.config.Settings`).

**Reuse, not duplication.** Match/CV/Bewerbung computation is the SAME
service-layer logic the manual endpoints already use
(`app.services.candidate_preparation.prepare_candidate_job_match`/
`prepare_candidate_cv_draft_with_outcome`/`prepare_bewerbung_draft`,
themselves extracted from `app.api.routes` unchanged) — this module adds
ONLY: (1) selecting which jobs THIS run's own collectors actually
touched, bounded and cheaply preselected before any match computation
(S8C-BOUND-001, S8C-POOL-001 — see `_preselect_candidate_job_ids`
below), (2) deterministic ranking + a score threshold to decide which of
them get drafts prepared at all, and (3) an automation-level Bewerbung
reuse policy, so an unchanged shortlisted job does not get a brand-new
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

**Exact run attribution, not a timestamp heuristic (S8C-POOL-001, Codex
review).** Earlier versions of this module selected candidate jobs via
`JobRecord.last_seen_at >= run.started_at` — a heuristic that could also
match a job independently refreshed by a CONCURRENT collector run for a
different account, or a manual endpoint call, in the same time window.
`prepare_shortlist_drafts` now takes `touched_jobs`: the exact, in-memory
list of `app.services.collector_runner.TouchedJob` entries THIS run's own
`run_bundesagentur`/`run_xing` calls appended immediately after each
successful persist (see `app.services.automation.run_automation_cycle`,
which creates one fresh list per run and passes it to both collector
steps). Only jobs present in that list are ever candidates.

**Two-stage bounded candidate selection (S8C-BOUND-001, Codex review).**
Stage 1 (`_preselect_candidate_job_ids`): cheap, in-memory-only —
dedupe `touched_jobs` by job_id, keep only entries whose OWN captured
status/recommendation looked eligible, sort by `score DESC, job_id ASC`,
cap to `settings.automation_candidate_match_max_per_run`. This avoids
ever building a huge unbounded `WHERE id IN (...)` clause or computing a
`CandidateJobMatch` for every job a large collector run touched. Stage 2
(`app.db.repositories.get_jobs_by_ids_if_eligible`): reloads ONLY that
bounded id set from the DB and re-checks status/recommendation there —
the DB is always authoritative, so a job the cheap preselection thought
eligible but whose status changed in the meantime (e.g. a human moved it
to APPLIED) is excluded here. `automation_shortlist_max_per_run` remains
a SEPARATE, later bound on how many of the MATCHED candidates actually
get CV/Bewerbung drafts (spec section 6/7) — see module docstring's
"final draft cap" note.

**Per-job failure isolation (S8C-AUDIT-001/002, Codex review).** Every
candidate job's match computation, and every shortlisted job's CV
preparation and (separately) Bewerbung preparation, each run in their
OWN try/except: one bad job's exception is rolled back, recorded as
`type(exc).__name__` only — never `str(exc)`/`repr(exc)`/a traceback
(this project's GMAIL-003/S8A-004 sanitized-logging convention) — and
processing continues with the next job/phase. Splitting CV and Bewerbung
into separate try/except blocks (rather than one, as an earlier version
did) is what makes the audit truthful: if CV preparation SUCCEEDS and
durably commits, then Bewerbung preparation FAILS, the resulting
`ShortlistDraftItem` still reports the real `cv_draft_id`/`cv_reused` —
Bewerbung's own `db.rollback()` cannot and does not un-commit an earlier,
already-committed CV draft. A job whose MATCH computation fails never
becomes shortlist-eligible (there is no score to rank it by) and is
recorded only as an `AutomationJobFailure(phase="match")` — no
`ShortlistDraftItem` of its own, since it never reached the shortlist
(bounding `items` to at most `automation_shortlist_max_per_run` entries).
A SHORTLISTED job whose CV or Bewerbung preparation fails DOES get a
`ShortlistDraftItem` (`status="failed"`, `phase` set) AND a matching
`AutomationJobFailure` entry — the same failure, two complementary,
uniformly-queryable views (see `app.models.automation.AutomationJobFailure`'s
own docstring). Every candidate job increments the step's `failed`
counter at most once, whichever phase it failed in.
"""

import logging

from sqlalchemy.orm import Session

from app.agents.bewerbung_generator import BEWERBUNG_GENERATOR_VERSION
from app.db.bewerbung_repository import get_latest_bewerbung_draft
from app.db.candidate_cv_draft_repository import get_draft_by_id
from app.db.models import CandidateCVDraftRecord
from app.db.repositories import get_jobs_by_ids_if_eligible
from app.models.automation import AutomationJobFailure, ShortlistDraftItem
from app.providers.bewerbung.deterministic import PROVIDER_NAME as DETERMINISTIC_BEWERBUNG_PROVIDER
from app.services.candidate_preparation import (
    prepare_bewerbung_draft,
    prepare_candidate_cv_draft_with_outcome,
    prepare_candidate_job_match,
)
from app.services.collector_runner import TouchedJob

logger = logging.getLogger(__name__)

__all__ = ["prepare_shortlist_drafts"]

_ELIGIBLE_STATUSES = frozenset({"NEW", "SAVED"})
_ELIGIBLE_RECOMMENDATIONS = frozenset({"APPLY", "MAYBE"})


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


def _preselect_candidate_job_ids(
    touched_jobs: list[TouchedJob], *, max_candidates: int
) -> list[int]:
    """S8C-BOUND-001/S8C-POOL-001 (Codex review): stage 1 of the two-stage
    bound — cheap, in-memory-only, no DB access. Dedupes `touched_jobs`
    by `job_id` (last entry for a given id wins — later touches reflect
    more current state), keeps only entries whose OWN captured
    status/recommendation looked eligible at touch time (a cheap filter;
    the DB reload in `get_jobs_by_ids_if_eligible` re-checks
    authoritatively), sorts by `score DESC, job_id ASC` (deterministic,
    never randomness), and caps to `max_candidates` BEFORE any
    `CandidateJobMatch` is ever computed.
    """
    by_job_id: dict[int, TouchedJob] = {}
    for touched in touched_jobs:
        by_job_id[touched.job_id] = touched

    cheaply_eligible = [
        touched
        for touched in by_job_id.values()
        if touched.status in _ELIGIBLE_STATUSES
        and touched.recommendation in _ELIGIBLE_RECOMMENDATIONS
    ]
    cheaply_eligible.sort(key=lambda touched: (-touched.score, touched.job_id))
    return [touched.job_id for touched in cheaply_eligible[:max_candidates]]


async def prepare_shortlist_drafts(
    db: Session, *, touched_jobs: list[TouchedJob], settings
) -> dict:
    """Runs Stage 8C for one automation cycle. Returns an
    AutomationRunStepResult-shaped dict: `{"status", "counters", "items",
    "failures", "error_type"}` — all plain, JSON-serializable values
    (`app.db.automation_repository.finish_run` persists the whole
    `step_results` dict via a plain `json.dumps`). `error_type` here is
    always `None` — a step-wide aggregate of independently-sanitized
    per-job failures has no single representative exception type of its
    own (each failure's own type is already on its `AutomationJobFailure`/
    `ShortlistDraftItem`); a genuinely unexpected STEP-level failure
    (e.g. the DB revalidation query itself raising) is caught by the
    caller (`app.services.automation._run_shortlist_drafts_step`),
    mirroring `_run_step`'s own two-layer contract for the Stage 8A
    collector steps.
    """
    candidate_ids = _preselect_candidate_job_ids(
        touched_jobs, max_candidates=settings.automation_candidate_match_max_per_run
    )
    eligible_jobs = get_jobs_by_ids_if_eligible(db, candidate_ids)
    candidate_jobs = len(eligible_jobs)

    matched: list[tuple] = []
    failed = 0
    failures: list[AutomationJobFailure] = []

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
            failures.append(
                AutomationJobFailure(job_id=job.id, phase="match", error_type=type(exc).__name__)
            )
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
        # --- CV phase (S8C-CACHE-001: race-safe created/reused via the
        # repository's own INSERT-or-reload outcome, never a separate
        # pre-check-then-call TOCTOU). ---
        try:
            cv_draft, cv_was_created = prepare_candidate_cv_draft_with_outcome(
                db, job.id, match.id, force_recompute=False
            )
            cv_was_reused = not cv_was_created
        except Exception as exc:
            db.rollback()
            logger.warning(
                "automation_shortlist_cv_prep_failed job_id=%s error_type=%s",
                job.id,
                type(exc).__name__,
            )
            failed += 1
            failures.append(
                AutomationJobFailure(job_id=job.id, phase="cv", error_type=type(exc).__name__)
            )
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
                    phase="cv",
                    error_type=type(exc).__name__,
                )
            )
            continue

        if cv_was_created:
            cv_created += 1
        else:
            cv_reused += 1

        # --- Bewerbung phase (S8C-AUDIT-001: a SEPARATE try/except so a
        # failure here can never deny the CV outcome captured above). ---
        try:
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
        except Exception as exc:
            db.rollback()
            logger.warning(
                "automation_shortlist_bewerbung_prep_failed job_id=%s error_type=%s",
                job.id,
                type(exc).__name__,
            )
            failed += 1
            failures.append(
                AutomationJobFailure(
                    job_id=job.id, phase="bewerbung", error_type=type(exc).__name__
                )
            )
            items.append(
                ShortlistDraftItem(
                    job_id=job.id,
                    match_id=match.id,
                    match_score=match.overall_score,
                    cv_draft_id=cv_draft.id,
                    bewerbung_draft_id=None,
                    cv_reused=cv_was_reused,
                    bewerbung_reused=False,
                    status="failed",
                    phase="bewerbung",
                    error_type=type(exc).__name__,
                )
            )
            continue

        items.append(
            ShortlistDraftItem(
                job_id=job.id,
                match_id=match.id,
                match_score=match.overall_score,
                cv_draft_id=cv_draft.id,
                bewerbung_draft_id=bewerbung_draft_id,
                cv_reused=cv_was_reused,
                bewerbung_reused=bewerbung_was_reused,
                status="ok",
                phase=None,
                error_type=None,
            )
        )

    # S8C-STATUS-001-adjacent (module-local): the denominator for this
    # step's own status must match the actual bounded work attempted
    # (candidate_jobs after preselection/revalidation), never the raw
    # touched-job count -- a job dropped by the bound or by DB
    # revalidation was never attempted and must not count as a failure.
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
    # and `failures` must already be plain JSON-serializable dicts here,
    # never live pydantic instances.
    plain_items = [item.model_dump() for item in items]
    plain_failures = [failure.model_dump() for failure in failures]

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

    return {
        "status": status,
        "counters": counters,
        "items": plain_items,
        "failures": plain_failures,
        "error_type": None,
    }
