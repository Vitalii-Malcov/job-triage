import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.agents.job_scorer import JobScorer
from app.agents.skill_extractor import extract_skills
from app.collectors.base import CollectorError, CollectorNotConfiguredError, is_configured
from app.collectors.bundesagentur import BundesagenturCollector, is_api_key_configured
from app.collectors.xing_email import XingEmailCollector
from app.core.config import get_settings
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
from app.models.job import Job, JobDetail, JobListItem, JobScore, StatusUpdateRequest
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_collector_rate_limit,
    enforce_rate_limit,
    enforce_xing_rate_limit,
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
            _, result, created = _score_and_persist(db, profile, job)
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
    for batch in message_batches:
        batch_failed = False
        for job in batch.jobs:
            try:
                _, result, created = _score_and_persist(db, profile, job)
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
