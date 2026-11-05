"""HTTP routes: response drafts (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers import common
from app.api.routers.common import MAX_OFFSET, _current_gmail_account_key
from app.db.gmail_repository import (
    get_message_by_id,
)
from app.db.response_draft_approval_repository import (
    PENDING_RESPONSE_DRAFTS_DEFAULT_LIMIT,
    PENDING_RESPONSE_DRAFTS_MAX_LIMIT,
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
from app.db.session import get_db
from app.models.response_draft import ResponseDraft
from app.models.response_draft_approval import (
    PendingResponseDraft,
    ResponseDraftApproval,
    ResponseDraftApprovalRequest,
    ResponseDraftSendStatus,
    ResponseDraftState,
)
from app.providers.email.smtp import GmailSmtpProvider
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_rate_limit,
    enforce_response_draft_decision_rate_limit,
    enforce_response_draft_rate_limit,
    enforce_response_draft_send_rate_limit,
)
from app.services.response_draft import (
    ResponseDraftAnalysisNotFoundError,
    ResponseDraftMessageNotFoundError,
    generate_response_draft_for_message,
)
from app.services.response_draft_send import (
    ResponseDraftAlreadyDecidedError,
    ResponseDraftAlreadySentError,
    ResponseDraftMessageAlreadyRepliedError,
    ResponseDraftMissingRecipientError,
    ResponseDraftNotApprovableError,
    ResponseDraftNotApprovedError,
    ResponseDraftNotFoundError,
    ResponseDraftRecipientDriftError,
    ResponseDraftRecipientMismatchError,
    ResponseDraftRecipientNotPinnedError,
    ResponseDraftSenderNotAuthenticatedError,
    ResponseDraftSendFailedError,
    ResponseDraftSendInProgressError,
    ResponseDraftSendOutcomeUncertainError,
    ResponseDraftSupersededError,
    approve_or_reject_response_draft,
    get_pending_response_drafts,
    get_response_draft_state,
    send_response_draft,
)
from app.utils.config_flags import is_configured

logger = logging.getLogger("app.api.routes")
router = APIRouter()

_SENDER_NOT_AUTHENTICATED_DETAIL = (
    "The sender of the inbound message is not authenticated (no DMARC pass aligned with the "
    "From domain); this system will not reply to it. Verify the sender elsewhere and answer "
    "manually"
)


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
    account_key = _current_gmail_account_key(common.get_settings())
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
    account_key = _current_gmail_account_key(common.get_settings())
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
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[ResponseDraft]:
    """The FULL, bounded, most-recent-first history of response-draft
    revisions for one message — each row is one immutable revision, not
    deduplicated to "latest" (see GET .../response-draft for that).
    """
    account_key = _current_gmail_account_key(common.get_settings())
    message = get_message_by_id(db, account_key, message_id)
    if message is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Gmail message not found"
        )
    records = list_response_drafts_for_message(db, account_key, message_id, limit, offset)
    return [to_response_draft(record) for record in records]


@router.get(
    "/response-drafts/pending",
    response_model=list[PendingResponseDraft],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def list_pending_response_drafts_endpoint(
    limit: int = Query(
        default=PENDING_RESPONSE_DRAFTS_DEFAULT_LIMIT, ge=1, le=PENDING_RESPONSE_DRAFTS_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[PendingResponseDraft]:
    """Stage 12: the human-decision queue -- each inbound message's
    newest PROPOSED response-draft revision that has no decision yet and
    whose message has no live reply. Pure read: never generates a draft,
    never records a decision, never sends. `is_current_revision=False`
    means the revision is superseded and must be regenerated before it can
    be approved.
    """
    account_key = _current_gmail_account_key(common.get_settings())
    return get_pending_response_drafts(db, account_key, limit=limit, offset=offset)


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

    Stage 12: APPROVED is refused (409) for a superseded revision or when
    another revision of the same message already has a live reply;
    REJECTED is always accepted for a PROPOSED revision.
    """
    account_key = _current_gmail_account_key(common.get_settings())
    try:
        record = approve_or_reject_response_draft(
            db, account_key, draft_id, body.decision, body.note, body.expected_recipient
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
    except ResponseDraftMissingRecipientError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No safe reply recipient is available for this message",
        ) from exc
    except ResponseDraftRecipientDriftError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="The reply recipient differs from the one the operator reviewed",
        ) from exc
    except ResponseDraftSenderNotAuthenticatedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_SENDER_NOT_AUTHENTICATED_DETAIL,
        ) from exc
    except ResponseDraftSupersededError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Response draft is superseded by a newer revision",
        ) from exc
    except ResponseDraftMessageAlreadyRepliedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Another revision for this message already has a reply in progress or sent",
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
    settings = common.get_settings()
    if not is_configured(settings.gmail_username) or not is_configured(settings.gmail_app_password):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outbound email sending is not configured",
        )
    # Stage 9 fail-closed kill switch: checked here, BEFORE
    # send_response_draft is ever called, so a disabled switch never
    # touches the CAS send-claim state machine at all (no PENDING/FAILED
    # row is created) — mirrors the is_configured check immediately
    # above. GmailSmtpProvider itself also refuses to transmit when
    # outbound_enabled is falsy (see its own send()) as defense in depth
    # for any future caller that bypasses this route.
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
    except ResponseDraftRecipientNotPinnedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This approval has no pinned recipient and can never be sent",
        ) from exc
    except ResponseDraftRecipientMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="The reply recipient changed since this draft was approved",
        ) from exc
    except ResponseDraftSenderNotAuthenticatedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_SENDER_NOT_AUTHENTICATED_DETAIL,
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
    except ResponseDraftMessageAlreadyRepliedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Another revision for this message already has a reply in progress or sent",
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
    account_key = _current_gmail_account_key(common.get_settings())
    try:
        return get_response_draft_state(db, account_key, draft_id)
    except ResponseDraftNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Response draft not found"
        ) from exc
