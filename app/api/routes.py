import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.agents.bewerbung_generator import (
    BewerbungCVDraftJobMismatchError,
    BewerbungCVDraftNotFoundError,
    BewerbungJobChangedError,
    BewerbungMatchInconsistentError,
    BewerbungMatchNotFoundError,
    BewerbungProfileChangedError,
)
from app.agents.bewerbung_renderer import BewerbungPlanRejectedError
from app.agents.candidate_job_matcher import ALGORITHM_VERSION, JobMatchInput, compute_match
from app.agents.cv_adapter import (
    CV_ADAPTER_VERSION,
    CVDraftJobChangedError,
    CVDraftMatchJobMismatchError,
    CVDraftMatchNotFoundError,
    CVDraftProfileChangedError,
    compute_cv_draft,
)
from app.agents.job_scorer import JobScorer
from app.agents.review_package_builder import (
    ReviewBewerbungDraftJobMismatchError,
    ReviewBewerbungDraftNotFoundError,
    ReviewCurrentJobMissingError,
    ReviewCurrentProfileMissingError,
    ReviewCVDraftJobMismatchError,
    ReviewCVDraftNotFoundError,
    ReviewJobChangedError,
    ReviewManualOverrideAcknowledgmentRequiredError,
    ReviewNotFoundError,
    ReviewNotPendingError,
    ReviewParagraphIndexError,
    ReviewProfileChangedError,
    ReviewSourceMismatchError,
    ReviewVersionConflictError,
)
from app.agents.skill_extractor import extract_skills
from app.collectors.base import CollectorError, CollectorNotConfiguredError, is_configured
from app.collectors.bundesagentur import BundesagenturCollector, is_api_key_configured
from app.collectors.xing_email import XingEmailCollector
from app.core.config import get_settings
from app.db.bewerbung_repository import (
    get_bewerbung_draft_by_id,
    get_latest_bewerbung_draft,
    to_bewerbung_draft,
)
from app.db.candidate_cv_draft_repository import (
    create_draft,
    get_cached_draft,
    get_draft_by_id,
    get_latest_draft,
    to_tailored_cv_draft,
)
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    create_match,
    get_cached_match,
    get_latest_match,
    get_match_by_id,
    to_candidate_job_match,
)
from app.db.candidate_profile_repository import (
    CandidateProfileVersionConflictError,
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
    to_candidate_profile_response,
)
from app.db.models import JobRecord, UserProfile
from app.db.repositories import (
    get_job_by_fingerprint,
    get_job_by_id,
    get_or_create_default_profile,
    is_message_processed,
    list_jobs,
    mark_message_processed,
    profile_skills,
    update_job_status,
    upsert_job,
)
from app.db.review_package_repository import (
    get_current_revision,
    get_latest_review_for_job,
    get_review_by_id,
    to_review_package,
)
from app.db.session import get_db
from app.domain.status_transitions import InvalidStatusTransitionError
from app.models.application_status import ApplicationStatus
from app.models.bewerbung import BewerbungDraft, BewerbungDraftRequest
from app.models.candidate_job_match import CandidateJobMatch, MatchRequest
from app.models.candidate_profile import CandidateProfile, CandidateProfilePatchRequest
from app.models.company_research import (
    CompanyResearchResponse,
    CompanyResearchRunResponse,
    ResearchRequest,
)
from app.models.cv_draft import CVDraftRequest, TailoredCVDraft
from app.models.job import Job, JobDetail, JobListItem, JobScore, StatusUpdateRequest
from app.models.review_package import (
    ReviewPackage,
    ReviewPackageApproveRequest,
    ReviewPackageCreateRequest,
    ReviewPackagePatchRequest,
    ReviewPackageRejectRequest,
)
from app.providers.base import ProviderNotConfiguredError
from app.providers.bewerbung.base import BewerbungProviderError, BewerbungProviderNotConfiguredError
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_bewerbung_rate_limit,
    enforce_collector_rate_limit,
    enforce_company_research_rate_limit,
    enforce_cv_draft_rate_limit,
    enforce_match_rate_limit,
    enforce_rate_limit,
    enforce_review_write_rate_limit,
    enforce_xing_rate_limit,
)
from app.services.bewerbung import BewerbungService
from app.services.company_research import (
    AmbiguousCompanyIdentityError,
    CompanyResearchService,
    InvalidCompanyIdentityError,
)
from app.services.review_package import ReviewPackageService, get_approved_package
from app.services.telegram import TelegramNotifier

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _score_and_persist(
    db: Session, profile: UserProfile, job: Job
) -> tuple[JobRecord, JobScore, bool]:
    """Score a Job against the given profile and persist it.

    Shared by POST /jobs/score and the collector run endpoint so scoring +
    deduplication logic lives in exactly one place.
    """
    result = JobScorer(profile_skills(profile)).score(job)
    record, created = upsert_job(db, job, result)
    result.is_duplicate = not created
    return record, result, created


@router.post(
    "/jobs/score",
    response_model=JobScore,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
async def score_job(job: Job, db: Session = Depends(get_db)) -> JobScore:
    settings = get_settings()
    profile = get_or_create_default_profile(db)
    record, result, created = _score_and_persist(db, profile, job)

    logger.info(
        "job_scored job_id=%s score=%s recommendation=%s duplicate=%s",
        record.id,
        result.score,
        result.recommendation,
        result.is_duplicate,
    )

    if created and result.score >= settings.min_job_score_to_notify:
        notifier = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_retries=settings.telegram_max_retries,
        )
        await notifier.send_job(job, result)

    return result


def _to_list_item(record: JobRecord) -> JobListItem:
    return JobListItem(
        id=record.id,
        source=record.source,
        title=record.title,
        company=record.company,
        location=record.location,
        score=record.score,
        recommendation=record.recommendation,
        status=ApplicationStatus(record.status),
        last_seen_at=record.last_seen_at,
    )


def _to_detail(record: JobRecord) -> JobDetail:
    return JobDetail(
        id=record.id,
        fingerprint=record.fingerprint,
        source=record.source,
        title=record.title,
        company=record.company,
        location=record.location,
        url=record.url,
        description=record.description,
        skills=json.loads(record.skills_json),
        data_confidence=record.data_confidence,
        skill_source=record.skill_source,
        must_have_skills=json.loads(record.must_have_skills_json),
        nice_to_have_skills=json.loads(record.nice_to_have_skills_json),
        score=record.score,
        recommendation=record.recommendation,
        status=ApplicationStatus(record.status),
        first_seen_at=record.first_seen_at,
        last_seen_at=record.last_seen_at,
    )


@router.get(
    "/jobs",
    response_model=list[JobListItem],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_jobs(
    status: ApplicationStatus | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[JobListItem]:
    records = list_jobs(db, status=status, limit=limit, offset=offset)
    return [_to_list_item(record) for record in records]


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetail,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_job(job_id: int, db: Session = Depends(get_db)) -> JobDetail:
    record = get_job_by_id(db, job_id)
    if record is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _to_detail(record)


@router.patch(
    "/jobs/{job_id}/status",
    response_model=JobDetail,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def patch_job_status(
    job_id: int, body: StatusUpdateRequest, db: Session = Depends(get_db)
) -> JobDetail:
    try:
        record = update_job_status(db, job_id, body.status)
    except InvalidStatusTransitionError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if record is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    return _to_detail(record)


@router.get(
    "/candidate-profile",
    response_model=CandidateProfile,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_profile(db: Session = Depends(get_db)) -> CandidateProfile:
    """Stage 6A: the single canonical Candidate Profile — the factual
    authority future CV/Bewerbung generation must read candidate-side
    claims from (see app/db/candidate_profile_repository.py's module
    docstring). Always 200: the singleton is created empty on first access
    rather than 404ing before any PATCH has ever been sent.
    """
    record = get_or_create_candidate_profile(db)
    return to_candidate_profile_response(record)


@router.patch(
    "/candidate-profile",
    response_model=CandidateProfile,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def patch_candidate_profile(
    body: CandidateProfilePatchRequest, db: Session = Depends(get_db)
) -> CandidateProfile:
    """Partial update — see CandidateProfilePatchRequest's docstring for
    the exact semantics (omitted keys untouched; a present list field
    replaces that list wholesale). No PUT endpoint is exposed; see the
    same docstring for why.

    `body.expected_profile_version` is required (structurally enforced —
    422 if omitted) and must match the profile's current version (CP-M-03)
    — a stale value raises CandidateProfileVersionConflictError, mapped to
    409 here. The caller must GET the profile again and retry with the
    fresh version; the response detail never includes profile content.
    """
    try:
        record = apply_candidate_profile_patch(db, body)
    except CandidateProfileVersionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "current_profile_version": exc.current_version,
            },
        ) from exc
    return to_candidate_profile_response(record)


async def _run_company_research(
    db: Session, settings, job_id: int, *, force_refresh: bool
) -> CompanyResearchRunResponse | None:
    """Fetch (or reuse cached) company research for one job.

    Shared by POST /jobs/{id}/research and the Telegram control center's
    `/research <id>` command (app/services/telegram_bot.py) — see
    `_run_bundesagentur` below for the same rationale. Returns None if the
    job doesn't exist; callers translate that into their own presentation
    (404 vs. a chat message). Raises ProviderNotConfiguredError if the
    active provider needs configuration that isn't set,
    InvalidCompanyIdentityError if the job has no usable company name, or
    AmbiguousCompanyIdentityError (FR-M-01) if the job's normalized company
    name is shared by 2+ distinct known-domain companies on file — no other
    provider failure propagates here, see
    CompanyResearchService.get_or_run's failure-isolation contract and
    CompanyResearchRunResponse's refresh-outcome fields.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None
    return await CompanyResearchService().get_or_run(db, job, settings, force_refresh=force_refresh)


@router.post(
    "/jobs/{job_id}/research",
    response_model=CompanyResearchRunResponse,
    dependencies=[Depends(require_api_key), Depends(enforce_company_research_rate_limit)],
)
async def run_company_research(
    job_id: int,
    body: ResearchRequest = ResearchRequest(),
    db: Session = Depends(get_db),
) -> CompanyResearchRunResponse:
    settings = get_settings()
    try:
        result = await _run_company_research(db, settings, job_id, force_refresh=body.force_refresh)
    except ProviderNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except InvalidCompanyIdentityError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except AmbiguousCompanyIdentityError as exc:
        # FR-M-01: the input (job id) is itself valid, but the identity it
        # resolves to is ambiguous relative to current DB state — 409, not
        # 422/404. Never falls back to returning an arbitrary company.
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    if result.research is None and not result.refresh_succeeded:
        # Total failure: the provider failed and there was no prior good
        # record to fall back to — nothing usable exists, so this must not
        # look like a successful 200 (see CompanyResearchRunResponse
        # docstring). The FAILED row CompanyResearchService already
        # persisted stays available for diagnostics/retry via GET.
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=result.error or "Company research failed.",
        )
    return result


@router.get(
    "/jobs/{job_id}/research",
    response_model=CompanyResearchResponse,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_company_research(job_id: int, db: Session = Depends(get_db)) -> CompanyResearchResponse:
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    try:
        result = CompanyResearchService().get_cached(db, job)
    except AmbiguousCompanyIdentityError as exc:
        # FR-M-01: GET is a pure cache read and must never arbitrarily pick
        # one of several known-domain companies sharing this job's
        # normalized company name — same controlled 409 as POST.
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Company research not found for this job. POST /jobs/{id}/research to run it.",
        )
    return result


def _run_candidate_job_match(
    db: Session, job_id: int, *, force_recompute: bool
) -> CandidateJobMatch | None:
    """Compute (or reuse a cached) Candidate Profile <-> Job match analysis
    (Stage 6B). Returns None if the job doesn't exist — callers translate
    that into their own 404.

    Deliberately synchronous (unlike _run_company_research/_run_bundesagentur
    above): app.agents.candidate_job_matcher.compute_match performs zero
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


@router.post(
    "/jobs/{job_id}/match",
    response_model=CandidateJobMatch,
    dependencies=[Depends(require_api_key), Depends(enforce_match_rate_limit)],
)
def run_candidate_job_match(
    job_id: int,
    body: MatchRequest = MatchRequest(),
    db: Session = Depends(get_db),
) -> CandidateJobMatch:
    """Compute or reuse a deterministic Candidate Profile <-> Job match
    analysis. Always 200 for an existing job, even with a sparse/empty
    Candidate Profile (section 33) — a profile with no confirmed facts
    yields low/neutral sub-scores plus an explicit warning, never a
    failure merely because the profile is sparse.
    """
    result = _run_candidate_job_match(db, job_id, force_recompute=body.force_recompute)
    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/match",
    response_model=CandidateJobMatch,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_job_match(job_id: int, db: Session = Depends(get_db)) -> CandidateJobMatch:
    """Pure cache read — never computes (section 23). Returns the most
    recently computed analysis for this job, whatever candidate profile
    version and job content it was computed against; the response's own
    `candidate_profile_version` tells the caller whether it may be stale
    relative to the current profile.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_match(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No match analysis found for this job. POST /jobs/{id}/match to compute it.",
        )
    return to_candidate_job_match(record)


def _run_candidate_cv_draft(
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

    Deliberately synchronous, like _run_candidate_job_match: compute_match
    (0 network) has already run in Stage 6B, and compute_cv_draft performs
    zero I/O of its own (no LLM, no network — section 29/42), so nothing
    here needs to await.
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


@router.post(
    "/jobs/{job_id}/cv-draft",
    response_model=TailoredCVDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_cv_draft_rate_limit)],
)
def run_candidate_cv_draft(
    job_id: int,
    body: CVDraftRequest,
    db: Session = Depends(get_db),
) -> TailoredCVDraft:
    """Compute or reuse a deterministic Tailored CV Draft pinned to
    `body.match_id`. `match_id` is required (no "latest match" fallback —
    section 5). Always 200 for a valid, still-fresh match, even with a
    sparse/empty Candidate Profile (section 37) — a profile with few
    trusted facts yields a sparse draft plus explicit warnings, never a
    failure merely because the profile is sparse.
    """
    try:
        result = _run_candidate_cv_draft(
            db, job_id, body.match_id, force_recompute=body.force_recompute
        )
    except CVDraftMatchNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CVDraftMatchJobMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except CVDraftProfileChangedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "match_profile_version": exc.match_profile_version,
                "current_profile_version": exc.current_profile_version,
            },
        ) from exc
    except CVDraftJobChangedError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/cv-draft",
    response_model=TailoredCVDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_cv_draft_for_job(job_id: int, db: Session = Depends(get_db)) -> TailoredCVDraft:
    """Pure cache read — never computes (section 35). Returns the most
    recently created draft for this job, whatever match/profile
    version/job content it was generated against.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_draft(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No CV draft found for this job. POST /jobs/{id}/cv-draft to create one.",
        )
    return to_tailored_cv_draft(record)


@router.get(
    "/cv-drafts/{draft_id}",
    response_model=TailoredCVDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_cv_draft_by_id(draft_id: int, db: Session = Depends(get_db)) -> TailoredCVDraft:
    """Returns the exact immutable draft snapshot for `draft_id` (section
    35) — never recomputed, never mutated.
    """
    record = get_draft_by_id(db, draft_id)
    if record is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="CV draft not found")
    return to_tailored_cv_draft(record)


async def _run_bewerbung_draft(db: Session, job_id: int, cv_draft_id: int) -> BewerbungDraft | None:
    """Generate a Bewerbung draft pinned to one specific persisted CV draft
    (Stage 6D). Returns None if the job doesn't exist — callers translate
    that into their own 404. Raises BewerbungCVDraftNotFoundError /
    BewerbungCVDraftJobMismatchError / BewerbungProfileChangedError /
    BewerbungJobChangedError / BewerbungMatchNotFoundError /
    BewerbungMatchInconsistentError / BewerbungPlanRejectedError /
    BewerbungProviderError for every other failure — see those classes'
    docstrings in app/agents/bewerbung_generator.py,
    app/agents/bewerbung_renderer.py, and app/providers/bewerbung/base.py
    for the exact status-code mapping below.

    Unlike _run_candidate_job_match/_run_candidate_cv_draft, this is async:
    BewerbungService.generate calls out to a BewerbungProvider, which may
    (for a future non-deterministic provider) perform real I/O.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None

    service = BewerbungService()
    return await service.generate(db, job, cv_draft_id)


@router.post(
    "/jobs/{job_id}/bewerbung-draft",
    response_model=BewerbungDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_bewerbung_rate_limit)],
)
async def run_bewerbung_draft(
    job_id: int,
    body: BewerbungDraftRequest,
    db: Session = Depends(get_db),
) -> BewerbungDraft:
    """Generate a new Bewerbung draft pinned to `body.cv_draft_id`.
    `cv_draft_id` is required (no "latest CV draft" fallback — section 3).
    Every successful call creates a NEW immutable draft row (section 35) —
    never reused/cached, unlike POST .../match or .../cv-draft.
    """
    try:
        result = await _run_bewerbung_draft(db, job_id, body.cv_draft_id)
    except BewerbungCVDraftNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BewerbungCVDraftJobMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except BewerbungProfileChangedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "cv_draft_profile_version": exc.cv_draft_profile_version,
                "current_profile_version": exc.current_profile_version,
            },
        ) from exc
    except BewerbungJobChangedError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BewerbungMatchNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BewerbungMatchInconsistentError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BewerbungProviderNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except BewerbungProviderError as exc:
        raise HTTPException(status_code=http_status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except BewerbungPlanRejectedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(exc), "codes": exc.codes},
        ) from exc

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/bewerbung-draft",
    response_model=BewerbungDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_bewerbung_draft_for_job(job_id: int, db: Session = Depends(get_db)) -> BewerbungDraft:
    """Pure cache read — never generates. Returns the most recently
    created Bewerbung draft for this job, whatever CV draft/match/profile
    version/job content it was generated against.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_bewerbung_draft(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=(
                "No Bewerbung draft found for this job. "
                "POST /jobs/{id}/bewerbung-draft to create one."
            ),
        )
    return to_bewerbung_draft(record)


@router.get(
    "/bewerbung-drafts/{draft_id}",
    response_model=BewerbungDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_bewerbung_draft_snapshot(draft_id: int, db: Session = Depends(get_db)) -> BewerbungDraft:
    """Returns the exact immutable draft snapshot for `draft_id` — never
    regenerated, never mutated.
    """
    record = get_bewerbung_draft_by_id(db, draft_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Bewerbung draft not found"
        )
    return to_bewerbung_draft(record)


def _run_create_review_package(
    db: Session, job_id: int, cv_draft_id: int, bewerbung_draft_id: int
) -> ReviewPackage | None:
    """Create a Stage 6E review package pinned to one specific persisted
    CV draft and one specific persisted Bewerbung draft. Returns None if
    the job doesn't exist — callers translate that into their own 404.
    Deterministic, synchronous, zero I/O beyond the database (no provider
    call, no LLM — spec section 45).
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None
    return ReviewPackageService().create(db, job, cv_draft_id, bewerbung_draft_id)


@router.post(
    "/jobs/{job_id}/review-package",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def create_review_package(
    job_id: int,
    body: ReviewPackageCreateRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """Create a new PENDING_REVIEW package pinned to `body.cv_draft_id` +
    `body.bewerbung_draft_id` — both required, no "latest" fallback
    (section 4). Never auto-approves.
    """
    try:
        result = _run_create_review_package(db, job_id, body.cv_draft_id, body.bewerbung_draft_id)
    except ReviewCVDraftNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewBewerbungDraftNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewCVDraftJobMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewBewerbungDraftJobMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewSourceMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(exc), "mismatched_fields": exc.mismatched_fields},
        ) from exc
    except ReviewCurrentProfileMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewProfileChangedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "pinned_profile_version": exc.pinned_profile_version,
                "current_profile_version": exc.current_profile_version,
            },
        ) from exc
    except ReviewCurrentJobMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewJobChangedError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/review-package",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_review_package_for_job(job_id: int, db: Session = Depends(get_db)) -> ReviewPackage:
    """Pure read — never creates. Returns the most recently created
    review package for this job, whatever its status.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_review_for_job(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=(
                "No review package found for this job. "
                "POST /jobs/{id}/review-package to create one."
            ),
        )
    revision = get_current_revision(db, record)
    return to_review_package(record, revision)


@router.get(
    "/review-packages/{review_id}",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_review_package_by_id(review_id: int, db: Session = Depends(get_db)) -> ReviewPackage:
    """Pure read by exact id — never creates, never mutates."""
    record = get_review_by_id(db, review_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Review package not found"
        )
    revision = get_current_revision(db, record)
    return to_review_package(record, revision)


@router.patch(
    "/review-packages/{review_id}",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def patch_review_package(
    review_id: int,
    body: ReviewPackagePatchRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """Apply a human edit to the CV/Bewerbung review surface, creating a
    new immutable revision (section 21/27) and bumping `review_version`
    (section 11) — only while the review is still PENDING_REVIEW (section
    28). Never edits the pinned source `candidate_cv_drafts`/
    `bewerbung_drafts` rows.
    """
    try:
        result = ReviewPackageService().patch(
            db,
            review_id,
            body.expected_review_version,
            body.cv_changes,
            body.bewerbung_changes,
            body.edit_note,
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewNotPendingError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "current_status": exc.current_status},
        ) from exc
    except ReviewParagraphIndexError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewVersionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "expected_review_version": exc.expected_review_version,
                "current_review_version": exc.current_review_version,
            },
        ) from exc
    return result


@router.post(
    "/review-packages/{review_id}/approve",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def approve_review_package(
    review_id: int,
    body: ReviewPackageApproveRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """The only endpoint in this project that may transition a review
    package to APPROVED (section 2). Never sends anything, never mutates
    ApplicationStatus (section 3) — approval means only "the human
    approved this exact package".
    """
    try:
        result = ReviewPackageService().approve(
            db,
            review_id,
            body.expected_review_version,
            body.acknowledge_manual_overrides,
            body.decision_note,
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewNotPendingError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "current_status": exc.current_status},
        ) from exc
    except ReviewCurrentProfileMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewProfileChangedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "pinned_profile_version": exc.pinned_profile_version,
                "current_profile_version": exc.current_profile_version,
            },
        ) from exc
    except ReviewCurrentJobMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewJobChangedError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewManualOverrideAcknowledgmentRequiredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewVersionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "expected_review_version": exc.expected_review_version,
                "current_review_version": exc.current_review_version,
            },
        ) from exc
    return result


@router.post(
    "/review-packages/{review_id}/reject",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def reject_review_package(
    review_id: int,
    body: ReviewPackageRejectRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """PENDING_REVIEW -> REJECTED. Never mutates the source drafts, never
    touches ApplicationStatus."""
    try:
        result = ReviewPackageService().reject(
            db, review_id, body.expected_review_version, body.decision_note
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewNotPendingError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "current_status": exc.current_status},
        ) from exc
    except ReviewVersionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "expected_review_version": exc.expected_review_version,
                "current_review_version": exc.current_review_version,
            },
        ) from exc
    return result


@router.get(
    "/jobs/{job_id}/approved-package",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_approved_package_for_job(job_id: int, db: Session = Depends(get_db)) -> ReviewPackage:
    """Pure read — the future submission-stage handoff boundary (section
    31/44). Returns only an actually APPROVED review package/revision;
    never auto-approves, never falls back to PENDING_REVIEW or a raw
    latest CV/Bewerbung draft.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = get_approved_package(db, job)
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No approved review package found for this job.",
        )
    return result


async def _maybe_auto_research(
    db: Session,
    settings,
    record: JobRecord,
    result: JobScore,
    budget: dict[str, int],
) -> None:
    """Best-effort, opt-in company research for a just-persisted high-score job.

    Off by default (settings.company_research_auto_enabled) — see
    app/core/config.py. Shared by _run_bundesagentur/_run_xing so the
    "research automatically for APPLY-recommended jobs" rule lives in one
    place. Failures here must never affect a collector run's
    created/updated/failed counts, same best-effort contract as the
    Telegram notification block right below each call site.

    `budget` is a per-collector-run mutable counter
    (`{"remaining": settings.company_research_auto_max_per_run}`, created
    once by the caller before its loop starts) — bounds how many automatic
    research runs a single collector run can trigger regardless of how many
    APPLY jobs it produces, so a large batch can't silently fan out into an
    unbounded number of research runs. Manual triggers (POST
    /jobs/{id}/research, Telegram /research) are unaffected by this budget.
    """
    if not settings.company_research_auto_enabled or result.recommendation != "APPLY":
        return
    if budget["remaining"] <= 0:
        return
    budget["remaining"] -= 1
    try:
        await CompanyResearchService().get_or_run(db, record, settings)
    except Exception:
        logger.warning(
            "company_research_auto_run_failed job_id=%s company=%s",
            record.id,
            record.company,
            exc_info=True,
        )


async def _run_bundesagentur(db: Session, settings) -> dict[str, int]:
    """Fetch + score + persist one Bundesagentur collector run.

    Shared by POST /collectors/bundesagentur/run and the Telegram control
    center's `/run bundesagentur` command (app/services/telegram_bot.py) so
    this logic lives in exactly one place. Raises CollectorNotConfiguredError
    if BUNDESAGENTUR_API_KEY isn't set, or a CollectorError subclass if the
    upstream fetch ultimately fails — callers translate these into their own
    presentation (HTTP status code vs. a chat message).
    """
    if not is_api_key_configured(settings.bundesagentur_api_key):
        raise CollectorNotConfiguredError(
            "Bundesagentur collector is not configured: set BUNDESAGENTUR_API_KEY."
        )

    collector = BundesagenturCollector(
        api_key=settings.bundesagentur_api_key,
        keywords=settings.bundesagentur_search_keywords,
        location=settings.bundesagentur_search_location,
        radius_km=settings.bundesagentur_search_radius_km,
    )

    jobs = await collector.fetch()

    profile = get_or_create_default_profile(db)
    # One notifier per collector run (not per job): send_job() opens its own
    # httpx.AsyncClient per call, so this only avoids repeated construction
    # overhead, but it also keeps the flood-limit pacing below scoped to a
    # single run via one shared notified_count counter.
    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        max_retries=settings.telegram_max_retries,
    )
    created_count = 0
    updated_count = 0
    failed_count = 0
    notified_count = 0
    auto_research_budget = {"remaining": settings.company_research_auto_max_per_run}
    for job in jobs:
        try:
            existing = get_job_by_fingerprint(db, job)
            description = job.description
            if not description.strip() and existing is not None and existing.description.strip():
                # Search responses currently contain no description. A
                # persisted non-empty BA description therefore means detail
                # enrichment already succeeded on an earlier run. Reuse it
                # and re-run the deterministic extractor locally; requiring
                # saved skills too would repeatedly call detail for valid
                # non-technical descriptions where zero matches is expected.
                description = existing.description
                logger.debug(
                    "bundesagentur_detail_reused referenznummer=%s",
                    job.source_reference,
                )
            elif not description.strip() and job.source_reference:
                detail_description = await collector.fetch_detail(job.source_reference)
                if detail_description is not None:
                    description = detail_description
            elif not description.strip():
                logger.warning(
                    "bundesagentur_detail_skipped reason=missing_referenznummer url=%s",
                    job.url,
                )

            extraction = extract_skills(job.title, description)
            all_skills = sorted(
                set(job.skills)
                | set(extraction.must_have_skills)
                | set(extraction.nice_to_have_skills)
            )
            job = job.model_copy(
                update={
                    "description": description,
                    "skills": all_skills,
                    "must_have_skills": extraction.must_have_skills,
                    "nice_to_have_skills": extraction.nice_to_have_skills,
                    "skill_source": extraction.skill_source,
                }
            )
            if existing is not None and description.strip():
                # Stage the BA-only enrichment update in the same transaction
                # committed by upsert_job. If scoring/persistence fails, the
                # surrounding rollback also restores the previous description.
                existing.description = description
            job_record, result, created = _score_and_persist(db, profile, job)
        except Exception:
            # A failure scoring/persisting one job (JobScorer bug, DB
            # constraint violation, etc.) must not abort the whole run and
            # lose the jobs already committed before it. db.rollback() is
            # required here: SQLAlchemy leaves the Session unusable after a
            # failed flush/commit until it's rolled back, so without this
            # every job after the first failure would also fail.
            db.rollback()
            failed_count += 1
            logger.exception(
                "bundesagentur_collector_job_persist_failed title=%s company=%s url=%s",
                job.title,
                job.company,
                job.url,
            )
            continue

        if created:
            created_count += 1
        else:
            updated_count += 1

        await _maybe_auto_research(db, settings, job_record, result, auto_research_budget)

        if result.recommendation == "APPLY" and result.score >= settings.min_job_score_to_notify:
            # Notification delivery is best-effort orchestration on top of
            # already-committed persistence: a failed/slow send must not
            # affect created/updated/failed counts or abort the run.
            if notified_count > 0:
                await asyncio.sleep(1)
            try:
                sent = await notifier.send_job(job, result)
            except Exception:
                logger.warning(
                    "bundesagentur_notification_error title=%s company=%s",
                    job.title,
                    job.company,
                    exc_info=True,
                )
            else:
                if not sent:
                    logger.warning(
                        "bundesagentur_notification_failed title=%s company=%s",
                        job.title,
                        job.company,
                    )
            notified_count += 1

    logger.info(
        "bundesagentur_collector_run fetched=%s created=%s updated=%s skipped_invalid=%s failed=%s",
        len(jobs),
        created_count,
        updated_count,
        collector.skipped_invalid_count,
        failed_count,
    )

    return {
        "fetched": len(jobs),
        "created": created_count,
        "updated": updated_count,
        "skipped_invalid": collector.skipped_invalid_count,
        "failed": failed_count,
    }


@router.post(
    "/collectors/bundesagentur/run",
    dependencies=[Depends(require_api_key), Depends(enforce_collector_rate_limit)],
)
async def run_bundesagentur_collector(db: Session = Depends(get_db)) -> dict[str, int]:
    settings = get_settings()
    try:
        return await _run_bundesagentur(db, settings)
    except CollectorNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CollectorError as exc:
        logger.exception("bundesagentur_collector_run_failed")
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"Bundesagentur Jobsuche API request failed: {exc}",
        ) from exc


async def _run_xing(db: Session, settings) -> dict[str, int]:
    """Fetch + score + persist one XING mailbox collector run.

    Shared by POST /collectors/xing/run and the Telegram control center's
    `/run xing` command (app/services/telegram_bot.py) — see
    `_run_bundesagentur` above for the same rationale.
    """
    if not is_configured(settings.xing_mailbox_username) or not is_configured(
        settings.xing_mailbox_app_password
    ):
        raise CollectorNotConfiguredError(
            "XING mailbox collector is not configured: set "
            "XING_MAILBOX_USERNAME and XING_MAILBOX_APP_PASSWORD."
        )

    collector = XingEmailCollector(
        imap_host=settings.xing_mailbox_imap_host,
        imap_port=settings.xing_mailbox_imap_port,
        username=settings.xing_mailbox_username,
        app_password=settings.xing_mailbox_app_password,
        lookback_days=settings.xing_lookback_days,
        # Bound to this request's db.Session via closures rather than
        # passed as a constructor `db` param, so the collector itself stays
        # decoupled from SQLAlchemy — see XingEmailCollector's docstring.
        is_message_processed=lambda message_id: is_message_processed(db, "xing", message_id),
    )

    message_batches = await collector.fetch_message_batches()
    jobs = [job for batch in message_batches for job in batch.jobs]

    profile = get_or_create_default_profile(db)
    # One notifier for the whole run (all batches), so the flood-limit pacing
    # via notified_count below is scoped per collector run, not per message.
    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        max_retries=settings.telegram_max_retries,
    )
    created_count = 0
    updated_count = 0
    failed_count = 0
    notified_count = 0
    auto_research_budget = {"remaining": settings.company_research_auto_max_per_run}
    for batch in message_batches:
        batch_failed = False
        for job in batch.jobs:
            try:
                job_record, result, created = _score_and_persist(db, profile, job)
            except Exception:
                # One bad job must not abort the run, but its source message
                # must remain unacknowledged. A later run will parse the whole
                # message again; jobs already committed from this batch are
                # safely deduplicated by fingerprint. Reprocessing is preferred
                # to silently losing the failed job forever.
                db.rollback()
                batch_failed = True
                failed_count += 1
                logger.exception(
                    "xing_collector_job_persist_failed title=%s company=%s url=%s",
                    job.title,
                    job.company,
                    job.url,
                )
                continue

            if created:
                created_count += 1
            else:
                updated_count += 1

            await _maybe_auto_research(db, settings, job_record, result, auto_research_budget)

            if (
                result.recommendation == "APPLY"
                and result.score >= settings.min_job_score_to_notify
            ):
                # Same best-effort contract as _run_bundesagentur: notification
                # failures are orchestration on top of already-committed
                # persistence and must not affect counts or abort the run.
                if notified_count > 0:
                    await asyncio.sleep(1)
                try:
                    sent = await notifier.send_job(job, result)
                except Exception:
                    logger.warning(
                        "xing_notification_error title=%s company=%s",
                        job.title,
                        job.company,
                        exc_info=True,
                    )
                else:
                    if not sent:
                        logger.warning(
                            "xing_notification_failed title=%s company=%s",
                            job.title,
                            job.company,
                        )
                notified_count += 1

        if not batch_failed:
            mark_message_processed(db, "xing", batch.message_id)

    logger.info(
        "xing_collector_run fetched=%s created=%s updated=%s skipped_invalid=%s failed=%s",
        len(jobs),
        created_count,
        updated_count,
        collector.skipped_invalid_count,
        failed_count,
    )

    return {
        "fetched": len(jobs),
        "created": created_count,
        "updated": updated_count,
        "skipped_invalid": collector.skipped_invalid_count,
        "failed": failed_count,
    }


@router.post(
    "/collectors/xing/run",
    dependencies=[Depends(require_api_key), Depends(enforce_xing_rate_limit)],
)
async def run_xing_collector(db: Session = Depends(get_db)) -> dict[str, int]:
    settings = get_settings()
    try:
        return await _run_xing(db, settings)
    except CollectorNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CollectorError as exc:
        logger.exception("xing_collector_run_failed")
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"XING mailbox collector request failed: {exc}",
        ) from exc
