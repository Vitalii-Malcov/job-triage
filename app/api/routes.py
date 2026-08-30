import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.agents.candidate_job_matcher import ALGORITHM_VERSION, JobMatchInput, compute_match
from app.agents.job_scorer import JobScorer
from app.agents.skill_extractor import extract_skills
from app.collectors.base import CollectorError, CollectorNotConfiguredError, is_configured
from app.collectors.bundesagentur import BundesagenturCollector, is_api_key_configured
from app.collectors.xing_email import XingEmailCollector
from app.core.config import get_settings
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    create_match,
    get_cached_match,
    get_latest_match,
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
from app.db.session import get_db
from app.domain.status_transitions import InvalidStatusTransitionError
from app.models.application_status import ApplicationStatus
from app.models.candidate_job_match import CandidateJobMatch, MatchRequest
from app.models.candidate_profile import CandidateProfile, CandidateProfilePatchRequest
from app.models.company_research import (
    CompanyResearchResponse,
    CompanyResearchRunResponse,
    ResearchRequest,
)
from app.models.job import Job, JobDetail, JobListItem, JobScore, StatusUpdateRequest
from app.providers.base import ProviderNotConfiguredError
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_collector_rate_limit,
    enforce_company_research_rate_limit,
    enforce_match_rate_limit,
    enforce_rate_limit,
    enforce_xing_rate_limit,
)
from app.services.company_research import (
    AmbiguousCompanyIdentityError,
    CompanyResearchService,
    InvalidCompanyIdentityError,
)
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
