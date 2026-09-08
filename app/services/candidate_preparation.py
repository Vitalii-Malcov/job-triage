"""Service-layer orchestration for Candidate Profile <-> Job matching
(Stage 6B), Tailored CV Draft generation (Stage 6C), and Bewerbung draft
generation (Stage 6D).

**Extracted from app/api/routes.py, not reimplemented (Stage 8C
S8C-ARCH-01).** `prepare_candidate_job_match`/`prepare_candidate_cv_draft`/
`prepare_bewerbung_draft` are the EXACT SAME logic that used to live as
`app.api.routes._run_candidate_job_match`/`_run_candidate_cv_draft`/
`_run_bewerbung_draft` — moved here verbatim (same cache-identity checks,
same staleness/profile-version/job-snapshot checks, same exception
classes, same Company Research traceability behavior, same
`force_recompute` semantics) so that `app.services.automation`'s Stage 8C
shortlist/draft preparation can call them without importing
`app.api.routes` — services must never import the API layer (that would
invert the dependency direction: API depends on services, never the
reverse). `app/api/routes.py` now imports these three functions instead
of defining its own private copies; its endpoints' behavior, HTTP status
mapping, and error handling are unchanged.
"""

import json
import logging

from sqlalchemy.orm import Session

from app.agents.candidate_job_matcher import ALGORITHM_VERSION, JobMatchInput, compute_match
from app.agents.cv_adapter import (
    CV_ADAPTER_VERSION,
    CVDraftJobChangedError,
    CVDraftMatchJobMismatchError,
    CVDraftMatchNotFoundError,
    CVDraftProfileChangedError,
    compute_cv_draft,
)
from app.db.candidate_cv_draft_repository import (
    create_draft,
    get_cached_draft,
    to_tailored_cv_draft,
)
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    create_match,
    get_cached_match,
    get_match_by_id,
    to_candidate_job_match,
)
from app.db.candidate_profile_repository import (
    get_or_create_candidate_profile,
    to_candidate_profile_response,
)
from app.db.repositories import get_job_by_id
from app.models.bewerbung import BewerbungDraft
from app.models.candidate_job_match import CandidateJobMatch
from app.models.cv_draft import TailoredCVDraft
from app.services.bewerbung import BewerbungService
from app.services.company_research import AmbiguousCompanyIdentityError, CompanyResearchService

logger = logging.getLogger(__name__)

__all__ = [
    "prepare_candidate_job_match",
    "prepare_candidate_cv_draft",
    "prepare_bewerbung_draft",
]


def prepare_candidate_job_match(
    db: Session, job_id: int, *, force_recompute: bool
) -> CandidateJobMatch | None:
    """Compute (or reuse a cached) Candidate Profile <-> Job match analysis
    (Stage 6B). Returns None if the job doesn't exist — callers translate
    that into their own 404.

    Deliberately synchronous (unlike run_company_research_for_job/
    run_bundesagentur in app.services.collector_runner):
    app.agents.candidate_job_matcher.compute_match performs zero
    I/O — no network, no LLM (Stage 6B sections 26/27) — so there is
    nothing here to await. Company Research is read via
    CompanyResearchService().get_cached (a pure DB read, never
    get_or_run) — matching must never trigger a company-research provider
    call, and an ambiguous company identity in that unrelated, optional
    feature must not block matching (section 19).
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None

    profile_record = get_or_create_candidate_profile(db)
    profile = to_candidate_profile_response(profile_record)

    company_research_id: int | None = None
    try:
        cached_research = CompanyResearchService().get_cached(db, job)
    except AmbiguousCompanyIdentityError:
        cached_research = None
        logger.warning("candidate_job_match_company_research_ambiguous job_id=%s", job_id)
    if cached_research is not None:
        company_research_id = cached_research.id

    fingerprint = compute_job_snapshot_fingerprint(job)

    if not force_recompute:
        existing = get_cached_match(
            db,
            job_id=job_id,
            candidate_profile_version=profile.profile_version,
            job_snapshot_fingerprint=fingerprint,
            algorithm_version=ALGORITHM_VERSION,
        )
        if existing is not None:
            return to_candidate_job_match(existing)

    job_input = JobMatchInput(
        job_id=job.id,
        title=job.title,
        description=job.description,
        must_have_skills=json.loads(job.must_have_skills_json),
        nice_to_have_skills=json.loads(job.nice_to_have_skills_json),
    )
    data = compute_match(job_input, profile, company_research_id=company_research_id)
    record, _created = create_match(db, data, fingerprint)

    # Privacy-safe (Stage 6B section 35): technical metadata only, never
    # candidate names/experience/project/skill content.
    logger.info(
        "candidate_job_match_computed job_id=%s profile_version=%s algorithm_version=%s "
        "match_id=%s overall_score=%s",
        job_id,
        profile.profile_version,
        ALGORITHM_VERSION,
        record.id,
        record.overall_score,
    )
    return to_candidate_job_match(record)


def prepare_candidate_cv_draft(
    db: Session, job_id: int, match_id: int, *, force_recompute: bool
) -> TailoredCVDraft | None:
    """Compute (or reuse a cached) Tailored CV Draft pinned to one
    specific persisted match (Stage 6C). Returns None if the job doesn't
    exist — callers translate that into their own 404. Raises
    CVDraftMatchNotFoundError / CVDraftMatchJobMismatchError /
    CVDraftProfileChangedError / CVDraftJobChangedError for every other
    validation failure — see those classes' docstrings in
    app/agents/cv_adapter.py for the exact 404/422/409 semantics this
    function's callers map them to.

    Deliberately synchronous, like prepare_candidate_job_match:
    compute_match (0 network) has already run in Stage 6B, and
    compute_cv_draft performs zero I/O of its own (no LLM, no network —
    section 29/42), so nothing here needs to await.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None

    match_record = get_match_by_id(db, match_id)
    if match_record is None:
        raise CVDraftMatchNotFoundError(match_id)
    if match_record.job_id != job_id:
        raise CVDraftMatchJobMismatchError(match_id=match_id, job_id=job_id)

    profile_record = get_or_create_candidate_profile(db)
    if profile_record.profile_version != match_record.candidate_profile_version:
        raise CVDraftProfileChangedError(
            match_profile_version=match_record.candidate_profile_version,
            current_profile_version=profile_record.profile_version,
        )

    current_fingerprint = compute_job_snapshot_fingerprint(job)
    if current_fingerprint != match_record.job_snapshot_fingerprint:
        raise CVDraftJobChangedError()

    if not force_recompute:
        existing = get_cached_draft(db, match_id=match_id, cv_adapter_version=CV_ADAPTER_VERSION)
        if existing is not None:
            return to_tailored_cv_draft(existing)

    profile = to_candidate_profile_response(profile_record)
    match = to_candidate_job_match(match_record)
    data = compute_cv_draft(profile, match)
    record, _created = create_draft(db, job_id, current_fingerprint, data)

    # Privacy-safe (Stage 6C section 41): technical metadata only, never
    # candidate name/summary/experience/project/skill/language content.
    logger.info(
        "candidate_cv_draft_computed job_id=%s match_id=%s draft_id=%s profile_version=%s "
        "adapter_version=%s status=%s",
        job_id,
        match_id,
        record.id,
        profile.profile_version,
        CV_ADAPTER_VERSION,
        record.status,
    )
    return to_tailored_cv_draft(record)


async def prepare_bewerbung_draft(
    db: Session, job_id: int, cv_draft_id: int
) -> BewerbungDraft | None:
    """Generate a Bewerbung draft pinned to one specific persisted CV draft
    (Stage 6D). Returns None if the job doesn't exist — callers translate
    that into their own 404. Raises BewerbungCVDraftNotFoundError /
    BewerbungCVDraftJobMismatchError / BewerbungProfileChangedError /
    BewerbungJobChangedError / BewerbungMatchNotFoundError /
    BewerbungMatchInconsistentError / BewerbungPlanRejectedError /
    BewerbungProviderError for every other failure — see those classes'
    docstrings in app/agents/bewerbung_generator.py,
    app/agents/bewerbung_renderer.py, and app/providers/bewerbung/base.py
    for the exact status-code mapping the API layer applies.

    Unlike prepare_candidate_job_match/prepare_candidate_cv_draft, this is
    async: BewerbungService.generate calls out to a BewerbungProvider,
    which may (for a future non-deterministic provider) perform real I/O.
    Always creates a NEW BewerbungDraftRecord row (Stage 6D section 35,
    unchanged) — Stage 8C's own automation-level reuse policy (deciding
    WHETHER to call this at all) lives in app.services.automation, never
    here, so manual callers (the API endpoint) keep their existing
    always-regenerate behavior unmodified.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None

    service = BewerbungService()
    return await service.generate(db, job, cv_draft_id)
