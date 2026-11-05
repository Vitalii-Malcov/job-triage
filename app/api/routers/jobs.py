"""HTTP routes: jobs (included by `app.api.routes`)."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers import common
from app.api.routers.common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, MAX_OFFSET
from app.core.config import reveal
from app.db.candidate_profile_repository import (
    get_candidate_skills_for_scoring,
)
from app.db.models import JobRecord
from app.db.repositories import (
    get_job_by_id,
    list_jobs,
    update_job_status,
)
from app.db.session import get_db
from app.domain.status_transitions import InvalidStatusTransitionError
from app.models.application_status import ApplicationStatus
from app.models.job import Job, JobDetail, JobListItem, JobScore, StatusUpdateRequest
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_rate_limit,
)
from app.services.collector_runner import (
    score_and_persist,
)
from app.services.offload import run_off_loop
from app.services.telegram import TelegramNotifier

logger = logging.getLogger("app.api.routes")
router = APIRouter()


@router.post(
    "/jobs/score",
    response_model=JobScore,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
async def score_job(job: Job, db: Session = Depends(get_db)) -> JobScore:
    # Review 2.4: synchronous scoring/persistence runs off the web loop.
    return await run_off_loop(lambda: _score_job_service(job, db))


async def _score_job_service(job: Job, db: Session) -> JobScore:
    settings = common.get_settings()
    # CandidateProfile is the sole runtime source of candidate
    # skills for JobScorer -- see get_candidate_skills_for_scoring's own
    # docstring. Never the legacy UserProfile table.
    candidate_skills = get_candidate_skills_for_scoring(db)
    record, result, created = score_and_persist(db, candidate_skills, job)

    logger.info(
        "job_scored job_id=%s score=%s recommendation=%s duplicate=%s",
        record.id,
        result.score,
        result.recommendation,
        result.is_duplicate,
    )

    # Stage 11E (and Stage
    # 11A/11B before it) can deliberately leave a job at a HIGH score
    # with recommendation="SKIP" -- e.g. a low-unique-evidence-cardinality
    # single-signal match, or a senior/irrelevant title excluded after
    # scoring. A score-only condition here would still fire a Telegram
    # notification for exactly those Stage-11E-rejected jobs. Must
    # require recommendation=="APPLY" in addition to the score threshold
    # -- the same recommendation+score semantics already used by every
    # collector run's own notification gate (see
    # app.services.collector_runner.run_bundesagentur/run_xing:
    # `result.recommendation == "APPLY" and result.score >= settings.
    # min_job_score_to_notify`), never scored/gated as an independent,
    # duplicate condition.
    if (
        created
        and result.recommendation == "APPLY"
        and result.score >= settings.min_job_score_to_notify
    ):
        # Codex gate follow-up (Astra R4B, NEW-005: Telegram isolation):
        # `record`/`result` above are ALREADY durably committed by
        # `score_and_persist` -- notification is best-effort orchestration
        # on top of that, exactly like every collector run's own
        # send_job() call (see app.services.collector_runner.run_xing/
        # run_bundesagentur's identical try/except). This was the one
        # send_job() call site NOT wrapped: an uncaught exception here
        # would propagate past this endpoint's return and turn an
        # already-successful score+persist into an HTTP 500, discarding
        # `result` the caller would otherwise have received.
        # `TelegramNotifier.send_job` itself never raises for ordinary
        # network/API failures (see app.services.telegram's outcome
        # classification), but this must fail closed regardless of that
        # internal contract, not rely on it.
        notifier = TelegramNotifier(
            bot_token=reveal(settings.telegram_bot_token),
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_retries=settings.telegram_max_retries,
        )
        try:
            sent = await notifier.send_job(job, result)
        except Exception as exc:
            logger.warning(
                "job_score_notification_error job_id=%s error_type=%s",
                record.id,
                type(exc).__name__,
            )
        else:
            if not sent:
                logger.warning("job_score_notification_failed job_id=%s", record.id)

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
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
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
