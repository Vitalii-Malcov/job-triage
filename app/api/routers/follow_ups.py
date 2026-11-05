"""HTTP routes: follow ups (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers import common
from app.api.routers.common import MAX_OFFSET, _current_gmail_account_key
from app.db.follow_up_approval_repository import to_follow_up_approval, to_follow_up_send_status
from app.db.follow_up_repository import (
    FOLLOW_UP_LIST_DEFAULT_LIMIT,
    FOLLOW_UP_LIST_MAX_LIMIT,
    get_follow_up_proposal_by_id,
    list_follow_up_proposals,
    to_follow_up_proposal,
)
from app.db.session import get_db
from app.models.follow_up import (
    FollowUpApproval,
    FollowUpApprovalRequest,
    FollowUpEvaluationResult,
    FollowUpProposal,
    FollowUpScanSummary,
    FollowUpSendStatus,
    FollowUpState,
)
from app.providers.email.smtp import GmailSmtpProvider
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_follow_up_decision_rate_limit,
    enforce_follow_up_evaluate_rate_limit,
    enforce_follow_up_send_rate_limit,
    enforce_rate_limit,
)
from app.services.follow_up import (
    FollowUpJobNotFoundError,
    evaluate_follow_up_for_job,
    list_due_follow_ups,
)
from app.services.follow_up_send import (
    FollowUpAlreadyDecidedError,
    FollowUpAlreadySentError,
    FollowUpAnchorAlreadyFollowedUpError,
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
from app.utils.config_flags import is_configured

logger = logging.getLogger("app.api.routes")
router = APIRouter()


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
    account_key = _current_gmail_account_key(common.get_settings())
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
    account_key = _current_gmail_account_key(common.get_settings())
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
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[FollowUpProposal]:
    """Pure read of already-persisted follow-up proposals — never
    triggers evaluation itself (mirrors GET /gmail/messages not
    triggering a sync — see POST /follow-ups/evaluate for that).
    """
    account_key = _current_gmail_account_key(common.get_settings())
    records = list_follow_up_proposals(db, account_key, limit=limit, offset=offset)
    return [to_follow_up_proposal(record) for record in records]


@router.get(
    "/follow-ups/{follow_up_id}",
    response_model=FollowUpProposal,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_follow_up(follow_up_id: int, db: Session = Depends(get_db)) -> FollowUpProposal:
    account_key = _current_gmail_account_key(common.get_settings())
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
    account_key = _current_gmail_account_key(common.get_settings())
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
    except FollowUpAnchorAlreadyFollowedUpError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Another follow-up for this correspondence is already in progress or sent",
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
    settings = common.get_settings()
    if not is_configured(settings.gmail_username) or not is_configured(settings.gmail_app_password):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outbound email sending is not configured",
        )
    # Stage 9 fail-closed kill switch: same rationale as
    # send_response_draft_endpoint's identical check above.
    if not settings.outbound_sending_enabled:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outbound sending is disabled",
        )

    account_key = _current_gmail_account_key(settings)
    provider = GmailSmtpProvider(
        smtp_host=settings.gmail_smtp_host,
        smtp_port=settings.gmail_smtp_port,
        username=settings.gmail_username,
        app_password=settings.gmail_app_password,
        outbound_enabled=settings.outbound_sending_enabled,
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
    except FollowUpAnchorAlreadyFollowedUpError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Another follow-up for this correspondence is already in progress or sent",
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
    account_key = _current_gmail_account_key(common.get_settings())
    try:
        return get_follow_up_state(db, account_key, follow_up_id)
    except FollowUpProposalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Follow-up proposal not found"
        ) from exc
