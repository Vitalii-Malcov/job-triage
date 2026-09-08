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
from app.agents.cv_adapter import (
    CVDraftJobChangedError,
    CVDraftMatchJobMismatchError,
    CVDraftMatchNotFoundError,
    CVDraftProfileChangedError,
)
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
from app.collectors.base import is_configured
from app.core.config import get_settings
from app.db.automation_repository import (
    AUTOMATION_RUN_LIST_DEFAULT_LIMIT,
    AUTOMATION_RUN_LIST_MAX_LIMIT,
    get_run_by_id,
    list_runs,
    to_automation_run,
)
from app.db.bewerbung_repository import (
    get_bewerbung_draft_by_id,
    get_latest_bewerbung_draft,
    to_bewerbung_draft,
)
from app.db.candidate_cv_draft_repository import (
    get_draft_by_id,
    get_latest_draft,
    to_tailored_cv_draft,
)
from app.db.candidate_job_match_repository import get_latest_match, to_candidate_job_match
from app.db.candidate_profile_repository import (
    CandidateProfileVersionConflictError,
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
    to_candidate_profile_response,
)
from app.db.follow_up_approval_repository import to_follow_up_approval, to_follow_up_send_status
from app.db.follow_up_repository import (
    FOLLOW_UP_LIST_DEFAULT_LIMIT,
    FOLLOW_UP_LIST_MAX_LIMIT,
    get_follow_up_proposal_by_id,
    list_follow_up_proposals,
    to_follow_up_proposal,
)
from app.db.gmail_analysis_repository import (
    get_latest_analysis_for_message,
    list_analyses,
    to_gmail_message_analysis,
)
from app.db.gmail_repository import (
    THREAD_DETAIL_DEFAULT_MESSAGE_LIMIT,
    THREAD_DETAIL_MAX_MESSAGE_LIMIT,
    get_known_uids,
    get_message_by_id,
    get_thread_by_id,
    get_thread_message_count,
    list_messages,
    list_threads_with_counts,
    to_gmail_message,
    to_gmail_message_summary,
    to_gmail_thread,
    to_gmail_thread_detail,
)
from app.db.models import JobRecord
from app.db.repositories import (
    get_job_by_id,
    get_or_create_default_profile,
    list_jobs,
    update_job_status,
)
from app.db.response_draft_approval_repository import (
    to_response_draft_approval,
    to_response_draft_send_status,
)
from app.db.response_draft_repository import (
    RESPONSE_DRAFT_HISTORY_DEFAULT_LIMIT,
    RESPONSE_DRAFT_HISTORY_MAX_LIMIT,
    get_latest_response_draft_for_message,
    list_response_drafts_for_message,
    to_response_draft,
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
from app.models.automation import AutomationRun
from app.models.bewerbung import BewerbungDraft, BewerbungDraftRequest
from app.models.candidate_job_match import CandidateJobMatch, MatchRequest
from app.models.candidate_profile import CandidateProfile, CandidateProfilePatchRequest
from app.models.company_research import (
    CompanyResearchResponse,
    CompanyResearchRunResponse,
    ResearchRequest,
)
from app.models.cv_draft import CVDraftRequest, TailoredCVDraft
from app.models.follow_up import (
    FollowUpApproval,
    FollowUpApprovalRequest,
    FollowUpEvaluationResult,
    FollowUpProposal,
    FollowUpScanSummary,
    FollowUpSendStatus,
    FollowUpState,
)
from app.models.gmail import (
    GmailMessage,
    GmailMessageSummary,
    GmailSyncResult,
    GmailThread,
    GmailThreadDetail,
)
from app.models.gmail_analysis import GmailMessageAnalysis
from app.models.job import Job, JobDetail, JobListItem, JobScore, StatusUpdateRequest
from app.models.response_draft import ResponseDraft
from app.models.response_draft_approval import (
    ResponseDraftApproval,
    ResponseDraftApprovalRequest,
    ResponseDraftSendStatus,
    ResponseDraftState,
)
from app.models.review_package import (
    ReviewPackage,
    ReviewPackageApproveRequest,
    ReviewPackageCreateRequest,
    ReviewPackagePatchRequest,
    ReviewPackageRejectRequest,
)
from app.providers.base import ProviderNotConfiguredError
from app.providers.bewerbung.base import BewerbungProviderError, BewerbungProviderNotConfiguredError
from app.providers.email.base import GmailProviderError, normalize_account_key
from app.providers.email.imap import GmailImapProvider
from app.providers.email.smtp import GmailSmtpProvider
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_automation_run_rate_limit,
    enforce_bewerbung_rate_limit,
    enforce_collector_rate_limit,
    enforce_company_research_rate_limit,
    enforce_cv_draft_rate_limit,
    enforce_follow_up_decision_rate_limit,
    enforce_follow_up_evaluate_rate_limit,
    enforce_follow_up_send_rate_limit,
    enforce_gmail_analysis_rate_limit,
    enforce_gmail_rate_limit,
    enforce_match_rate_limit,
    enforce_rate_limit,
    enforce_response_draft_decision_rate_limit,
    enforce_response_draft_rate_limit,
    enforce_response_draft_send_rate_limit,
    enforce_review_write_rate_limit,
    enforce_xing_rate_limit,
)
from app.services.automation import (
    AutomationRunAlreadyInProgressError,
    AutomationRunLeaseLostError,
    run_automation_cycle,
)
from app.services.candidate_preparation import (
    prepare_bewerbung_draft as _run_bewerbung_draft,
)
from app.services.candidate_preparation import (
    prepare_candidate_cv_draft as _run_candidate_cv_draft,
)
from app.services.candidate_preparation import (
    prepare_candidate_job_match as _run_candidate_job_match,
)
from app.services.collector_runner import (
    CollectorError,
    CollectorNotConfiguredError,
    run_bundesagentur,
    run_company_research_for_job,
    run_xing,
    score_and_persist,
)
from app.services.company_research import (
    AmbiguousCompanyIdentityError,
    CompanyResearchService,
    InvalidCompanyIdentityError,
)
from app.services.follow_up import (
    FollowUpJobNotFoundError,
    evaluate_follow_up_for_job,
    list_due_follow_ups,
)
from app.services.follow_up_send import (
    FollowUpAlreadyDecidedError,
    FollowUpAlreadySentError,
    FollowUpMissingRecipientError,
    FollowUpNotApprovedError,
    FollowUpProposalNotFoundError,
    FollowUpProposalStaleAtSendTimeError,
    FollowUpSendFailedError,
    FollowUpSendInProgressError,
    FollowUpSendOutcomeUncertainError,
    approve_or_reject_follow_up,
    get_follow_up_state,
    send_follow_up,
)
from app.services.gmail_inbox import GmailInboxService
from app.services.gmail_message_analysis import GmailMessageNotFoundError, analyze_gmail_message
from app.services.response_draft import (
    ResponseDraftAnalysisNotFoundError,
    ResponseDraftMessageNotFoundError,
    generate_response_draft_for_message,
)
from app.services.response_draft_send import (
    ResponseDraftAlreadyDecidedError,
    ResponseDraftAlreadySentError,
    ResponseDraftMissingRecipientError,
    ResponseDraftNotApprovableError,
    ResponseDraftNotApprovedError,
    ResponseDraftNotFoundError,
    ResponseDraftSendFailedError,
    ResponseDraftSendInProgressError,
    ResponseDraftSendOutcomeUncertainError,
    approve_or_reject_response_draft,
    get_response_draft_state,
    send_response_draft,
)
from app.services.review_package import ReviewPackageService, get_approved_package
from app.services.telegram import TelegramNotifier

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

GMAIL_DEFAULT_LIST_LIMIT = 50
GMAIL_MAX_LIST_LIMIT = 200

GMAIL_ANALYSES_DEFAULT_LIST_LIMIT = 50
GMAIL_ANALYSES_MAX_LIST_LIMIT = 200

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/jobs/score",
    response_model=JobScore,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
async def score_job(job: Job, db: Session = Depends(get_db)) -> JobScore:
    settings = get_settings()
    profile = get_or_create_default_profile(db)
    record, result, created = score_and_persist(db, profile, job)

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
        result = await run_company_research_for_job(
            db, settings, job_id, force_refresh=body.force_refresh
        )
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


@router.post(
    "/collectors/bundesagentur/run",
    dependencies=[Depends(require_api_key), Depends(enforce_collector_rate_limit)],
)
async def run_bundesagentur_collector(db: Session = Depends(get_db)) -> dict[str, int]:
    """S8A-003: fetch + score + persist logic lives in
    `app.services.collector_runner.run_bundesagentur` — shared with the
    Telegram control center's `/run bundesagentur` command and Stage 8A's
    automation orchestrator. This endpoint only translates that
    function's exceptions into HTTP status codes.
    """
    settings = get_settings()
    try:
        return await run_bundesagentur(db, settings)
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


@router.post(
    "/collectors/xing/run",
    dependencies=[Depends(require_api_key), Depends(enforce_xing_rate_limit)],
)
async def run_xing_collector(db: Session = Depends(get_db)) -> dict[str, int]:
    """S8A-003: see `run_bundesagentur_collector` above — same split,
    `app.services.collector_runner.run_xing` owns the actual logic.
    """
    settings = get_settings()
    try:
        return await run_xing(db, settings)
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


@router.post(
    "/automation/runs",
    response_model=AutomationRun,
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key), Depends(enforce_automation_run_rate_limit)],
)
async def start_automation_run(db: Session = Depends(get_db)) -> AutomationRun:
    """Stage 8A: start exactly one synchronous, manually-triggered
    job-search cycle — coordinates the EXISTING Bundesagentur + XING
    collector runs (see app.services.automation's module docstring for
    why no new collection/scoring/dedup logic exists here). Awaits the
    full cycle before responding; no background task, no scheduler.

    Fails closed (409) if a run for this account is already RUNNING —
    see `AutomationRunRecord`'s docstring for the DB-enforced partial
    unique index this relies on.
    """
    settings = get_settings()
    account_key = _current_gmail_account_key(settings)
    try:
        run = await run_automation_cycle(db, account_key=account_key, settings=settings)
    except AutomationRunAlreadyInProgressError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AutomationRunLeaseLostError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return to_automation_run(run)


@router.get(
    "/automation/runs/{run_id}",
    response_model=AutomationRun,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_automation_run(run_id: int, db: Session = Depends(get_db)) -> AutomationRun:
    """Pure read of an already-persisted run — never starts, retries, or
    otherwise mutates one (mirrors GET /follow-ups/{id} not triggering
    evaluation)."""
    account_key = _current_gmail_account_key(get_settings())
    record = get_run_by_id(db, account_key, run_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Automation run not found"
        )
    return to_automation_run(record)


@router.get(
    "/automation/runs",
    response_model=list[AutomationRun],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def list_automation_runs(
    limit: int = Query(
        default=AUTOMATION_RUN_LIST_DEFAULT_LIMIT, ge=1, le=AUTOMATION_RUN_LIST_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[AutomationRun]:
    account_key = _current_gmail_account_key(get_settings())
    records = list_runs(db, account_key, limit=limit, offset=offset)
    return [to_automation_run(record) for record in records]


def _sum_gmail_sync_results(a: GmailSyncResult, b: GmailSyncResult) -> GmailSyncResult:
    return GmailSyncResult(
        fetched=a.fetched + b.fetched,
        created=a.created + b.created,
        duplicates=a.duplicates + b.duplicates,
        skipped=a.skipped + b.skipped,
        failed=a.failed + b.failed,
    )


async def _run_gmail_sync(db: Session, settings) -> GmailSyncResult:
    """Fetch (read-only IMAP) + persist one Gmail Inbox Foundation sync run.

    The configuration check lives here (not inside GmailInboxService),
    mirroring app.services.collector_runner.run_xing/run_bundesagentur's
    own split between "not configured" (503, see run_gmail_sync below)
    and "upstream/provider failure" (502) — GmailInboxService itself
    never fails closed on
    missing credentials, it just orchestrates fetch+persist for an
    already-constructed provider.

    S7E-001 (Codex remediation, HIGH): syncs BOTH the primary mailbox
    (gmail_mailbox, INBOUND — `trusted_outbound=False`) and the real
    Sent-mail folder (gmail_sent_mailbox, `trusted_outbound=True`) every
    run. Only messages fetched from the Sent folder are ever trusted as
    OUTBOUND — see app/providers/email/imap.py's `_direction` docstring for
    why a message's own `From` header is no longer used to decide
    direction. Both mailboxes share the same account_key/dedup namespace
    (their (mailbox, uid_validity, uid) identities are independent, so no
    collision risk), and one mailbox's sync failure/persistence errors
    never block the other's.
    """
    if not is_configured(settings.gmail_username) or not is_configured(settings.gmail_app_password):
        raise CollectorNotConfiguredError(
            "Gmail inbox sync is not configured: set GMAIL_USERNAME and GMAIL_APP_PASSWORD."
        )

    account_key = normalize_account_key(settings.gmail_username)

    def _make_provider(mailbox: str, *, trusted_outbound: bool) -> GmailImapProvider:
        return GmailImapProvider(
            imap_host=settings.gmail_imap_host,
            imap_port=settings.gmail_imap_port,
            username=settings.gmail_username,
            app_password=settings.gmail_app_password,
            mailbox=mailbox,
            lookback_days=settings.gmail_lookback_days,
            # GMAIL-005 starvation fix (GMAIL-012: bulk, not per-UID): bound
            # to this request's db.Session via closure — lets the provider
            # skip already-persisted UIDs before applying its
            # MAX_MESSAGES_PER_SYNC cap, in one query per chunk rather than
            # one query per UID.
            get_known_uids=lambda uid_validity, candidate_uids, _mailbox=mailbox: get_known_uids(
                db, account_key, _mailbox, uid_validity, candidate_uids
            ),
            trusted_outbound=trusted_outbound,
        )

    inbox_result = await GmailInboxService().sync(
        db, _make_provider(settings.gmail_mailbox, trusted_outbound=False)
    )
    sent_result = await GmailInboxService().sync(
        db, _make_provider(settings.gmail_sent_mailbox, trusted_outbound=True)
    )
    return _sum_gmail_sync_results(inbox_result, sent_result)


@router.post(
    "/gmail/sync",
    response_model=GmailSyncResult,
    dependencies=[Depends(require_api_key), Depends(enforce_gmail_rate_limit)],
)
async def run_gmail_sync(db: Session = Depends(get_db)) -> GmailSyncResult:
    """Read-only IMAP fetch + idempotent persist. Never sends, replies,
    drafts, marks read, deletes, or otherwise mutates the mailbox; never
    changes Job/ApplicationStatus; never invokes an LLM or classifier
    (Stage 7A scope — see CLAUDE.md).
    """
    settings = get_settings()
    try:
        return await _run_gmail_sync(db, settings)
    except CollectorNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except GmailProviderError as exc:
        # GMAIL-003: never interpolate the caught exception into the HTTP
        # response or into the log line — a provider/persistence error's
        # own message text could otherwise carry back a server-echoed
        # mailbox address, hostname, or (via a driver-level error further
        # down the stack) message content. Log only the exception's type;
        # respond with a fixed, generic detail string.
        logger.warning("gmail_sync_run_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Gmail inbox sync failed",
        ) from exc


def _current_gmail_account_key(settings) -> str:
    """The account_key every Gmail read endpoint scopes itself to
    (GMAIL-002) — the currently configured GMAIL_USERNAME, normalized.
    Read endpoints never require Gmail to be configured to respond (an
    empty account_key simply matches no persisted rows, which is a safe,
    fail-closed default), but scoping every read by it means a later
    GMAIL_USERNAME change can never leak a previous account's persisted
    correspondence through the read API.
    """
    return normalize_account_key(settings.gmail_username)


@router.get(
    "/gmail/messages",
    response_model=list[GmailMessageSummary],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_messages(
    limit: int = Query(default=GMAIL_DEFAULT_LIST_LIMIT, ge=1, le=GMAIL_MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[GmailMessageSummary]:
    """Pure read of already-synced messages, in compact summary form
    (GMAIL-007) — never body_plain/full recipients/references; see
    GET /gmail/messages/{id} for full detail. Never triggers a sync
    itself — POST /gmail/sync is the only endpoint that reads the
    mailbox.
    """
    account_key = _current_gmail_account_key(get_settings())
    records = list_messages(db, account_key, limit=limit, offset=offset)
    return [to_gmail_message_summary(record) for record in records]


@router.get(
    "/gmail/messages/{message_id}",
    response_model=GmailMessage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_message(message_id: int, db: Session = Depends(get_db)) -> GmailMessage:
    account_key = _current_gmail_account_key(get_settings())
    record = get_message_by_id(db, account_key, message_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message not found"
        )
    return to_gmail_message(record)


@router.get(
    "/gmail/threads",
    response_model=list[GmailThread],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_threads(
    limit: int = Query(default=GMAIL_DEFAULT_LIST_LIMIT, ge=1, le=GMAIL_MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[GmailThread]:
    account_key = _current_gmail_account_key(get_settings())
    # GMAIL-008: one grouped query for the whole page, never a per-thread
    # COUNT query.
    pairs = list_threads_with_counts(db, account_key, limit=limit, offset=offset)
    return [to_gmail_thread(record, count) for record, count in pairs]


@router.get(
    "/gmail/threads/{thread_id}",
    response_model=GmailThreadDetail,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_thread(
    thread_id: int,
    message_limit: int = Query(
        default=THREAD_DETAIL_DEFAULT_MESSAGE_LIMIT, ge=1, le=THREAD_DETAIL_MAX_MESSAGE_LIMIT
    ),
    db: Session = Depends(get_db),
) -> GmailThreadDetail:
    """Thread header plus a bounded, chronologically-ordered list of its
    messages in summary form (section 13 "thread detail API readiness") —
    a Stage 7B consumer never needs to devise its own unbounded query to
    read one thread's correspondence context.
    """
    account_key = _current_gmail_account_key(get_settings())
    record = get_thread_by_id(db, account_key, thread_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail thread not found"
        )
    message_count = get_thread_message_count(db, account_key, record.id)
    return to_gmail_thread_detail(db, record, message_count, message_limit=message_limit)


@router.post(
    "/gmail/messages/{message_id}/analyze",
    response_model=GmailMessageAnalysis,
    dependencies=[Depends(require_api_key), Depends(enforce_gmail_analysis_rate_limit)],
)
def run_gmail_message_analysis(
    message_id: int, db: Session = Depends(get_db)
) -> GmailMessageAnalysis:
    """Deterministic, evidence-based job/application matching +
    correspondence classification for one already-persisted Gmail
    message (Stage 7B). INFORMATION ONLY — see
    app/services/gmail_message_analysis.py's module docstring for the
    full hard boundary (no send/draft/reply, no mailbox mutation, no
    ApplicationStatus mutation, no email-derived HTTP, no LLM/external
    call). Idempotent: re-analyzing the same message under the same
    algorithm version and unchanged content returns the existing
    revision rather than creating a duplicate.
    """
    account_key = _current_gmail_account_key(get_settings())
    try:
        record, _created = analyze_gmail_message(db, account_key, message_id)
    except GmailMessageNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message not found"
        ) from exc
    except Exception as exc:
        # Mirrors GMAIL-003 (POST /gmail/sync): never interpolate a
        # caught exception into the HTTP response or the log line — a
        # driver-level DB error's own message text could in principle
        # embed row content (subject/body/addresses). Log only the
        # exception's type; respond with a fixed, generic detail string.
        db.rollback()
        logger.warning("gmail_message_analysis_run_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gmail message analysis failed",
        ) from exc
    return to_gmail_message_analysis(record)


@router.get(
    "/gmail/messages/{message_id}/analysis",
    response_model=GmailMessageAnalysis,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_message_analysis(
    message_id: int, db: Session = Depends(get_db)
) -> GmailMessageAnalysis:
    """The latest analysis revision for one message, if it has already
    been analyzed — pure read, never triggers analysis itself (mirrors
    GET /gmail/messages not triggering a sync — see
    POST /gmail/messages/{id}/analyze for that).
    """
    account_key = _current_gmail_account_key(get_settings())
    # Also confirms the message itself exists and belongs to this
    # account before reporting "no analysis found" vs. "message not
    # found" — two distinct 404 reasons.
    message = get_message_by_id(db, account_key, message_id)
    if message is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message not found"
        )
    record = get_latest_analysis_for_message(db, account_key, message_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message has not been analyzed"
        )
    return to_gmail_message_analysis(record)


@router.get(
    "/gmail/analyses",
    response_model=list[GmailMessageAnalysis],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_analyses(
    limit: int = Query(
        default=GMAIL_ANALYSES_DEFAULT_LIST_LIMIT, ge=1, le=GMAIL_ANALYSES_MAX_LIST_LIMIT
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[GmailMessageAnalysis]:
    """Bounded, most-recent-first list of persisted analysis revisions
    for the configured mailbox account. Each row is one immutable
    revision (not deduplicated to "latest per message") — a message
    re-analyzed under a new ANALYSIS_VERSION appears here more than once.
    """
    account_key = _current_gmail_account_key(get_settings())
    records = list_analyses(db, account_key, limit=limit, offset=offset)
    return [to_gmail_message_analysis(record) for record in records]


@router.post(
    "/gmail/messages/{message_id}/response-draft",
    response_model=ResponseDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_response_draft_rate_limit)],
)
def create_gmail_message_response_draft(
    message_id: int, db: Session = Depends(get_db)
) -> ResponseDraft:
    """Generate (or idempotently re-fetch) a Stage 7C response-draft
    PROPOSAL for an already-analyzed Gmail message. INFORMATION ONLY —
    see app/services/response_draft.py's module docstring for the full
    hard boundary (no send/Gmail-draft-creation/reply, no mailbox
    mutation, no ApplicationStatus mutation, no LLM/external call).
    `requires_human_review` is always `True` in the response — this
    endpoint only ever proposes text for a human to review and send
    manually. Requires POST /gmail/messages/{id}/analyze to have already
    run for this message (409 otherwise) — 7C reuses 7B's analysis, it
    never re-derives matching/classification itself.
    """
    account_key = _current_gmail_account_key(get_settings())
    try:
        record, _created = generate_response_draft_for_message(db, account_key, message_id)
    except ResponseDraftMessageNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message not found"
        ) from exc
    except ResponseDraftAnalysisNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Gmail message has not been analyzed yet",
        ) from exc
    except Exception as exc:
        # Mirrors POST /gmail/messages/{id}/analyze: never interpolate a
        # caught exception into the HTTP response or the log line.
        db.rollback()
        logger.warning("response_draft_generation_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Response draft generation failed",
        ) from exc
    return to_response_draft(record)


@router.get(
    "/gmail/messages/{message_id}/response-draft",
    response_model=ResponseDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_message_response_draft(
    message_id: int, db: Session = Depends(get_db)
) -> ResponseDraft:
    """The latest response-draft revision for one message, if one has
    already been generated — pure read, never triggers generation itself
    (mirrors GET /gmail/messages/{id}/analysis not triggering analysis).
    """
    account_key = _current_gmail_account_key(get_settings())
    message = get_message_by_id(db, account_key, message_id)
    if message is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message not found"
        )
    record = get_latest_response_draft_for_message(db, account_key, message_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Gmail message has no response draft",
        )
    return to_response_draft(record)


@router.get(
    "/gmail/messages/{message_id}/response-drafts",
    response_model=list[ResponseDraft],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_message_response_draft_history(
    message_id: int,
    limit: int = Query(
        default=RESPONSE_DRAFT_HISTORY_DEFAULT_LIMIT, ge=1, le=RESPONSE_DRAFT_HISTORY_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ResponseDraft]:
    """The FULL, bounded, most-recent-first history of response-draft
    revisions for one message — each row is one immutable revision, not
    deduplicated to "latest" (see GET .../response-draft for that).
    """
    account_key = _current_gmail_account_key(get_settings())
    message = get_message_by_id(db, account_key, message_id)
    if message is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message not found"
        )
    records = list_response_drafts_for_message(db, account_key, message_id, limit, offset)
    return [to_response_draft(record) for record in records]


@router.post(
    "/response-drafts/{draft_id}/decision",
    response_model=ResponseDraftApproval,
    dependencies=[Depends(require_api_key), Depends(enforce_response_draft_decision_rate_limit)],
)
def decide_response_draft(
    draft_id: int, body: ResponseDraftApprovalRequest, db: Session = Depends(get_db)
) -> ResponseDraftApproval:
    """Record one immutable APPROVE/REJECT human decision on an exact
    Stage 7C response-draft revision (Stage 7D). Pins the exact
    `subject`/`body` being authorized at decision time. A decision is
    permanent: approving/rejecting an already-decided revision fails
    (409) rather than overwriting it — generate a new draft revision to
    reconsider. This endpoint NEVER sends anything — see
    POST /response-drafts/{draft_id}/send for the only endpoint that
    does, and only once this decision is APPROVED.
    """
    account_key = _current_gmail_account_key(get_settings())
    try:
        record = approve_or_reject_response_draft(
            db, account_key, draft_id, body.decision, body.note
        )
    except ResponseDraftNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Response draft not found"
        ) from exc
    except ResponseDraftNotApprovableError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Response draft has no content to approve or reject",
        ) from exc
    except ResponseDraftAlreadyDecidedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Response draft already has a recorded decision",
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.warning("response_draft_decision_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Response draft decision failed",
        ) from exc
    return to_response_draft_approval(record)


@router.post(
    "/response-drafts/{draft_id}/send",
    response_model=ResponseDraftSendStatus,
    dependencies=[Depends(require_api_key), Depends(enforce_response_draft_send_rate_limit)],
)
def send_response_draft_endpoint(
    draft_id: int, db: Session = Depends(get_db)
) -> ResponseDraftSendStatus:
    """Send an APPROVED Stage 7C response draft as a real Gmail reply
    (Stage 7D) — the ONLY endpoint in this project that transmits
    outbound email. NO APPROVAL = NO SEND: see
    app/services/response_draft_send.py's module docstring for the exact
    gate this enforces (existence + account ownership, PROPOSED status,
    an APPROVED decision pinned to this exact revision, and that the
    approval has not already been consumed by a prior send). Safe against
    duplicate/concurrent/retried requests — never sends the same approval
    twice; a prior provider failure may be retried.
    """
    settings = get_settings()
    if not is_configured(settings.gmail_username) or not is_configured(settings.gmail_app_password):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outbound email sending is not configured",
        )

    account_key = _current_gmail_account_key(settings)
    provider = GmailSmtpProvider(
        smtp_host=settings.gmail_smtp_host,
        smtp_port=settings.gmail_smtp_port,
        username=settings.gmail_username,
        app_password=settings.gmail_app_password,
    )
    try:
        record = send_response_draft(db, account_key, draft_id, provider)
    except ResponseDraftNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Response draft not found"
        ) from exc
    except ResponseDraftNotApprovableError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Response draft is not sendable",
        ) from exc
    except ResponseDraftNotApprovedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Response draft has not been approved",
        ) from exc
    except ResponseDraftMissingRecipientError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No recipient address is available for this message",
        ) from exc
    except ResponseDraftAlreadySentError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Response draft has already been sent",
        ) from exc
    except ResponseDraftSendInProgressError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="A send attempt for this response draft is already in progress",
        ) from exc
    except ResponseDraftSendFailedError as exc:
        # ResponseDraftSendFailedError already carries no upstream
        # exception text (see EmailSendError's docstring) — log only the
        # type, same GMAIL-003-style discipline as every other Gmail
        # error path in this file. Only ever raised for a DEFINITE
        # pre-transmission failure — see ResponseDraftSendOutcomeUncertainError
        # for the separate, never-auto-retried ambiguous-outcome case.
        logger.warning("response_draft_send_endpoint_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Sending the response draft failed",
        ) from exc
    except ResponseDraftSendOutcomeUncertainError as exc:
        # Delivery could be neither confirmed nor ruled out — fail
        # closed. This is NEVER auto-retried; a later POST here is
        # refused before the provider is called again (see
        # app.services.response_draft_send's module docstring).
        logger.warning("response_draft_send_endpoint_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "Response draft send outcome is uncertain; manual reconciliation is "
                "required, not an automatic retry"
            ),
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.warning("response_draft_send_endpoint_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Response draft send failed",
        ) from exc
    return to_response_draft_send_status(record)


@router.get(
    "/response-drafts/{draft_id}/state",
    response_model=ResponseDraftState,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_response_draft_state_endpoint(
    draft_id: int, db: Session = Depends(get_db)
) -> ResponseDraftState:
    """Pure read of the combined approval/send state for one exact
    response-draft revision — never triggers a decision or a send.
    """
    account_key = _current_gmail_account_key(get_settings())
    try:
        return get_response_draft_state(db, account_key, draft_id)
    except ResponseDraftNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Response draft not found"
        ) from exc


@router.post(
    "/follow-ups/evaluate",
    response_model=FollowUpScanSummary,
    dependencies=[Depends(require_api_key), Depends(enforce_follow_up_evaluate_rate_limit)],
)
def evaluate_follow_ups(
    after_job_id: int | None = Query(
        default=None,
        ge=1,
        description=(
            "S7E-006: resume a keyset-paginated scan after this job id "
            "(pass back a prior call's next_cursor); omit to start from "
            "the oldest tracked APPLIED job."
        ),
    ),
    db: Session = Depends(get_db),
) -> FollowUpScanSummary:
    """Stage 7E: the only way follow-up eligibility is ever evaluated —
    there is no background scheduler/cron (spec requirement), so this
    must be triggered manually. Runs one bounded, KEYSET-paginated scan
    across tracked APPLIED jobs (see app.services.follow_up.FOLLOW_UP_JOB_SCAN_LIMIT),
    persisting a new `FollowUpProposalRecord` for every newly-eligible
    correspondence anchor. Idempotent: re-running this with the same
    `after_job_id` never duplicates a proposal for an anchor whose trusted
    inputs are unchanged. A backlog larger than one scan's limit is
    drained by repeatedly passing the response's `next_cursor` back in as
    `after_job_id` — see app.services.follow_up.list_due_follow_ups.
    """
    account_key = _current_gmail_account_key(get_settings())
    return list_due_follow_ups(db, account_key, after_job_id=after_job_id)


@router.post(
    "/jobs/{job_id}/follow-up/evaluate",
    response_model=FollowUpEvaluationResult,
    dependencies=[Depends(require_api_key), Depends(enforce_follow_up_evaluate_rate_limit)],
)
def evaluate_follow_up_for_single_job(
    job_id: int, db: Session = Depends(get_db)
) -> FollowUpEvaluationResult:
    """Evaluate follow-up eligibility for exactly one job — the
    single-job counterpart to `POST /follow-ups/evaluate`'s bounded scan.
    """
    account_key = _current_gmail_account_key(get_settings())
    try:
        return evaluate_follow_up_for_job(db, account_key, job_id)
    except FollowUpJobNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found"
        ) from exc


@router.get(
    "/follow-ups",
    response_model=list[FollowUpProposal],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_follow_ups(
    limit: int = Query(default=FOLLOW_UP_LIST_DEFAULT_LIMIT, ge=1, le=FOLLOW_UP_LIST_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[FollowUpProposal]:
    """Pure read of already-persisted follow-up proposals — never
    triggers evaluation itself (mirrors GET /gmail/messages not
    triggering a sync — see POST /follow-ups/evaluate for that).
    """
    account_key = _current_gmail_account_key(get_settings())
    records = list_follow_up_proposals(db, account_key, limit=limit, offset=offset)
    return [to_follow_up_proposal(record) for record in records]


@router.get(
    "/follow-ups/{follow_up_id}",
    response_model=FollowUpProposal,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_follow_up(follow_up_id: int, db: Session = Depends(get_db)) -> FollowUpProposal:
    account_key = _current_gmail_account_key(get_settings())
    record = get_follow_up_proposal_by_id(db, account_key, follow_up_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Follow-up proposal not found"
        )
    return to_follow_up_proposal(record)


@router.post(
    "/follow-ups/{follow_up_id}/decision",
    response_model=FollowUpApproval,
    dependencies=[Depends(require_api_key), Depends(enforce_follow_up_decision_rate_limit)],
)
def decide_follow_up(
    follow_up_id: int, body: FollowUpApprovalRequest, db: Session = Depends(get_db)
) -> FollowUpApproval:
    """Record one immutable APPROVE/REJECT human decision on an exact
    Stage 7E follow-up proposal. A decision is permanent: approving/
    rejecting an already-decided proposal fails (409). This endpoint
    NEVER sends anything — see POST /follow-ups/{id}/send for the only
    endpoint that does, and only once this decision is APPROVED.
    """
    account_key = _current_gmail_account_key(get_settings())
    try:
        record = approve_or_reject_follow_up(
            db, account_key, follow_up_id, body.decision, body.note
        )
    except FollowUpProposalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Follow-up proposal not found"
        ) from exc
    except FollowUpAlreadyDecidedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Follow-up proposal already has a recorded decision",
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.warning("follow_up_decision_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Follow-up decision failed",
        ) from exc
    return to_follow_up_approval(record)


@router.post(
    "/follow-ups/{follow_up_id}/send",
    response_model=FollowUpSendStatus,
    dependencies=[Depends(require_api_key), Depends(enforce_follow_up_send_rate_limit)],
)
def send_follow_up_endpoint(follow_up_id: int, db: Session = Depends(get_db)) -> FollowUpSendStatus:
    """Send an APPROVED Stage 7E follow-up as a real Gmail message — the
    only endpoint in this project (besides POST /response-drafts/{id}/send)
    that transmits outbound email. NO APPROVAL = NO FOLLOW-UP SEND — see
    app/services/follow_up_send.py's module docstring for the exact gate.
    """
    settings = get_settings()
    if not is_configured(settings.gmail_username) or not is_configured(settings.gmail_app_password):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outbound email sending is not configured",
        )

    account_key = _current_gmail_account_key(settings)
    provider = GmailSmtpProvider(
        smtp_host=settings.gmail_smtp_host,
        smtp_port=settings.gmail_smtp_port,
        username=settings.gmail_username,
        app_password=settings.gmail_app_password,
    )
    try:
        record = send_follow_up(db, account_key, follow_up_id, provider)
    except FollowUpProposalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Follow-up proposal not found"
        ) from exc
    except FollowUpNotApprovedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Follow-up proposal has not been approved",
        ) from exc
    except FollowUpMissingRecipientError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No recipient address is available for this follow-up",
        ) from exc
    except FollowUpAlreadySentError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Follow-up proposal has already been sent",
        ) from exc
    except FollowUpSendInProgressError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="A send attempt for this follow-up proposal is already in progress",
        ) from exc
    except FollowUpProposalStaleAtSendTimeError as exc:
        logger.warning("follow_up_send_endpoint_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This follow-up proposal is no longer eligible to send",
        ) from exc
    except FollowUpSendFailedError as exc:
        logger.warning("follow_up_send_endpoint_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Sending the follow-up failed",
        ) from exc
    except FollowUpSendOutcomeUncertainError as exc:
        logger.warning("follow_up_send_endpoint_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "Follow-up send outcome is uncertain; manual reconciliation is "
                "required, not an automatic retry"
            ),
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.warning("follow_up_send_endpoint_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Follow-up send failed",
        ) from exc
    return to_follow_up_send_status(record)


@router.get(
    "/follow-ups/{follow_up_id}/state",
    response_model=FollowUpState,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_follow_up_state_endpoint(follow_up_id: int, db: Session = Depends(get_db)) -> FollowUpState:
    """Pure read of the combined approval/send state for one follow-up
    proposal — never triggers a decision or a send.
    """
    account_key = _current_gmail_account_key(get_settings())
    try:
        return get_follow_up_state(db, account_key, follow_up_id)
    except FollowUpProposalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Follow-up proposal not found"
        ) from exc
