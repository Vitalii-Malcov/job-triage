"""Stage 7D orchestration: human APPROVE/REJECT decisions on an exact
Stage 7C `ResponseDraftRecord` revision, and — only once approved —
sending that draft as a real Gmail reply via an injected
`OutboundEmailProvider`.

**HARD INVARIANT: NO APPROVAL = NO SEND.** `send_response_draft` cannot
reach the outbound provider at all unless every one of these holds:

- the draft exists AND belongs to the caller's current account
  (`get_response_draft_by_id` is account-scoped by construction — a
  cross-account `draft_id` simply does not resolve, never a 403 that
  would confirm the id exists);
- `draft.status == "PROPOSED"` (a `NO_RESPONSE_RECOMMENDED` draft has no
  content to send and can never be approved in the first place — see
  `approve_or_reject_response_draft`);
- `draft.requires_human_review is True` (always true for every Stage 7C
  row by construction — checked here anyway, defense in depth, mirroring
  this project's GMAIL-009-style "DB/app invariant checked again at the
  point of consequence" convention);
- an `ResponseDraftApprovalRecord` exists for this EXACT `response_draft_id`
  with `decision == "APPROVED"` (a `REJECTED` decision, or no decision at
  all, both fail closed — see `ResponseDraftNotApprovedError`);
- that approval has not already been consumed by a prior successful send,
  and no OTHER concurrent request currently holds the send claim (see
  `app.db.response_draft_approval_repository`'s `claim_send_attempt` /
  `retry_send_attempt` CAS discipline, and `ResponseDraftAlreadySentError`
  / `ResponseDraftSendInProgressError`).

**Never trusts email-body content for recipient/header/action selection.**
The outbound recipient is the ORIGINAL inbound message's own
`from_address` (already-parsed, already-persisted Stage 7A structural
metadata — never text re-derived from `body_plain`), and
`In-Reply-To`/`References` are copied verbatim from that same message's
own headers. Subject/body sent are the approval's PINNED copies (see
`ResponseDraftApprovalRecord`'s docstring), never a live re-read of
`ResponseDraftRecord` at send time. Nothing in this module ever parses
`body_plain` looking for an instruction, a different recipient, or any
other directive — an attacker who fully controls the analyzed email's
content still cannot influence WHO this module sends to or WHAT header
values it uses; the only degree of freedom they retain is indirectly
shaping which Stage 7C TEMPLATE gets chosen upstream (already governed by
Stage 7C's own trust boundary — see app.services.response_draft's module
docstring), never this module's own recipient/header logic.

**No other external side effect.** This module never mutates
`JobRecord.status`/`ApplicationStatus`, never calls Telegram, never
fetches a URL, and never follows a tracking link. The ONLY external
action anywhere in Stage 7D is the one explicitly-approved
`OutboundEmailProvider.send` call in `send_response_draft`.

**Do not mark SENT before provider success; failures never falsely
consume the approval.** See `app.db.response_draft_approval_repository`'s
`mark_send_sent`/`mark_send_failed`/`retry_send_attempt` docstrings for
the exact CAS state machine this module drives.

**Honest concurrency/delivery contract (no false exactly-once claim).**
This project's DB-level concurrency control (`ResponseDraftSendRecord`'s
`UNIQUE(response_draft_id)` claim + CAS state machine) reliably prevents
two concurrent/retried REQUESTS to this module from both attempting to
send the same approval — that guarantee is real and unconditional. It
does NOT, and cannot, prove that an ambiguous SMTP failure means the
recruiter never received the reply: `app.providers.email.outbound_base
.EmailSendOutcomeUnknownError` covers exactly that "transmission was
attempted, acceptance cannot be proven either way" case, and this
module's response is fail-closed, not exactly-once — see
`ResponseDraftSendOutcomeUncertainError` and `ResponseDraftSendRecord`'s
`UNCERTAIN` status: transmission is never automatically retried once
ambiguous, and a later send request for the same draft is refused before
the provider is ever called again. Resolving a genuinely `UNCERTAIN`
outcome (confirming with the recruiter, deciding whether to manually
follow up) is a human task this module deliberately does not attempt.
"""

import json
import logging

from sqlalchemy.orm import Session

from app.db.gmail_repository import get_message_by_id
from app.db.models import ResponseDraftApprovalRecord, ResponseDraftSendRecord
from app.db.response_draft_approval_repository import (
    claim_send_attempt,
    create_approval,
    get_approval_for_draft,
    get_response_draft_by_id,
    get_send_for_draft,
    mark_send_failed,
    mark_send_sent,
    mark_send_uncertain,
    retry_send_attempt,
    to_response_draft_approval,
    to_response_draft_send_status,
)
from app.models.response_draft_approval import ResponseDraftState
from app.providers.email.outbound_base import (
    EmailSendError,
    EmailSendOutcomeUnknownError,
    OutboundEmailProvider,
    OutboundMessage,
)

logger = logging.getLogger(__name__)


class ResponseDraftNotFoundError(Exception):
    """No `ResponseDraftRecord` exists for (account_key, draft_id) —
    mapped to 404. Covers both "no such id at all" and "exists, but for
    a different account" identically (cross-account access must never be
    distinguishable from non-existence).
    """


class ResponseDraftNotApprovableError(Exception):
    """The draft's `status` is not `"PROPOSED"` (e.g.
    `NO_RESPONSE_RECOMMENDED`, or `requires_human_review` is somehow not
    True) — there is no content to approve or send. Mapped to 422.
    """


class ResponseDraftAlreadyDecidedError(Exception):
    """A decision already exists for this exact `response_draft_id` —
    decisions are permanent (see `ResponseDraftApprovalRecord`'s
    docstring); a new draft revision, not a changed decision, is the only
    way to reconsider. Mapped to 409.
    """


class ResponseDraftNotApprovedError(Exception):
    """No `APPROVED` decision exists for this exact `response_draft_id`
    (either no decision at all, or a `REJECTED` one) — mapped to 403.
    This is the direct enforcement point of "NO APPROVAL = NO SEND".
    """


class ResponseDraftMissingRecipientError(Exception):
    """The original inbound message this draft responds to has no
    `from_address` on record — there is no trusted recipient to send to.
    Mapped to 422. Should be rare in practice (Stage 7A persists
    `from_address` as `str | None`) but never guessed/defaulted.
    """


class ResponseDraftAlreadySentError(Exception):
    """This draft's approval has already been successfully consumed by a
    prior send — mapped to 409. Idempotency boundary: a second send
    attempt is REJECTED, never silently re-sent and never silently
    reported as a fresh success.
    """


class ResponseDraftSendInProgressError(Exception):
    """Another concurrent request currently holds the send claim for
    this draft (a live `PENDING` attempt, or this request lost a
    `FAILED -> PENDING` retry race to a concurrent retry) — mapped to
    409. The caller should poll GET .../state rather than retry
    immediately in a tight loop.
    """


class ResponseDraftSendFailedError(Exception):
    """The outbound provider raised a DEFINITE pre-transmission failure
    (`EmailSendAuthError`/`EmailSendConnectionError`) while attempting
    this send — the underlying `response_draft_sends` row has already
    been transitioned to `FAILED` (never left dangling in `PENDING`) and
    MAY be retried by a later call. Mapped to 502. Carries no upstream
    exception text (see app.providers.email.outbound_base.EmailSendError's
    docstring) — only a fixed, generic message. NEVER raised for an
    ambiguous outcome — see `ResponseDraftSendOutcomeUncertainError`.
    """


class ResponseDraftSendOutcomeUncertainError(Exception):
    """Raised in two situations, both mapped to 409 — either (a) THIS
    send attempt just became ambiguous (the outbound provider raised
    `EmailSendOutcomeUnknownError`: transmission was attempted but
    delivery could not be confirmed OR ruled out — the underlying
    `response_draft_sends` row has already been transitioned to the
    terminal `UNCERTAIN` status), or (b) this draft's send was ALREADY
    `UNCERTAIN` from a prior attempt, and this request is refused BEFORE
    the outbound provider is ever called again — see
    `_claim_or_retry_send`. Either way: NO automatic retry, ever;
    resolving the ambiguity is a human task outside Stage 7D's scope
    (see module docstring's "honest concurrency/delivery contract").
    """


def approve_or_reject_response_draft(
    db: Session,
    account_key: str,
    draft_id: int,
    decision: str,
    note: str | None,
) -> ResponseDraftApprovalRecord:
    """Record one immutable APPROVE/REJECT decision, pinning the exact
    `subject`/`body` of the target draft at decision time. Raises
    `ResponseDraftNotFoundError`, `ResponseDraftNotApprovableError`, or
    `ResponseDraftAlreadyDecidedError` — never silently overwrites an
    existing decision.
    """
    draft = get_response_draft_by_id(db, account_key, draft_id)
    if draft is None:
        raise ResponseDraftNotFoundError(
            f"No response_drafts row for account_key={account_key!r} id={draft_id!r}"
        )
    if draft.status != "PROPOSED" or draft.subject is None or draft.body is None:
        raise ResponseDraftNotApprovableError(
            f"response_draft_id={draft_id!r} has status={draft.status!r}; only a "
            "PROPOSED draft with content can be approved or rejected."
        )

    record, created = create_approval(
        db,
        account_key=account_key,
        response_draft_id=draft.id,
        gmail_message_id=draft.gmail_message_id,
        decision=decision,
        decision_note=note,
        pinned_subject=draft.subject,
        pinned_body=draft.body,
    )
    if not created:
        raise ResponseDraftAlreadyDecidedError(
            f"response_draft_id={draft_id!r} already has a recorded decision "
            f"({record.decision!r}); decisions are permanent."
        )

    logger.info(
        "response_draft_decision_recorded response_draft_id=%s decision=%s",
        draft.id,
        decision,
    )
    return record


def _build_outbound_message(message, approval: ResponseDraftApprovalRecord) -> OutboundMessage:
    if not message.from_address:
        raise ResponseDraftMissingRecipientError(
            f"gmail_message_id={message.id!r} has no from_address on record"
        )
    references = tuple(json.loads(message.references_json))
    if message.message_id_header and message.message_id_header not in references:
        references = (*references, message.message_id_header)
    return OutboundMessage(
        to_address=message.from_address,
        subject=approval.pinned_subject,
        body=approval.pinned_body,
        in_reply_to=message.message_id_header,
        references=references,
    )


def _claim_or_retry_send(
    db: Session, *, account_key: str, draft, approval: ResponseDraftApprovalRecord
) -> ResponseDraftSendRecord:
    """Wins (or refuses) the right to actually call the outbound
    provider for this draft — see module docstring's concurrency
    section. Raises `ResponseDraftAlreadySentError` /
    `ResponseDraftSendInProgressError` /
    `ResponseDraftSendOutcomeUncertainError` when this call must NOT
    proceed.
    """
    record, claimed = claim_send_attempt(
        db,
        account_key=account_key,
        response_draft_id=draft.id,
        gmail_message_id=draft.gmail_message_id,
        approval_id=approval.id,
    )
    if claimed:
        return record

    if record.status == "SENT":
        raise ResponseDraftAlreadySentError(f"response_draft_id={draft.id!r} has already been sent")
    if record.status == "PENDING":
        raise ResponseDraftSendInProgressError(
            f"A send attempt for response_draft_id={draft.id!r} is already in progress"
        )
    if record.status == "UNCERTAIN":
        # Fail-closed and terminal — see ResponseDraftSendRecord's
        # docstring. Refused BEFORE the provider is ever called again;
        # never routed through retry_send_attempt (whose CAS only ever
        # matches status='FAILED' and therefore could never touch this
        # row anyway, but the explicit check here makes the refusal
        # reason accurate rather than an incidental side effect).
        raise ResponseDraftSendOutcomeUncertainError(
            f"response_draft_id={draft.id!r} has an uncertain prior send outcome; "
            "manual reconciliation is required, not an automatic retry"
        )
    # status == "FAILED": a legitimate retry — try to win the CAS back to
    # PENDING. If we lose (a concurrent retry got there first), the
    # winner owns this attempt; we must not also proceed.
    won_retry = retry_send_attempt(db, record)
    if not won_retry:
        raise ResponseDraftSendInProgressError(
            f"A concurrent retry for response_draft_id={draft.id!r} is already in progress"
        )
    return record


def send_response_draft(
    db: Session, account_key: str, draft_id: int, provider: OutboundEmailProvider
) -> ResponseDraftSendRecord:
    """Send an APPROVED response draft as a real Gmail reply. See module
    docstring for the full send-gate contract. Raises one of
    `ResponseDraftNotFoundError` / `ResponseDraftNotApprovableError` /
    `ResponseDraftNotApprovedError` / `ResponseDraftMissingRecipientError`
    / `ResponseDraftAlreadySentError` / `ResponseDraftSendInProgressError`
    / `ResponseDraftSendFailedError` / `ResponseDraftSendOutcomeUncertainError`.
    """
    draft = get_response_draft_by_id(db, account_key, draft_id)
    if draft is None:
        raise ResponseDraftNotFoundError(
            f"No response_drafts row for account_key={account_key!r} id={draft_id!r}"
        )
    if (
        draft.status != "PROPOSED"
        or draft.subject is None
        or draft.body is None
        or draft.requires_human_review is not True
    ):
        raise ResponseDraftNotApprovableError(
            f"response_draft_id={draft_id!r} has status={draft.status!r}; only a "
            "PROPOSED, human-review-flagged draft can be sent."
        )

    approval = get_approval_for_draft(db, account_key, draft.id)
    if approval is None or approval.decision != "APPROVED":
        raise ResponseDraftNotApprovedError(
            f"response_draft_id={draft_id!r} has no APPROVED decision on record"
        )

    message = get_message_by_id(db, account_key, draft.gmail_message_id)
    if message is None:
        raise ResponseDraftMissingRecipientError(
            f"gmail_message_id={draft.gmail_message_id!r} could not be resolved"
        )
    outbound_message = _build_outbound_message(message, approval)

    send_record = _claim_or_retry_send(db, account_key=account_key, draft=draft, approval=approval)

    try:
        result = provider.send(outbound_message)
    except EmailSendOutcomeUnknownError as exc:
        # Transmission was ATTEMPTED — delivery can be neither confirmed
        # nor ruled out. This must NEVER be treated as FAILED (that would
        # make it retryable and risk a duplicate send) — it transitions
        # to the terminal UNCERTAIN state instead. Caught BEFORE the
        # broader `EmailSendError` below (this is a subclass of it) —
        # ordering matters.
        mark_send_uncertain(db, send_record, last_error=type(exc).__name__)
        logger.warning(
            "response_draft_send_outcome_uncertain response_draft_id=%s error_type=%s",
            draft.id,
            type(exc).__name__,
        )
        raise ResponseDraftSendOutcomeUncertainError(
            f"Sending response_draft_id={draft_id!r} had an uncertain outcome"
        ) from exc
    except EmailSendError as exc:
        # A DEFINITE pre-transmission failure only — see
        # EmailSendConnectionError/EmailSendAuthError's docstrings.
        # GMAIL-003-style: never persist/log the upstream exception's own
        # message text — only its type. mark_send_failed further bounds
        # this to 500 chars regardless.
        mark_send_failed(db, send_record, last_error=type(exc).__name__)
        logger.warning(
            "response_draft_send_failed response_draft_id=%s error_type=%s",
            draft.id,
            type(exc).__name__,
        )
        raise ResponseDraftSendFailedError(
            f"Sending response_draft_id={draft_id!r} failed"
        ) from exc

    mark_send_sent(db, send_record, provider_message_id=result.provider_message_id)
    logger.info("response_draft_sent response_draft_id=%s", draft.id)
    return send_record


def get_response_draft_state(db: Session, account_key: str, draft_id: int) -> ResponseDraftState:
    """Pure read of the combined approval/send state for one draft — GET
    /response-drafts/{draft_id}/state. Raises `ResponseDraftNotFoundError`
    if the draft does not exist (or belongs to a different account).
    """
    draft = get_response_draft_by_id(db, account_key, draft_id)
    if draft is None:
        raise ResponseDraftNotFoundError(
            f"No response_drafts row for account_key={account_key!r} id={draft_id!r}"
        )

    approval = get_approval_for_draft(db, account_key, draft.id)
    send = get_send_for_draft(db, account_key, draft.id)

    return ResponseDraftState(
        response_draft_id=draft.id,
        gmail_message_id=draft.gmail_message_id,
        draft_status=draft.status,
        approval=to_response_draft_approval(approval) if approval is not None else None,
        send=to_response_draft_send_status(send) if send is not None else None,
    )
