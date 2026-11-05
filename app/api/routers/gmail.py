"""HTTP routes: gmail (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers import common
from app.api.routers.common import (
    GMAIL_ANALYSES_DEFAULT_LIST_LIMIT,
    GMAIL_ANALYSES_MAX_LIST_LIMIT,
    GMAIL_DEFAULT_LIST_LIMIT,
    GMAIL_MAX_LIST_LIMIT,
    MAX_OFFSET,
    _current_gmail_account_key,
)
from app.db.gmail_analysis_repository import (
    get_latest_analysis_for_message,
    list_analyses,
    to_gmail_message_analysis,
)
from app.db.gmail_repository import (
    THREAD_DETAIL_DEFAULT_MESSAGE_LIMIT,
    THREAD_DETAIL_MAX_MESSAGE_LIMIT,
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
from app.db.session import get_db
from app.models.gmail import (
    GmailMessage,
    GmailMessageSummary,
    GmailSyncResult,
    GmailThread,
    GmailThreadDetail,
)
from app.models.gmail_analysis import GmailMessageAnalysis
from app.providers.email.base import GmailProviderError
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_gmail_analysis_rate_limit,
    enforce_gmail_rate_limit,
    enforce_rate_limit,
)
from app.services.collector_runner import (
    CollectorNotConfiguredError,
)
from app.services.gmail_message_analysis import GmailMessageNotFoundError, analyze_gmail_message
from app.services.gmail_sync import run_gmail_sync as run_gmail_sync_service
from app.services.offload import run_off_loop

logger = logging.getLogger("app.api.routes")
router = APIRouter()


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
    settings = common.get_settings()
    try:
        return await run_off_loop(lambda: run_gmail_sync_service(db, settings))
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


@router.get(
    "/gmail/messages",
    response_model=list[GmailMessageSummary],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_messages(
    limit: int = Query(default=GMAIL_DEFAULT_LIST_LIMIT, ge=1, le=GMAIL_MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[GmailMessageSummary]:
    """Pure read of already-synced messages, in compact summary form
    (GMAIL-007) — never body_plain/full recipients/references; see
    GET /gmail/messages/{id} for full detail. Never triggers a sync
    itself — POST /gmail/sync is the only endpoint that reads the
    mailbox.
    """
    account_key = _current_gmail_account_key(common.get_settings())
    records = list_messages(db, account_key, limit=limit, offset=offset)
    return [to_gmail_message_summary(record) for record in records]


@router.get(
    "/gmail/messages/{message_id}",
    response_model=GmailMessage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_gmail_message(message_id: int, db: Session = Depends(get_db)) -> GmailMessage:
    account_key = _current_gmail_account_key(common.get_settings())
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
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[GmailThread]:
    account_key = _current_gmail_account_key(common.get_settings())
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
    account_key = _current_gmail_account_key(common.get_settings())
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
    account_key = _current_gmail_account_key(common.get_settings())
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
    account_key = _current_gmail_account_key(common.get_settings())
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
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[GmailMessageAnalysis]:
    """Bounded, most-recent-first list of persisted analysis revisions
    for the configured mailbox account. Each row is one immutable
    revision (not deduplicated to "latest per message") — a message
    re-analyzed under a new ANALYSIS_VERSION appears here more than once.
    """
    account_key = _current_gmail_account_key(common.get_settings())
    records = list_analyses(db, account_key, limit=limit, offset=offset)
    return [to_gmail_message_analysis(record) for record in records]
