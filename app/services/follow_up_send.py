"""Stage 7E orchestration: human APPROVE/REJECT decisions on an exact
`FollowUpProposalRecord`, and — only once approved — sending it as a real
Gmail message via an injected `OutboundEmailProvider`. This module is a
close mirror of app.services.response_draft_send (Stage 7D) — same hard
invariant, same CAS state machine, same honest delivery-outcome contract
— adapted for a candidate-initiated follow-up rather than a reply to an
inbound message. See that module's docstring for the full rationale;
only the differences are called out below.

**HARD INVARIANT: NO APPROVAL = NO FOLLOW-UP SEND.** `send_follow_up`
cannot reach the outbound provider unless every one of these holds:

- the proposal exists AND belongs to the caller's current account;
- a `FollowUpApprovalRecord` exists for this EXACT `follow_up_proposal_id`
  with `decision == "APPROVED"`;
- that approval has not already been consumed by a prior successful send,
  and no OTHER concurrent request currently holds the send claim.

**Recipient/threading trust boundary — different anchor than Stage 7D.**
Stage 7D replies to an INBOUND message and trusts that message's own
`from_address` as the recipient. A Stage 7E follow-up is candidate-
initiated — there is no fresh inbound message to reply to. The recipient
is instead the anchor **OUTBOUND** message's own `to_addresses` (the
address the candidate's own prior message was already sent to — already-
parsed, already-persisted Stage 7A structural metadata, never text
re-derived from a message body), and `In-Reply-To`/`References` are
built from that same anchor message's own threading headers. Nothing in
this module ever parses `body_plain` looking for a recipient/instruction.

**No other external side effect.** This module never mutates
`JobRecord.status`/`ApplicationStatus`, never calls Telegram, and never
fetches a URL. The ONLY external action anywhere in this module is the
one explicitly-approved `OutboundEmailProvider.send` call in
`send_follow_up`.
"""

import json
import logging

from sqlalchemy.orm import Session

from app.db.follow_up_approval_repository import (
    claim_send_attempt,
    create_approval,
    get_approval_for_proposal,
    get_follow_up_proposal_by_id,
    get_send_for_proposal,
    mark_send_failed,
    mark_send_sent,
    mark_send_uncertain,
    retry_send_attempt,
    to_follow_up_approval,
    to_follow_up_send_status,
)
from app.db.gmail_repository import get_message_by_id
from app.db.models import FollowUpApprovalRecord, FollowUpSendRecord
from app.models.follow_up import FollowUpState
from app.providers.email.outbound_base import (
    EmailSendError,
    EmailSendOutcomeUnknownError,
    OutboundEmailProvider,
    OutboundMessage,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FollowUpAlreadyDecidedError",
    "FollowUpAlreadySentError",
    "FollowUpMissingRecipientError",
    "FollowUpNotApprovedError",
    "FollowUpProposalNotFoundError",
    "FollowUpSendFailedError",
    "FollowUpSendInProgressError",
    "FollowUpSendOutcomeUncertainError",
    "approve_or_reject_follow_up",
    "get_follow_up_state",
    "send_follow_up",
]


class FollowUpProposalNotFoundError(Exception):
    """No `FollowUpProposalRecord` exists for (account_key, proposal_id)
    — mapped to 404. Covers both "no such id" and "exists, but for a
    different account" identically.
    """


class FollowUpAlreadyDecidedError(Exception):
    """A decision already exists for this exact `follow_up_proposal_id`
    — decisions are permanent. Mapped to 409.
    """


class FollowUpNotApprovedError(Exception):
    """No `APPROVED` decision exists for this exact `follow_up_proposal_id`
    (either no decision at all, or a `REJECTED` one) — mapped to 403.
    This is the direct enforcement point of "NO APPROVAL = NO FOLLOW-UP
    SEND".
    """


class FollowUpMissingRecipientError(Exception):
    """The anchor OUTBOUND message this follow-up continues has no usable
    recipient address on record — mapped to 422.
    """


class FollowUpAlreadySentError(Exception):
    """This proposal's approval has already been successfully consumed by
    a prior send — mapped to 409.
    """


class FollowUpSendInProgressError(Exception):
    """Another concurrent request currently holds the send claim for this
    proposal — mapped to 409.
    """


class FollowUpSendFailedError(Exception):
    """The outbound provider raised a DEFINITE pre-transmission failure —
    mapped to 502. May be retried by a later call.
    """


class FollowUpSendOutcomeUncertainError(Exception):
    """Transmission was attempted but delivery could be neither confirmed
    nor ruled out — mapped to 409, terminal, never auto-retried. See
    app.providers.email.outbound_base.EmailSendOutcomeUnknownError.
    """


def approve_or_reject_follow_up(
    db: Session,
    account_key: str,
    follow_up_proposal_id: int,
    decision: str,
    note: str | None,
) -> FollowUpApprovalRecord:
    """Record one immutable APPROVE/REJECT decision, pinning the exact
    `subject`/`body` of the target proposal at decision time. Raises
    `FollowUpProposalNotFoundError` or `FollowUpAlreadyDecidedError`.
    """
    proposal = get_follow_up_proposal_by_id(db, account_key, follow_up_proposal_id)
    if proposal is None:
        raise FollowUpProposalNotFoundError(
            f"No follow_up_proposals row for account_key={account_key!r} "
            f"id={follow_up_proposal_id!r}"
        )

    record, created = create_approval(
        db,
        account_key=account_key,
        follow_up_proposal_id=proposal.id,
        gmail_message_id=proposal.anchor_gmail_message_id,
        decision=decision,
        decision_note=note,
        pinned_subject=proposal.subject,
        pinned_body=proposal.body,
    )
    if not created:
        raise FollowUpAlreadyDecidedError(
            f"follow_up_proposal_id={follow_up_proposal_id!r} already has a recorded "
            f"decision ({record.decision!r}); decisions are permanent."
        )

    logger.info(
        "follow_up_decision_recorded follow_up_proposal_id=%s decision=%s",
        proposal.id,
        decision,
    )
    return record


def _build_outbound_message(anchor_message, approval: FollowUpApprovalRecord) -> OutboundMessage:
    to_addresses = json.loads(anchor_message.to_addresses_json)
    if not to_addresses:
        raise FollowUpMissingRecipientError(
            f"gmail_message_id={anchor_message.id!r} (follow-up anchor) has no "
            "to_addresses on record"
        )
    references = tuple(json.loads(anchor_message.references_json))
    if anchor_message.message_id_header and anchor_message.message_id_header not in references:
        references = (*references, anchor_message.message_id_header)
    return OutboundMessage(
        to_address=to_addresses[0],
        subject=approval.pinned_subject,
        body=approval.pinned_body,
        in_reply_to=anchor_message.message_id_header,
        references=references,
    )


def _claim_or_retry_send(
    db: Session, *, account_key: str, proposal, approval: FollowUpApprovalRecord
) -> FollowUpSendRecord:
    """Wins (or refuses) the right to actually call the outbound provider
    for this proposal — see module docstring's concurrency section.
    Mirrors app.services.response_draft_send._claim_or_retry_send exactly.
    """
    record, claimed = claim_send_attempt(
        db,
        account_key=account_key,
        follow_up_proposal_id=proposal.id,
        gmail_message_id=proposal.anchor_gmail_message_id,
        approval_id=approval.id,
    )
    if claimed:
        return record

    if record.status == "SENT":
        raise FollowUpAlreadySentError(
            f"follow_up_proposal_id={proposal.id!r} has already been sent"
        )
    if record.status == "PENDING":
        raise FollowUpSendInProgressError(
            f"A send attempt for follow_up_proposal_id={proposal.id!r} is already in progress"
        )
    if record.status == "UNCERTAIN":
        raise FollowUpSendOutcomeUncertainError(
            f"follow_up_proposal_id={proposal.id!r} has an uncertain prior send outcome; "
            "manual reconciliation is required, not an automatic retry"
        )
    won_retry = retry_send_attempt(db, record)
    if not won_retry:
        raise FollowUpSendInProgressError(
            f"A concurrent retry for follow_up_proposal_id={proposal.id!r} is already in progress"
        )
    return record


def send_follow_up(
    db: Session, account_key: str, follow_up_proposal_id: int, provider: OutboundEmailProvider
) -> FollowUpSendRecord:
    """Send an APPROVED follow-up as a real Gmail message. See module
    docstring for the full send-gate contract. Raises one of
    `FollowUpProposalNotFoundError` / `FollowUpNotApprovedError` /
    `FollowUpMissingRecipientError` / `FollowUpAlreadySentError` /
    `FollowUpSendInProgressError` / `FollowUpSendFailedError` /
    `FollowUpSendOutcomeUncertainError`.
    """
    proposal = get_follow_up_proposal_by_id(db, account_key, follow_up_proposal_id)
    if proposal is None:
        raise FollowUpProposalNotFoundError(
            f"No follow_up_proposals row for account_key={account_key!r} "
            f"id={follow_up_proposal_id!r}"
        )

    approval = get_approval_for_proposal(db, account_key, proposal.id)
    if approval is None or approval.decision != "APPROVED":
        raise FollowUpNotApprovedError(
            f"follow_up_proposal_id={follow_up_proposal_id!r} has no APPROVED decision on record"
        )

    anchor_message = get_message_by_id(db, account_key, proposal.anchor_gmail_message_id)
    if anchor_message is None:
        raise FollowUpMissingRecipientError(
            f"anchor gmail_message_id={proposal.anchor_gmail_message_id!r} could not be resolved"
        )
    outbound_message = _build_outbound_message(anchor_message, approval)

    send_record = _claim_or_retry_send(
        db, account_key=account_key, proposal=proposal, approval=approval
    )

    try:
        result = provider.send(outbound_message)
    except EmailSendOutcomeUnknownError as exc:
        mark_send_uncertain(db, send_record, last_error=type(exc).__name__)
        logger.warning(
            "follow_up_send_outcome_uncertain follow_up_proposal_id=%s error_type=%s",
            proposal.id,
            type(exc).__name__,
        )
        raise FollowUpSendOutcomeUncertainError(
            f"Sending follow_up_proposal_id={follow_up_proposal_id!r} had an uncertain outcome"
        ) from exc
    except EmailSendError as exc:
        mark_send_failed(db, send_record, last_error=type(exc).__name__)
        logger.warning(
            "follow_up_send_failed follow_up_proposal_id=%s error_type=%s",
            proposal.id,
            type(exc).__name__,
        )
        raise FollowUpSendFailedError(
            f"Sending follow_up_proposal_id={follow_up_proposal_id!r} failed"
        ) from exc

    mark_send_sent(db, send_record, provider_message_id=result.provider_message_id)
    logger.info("follow_up_sent follow_up_proposal_id=%s", proposal.id)
    return send_record


def get_follow_up_state(db: Session, account_key: str, follow_up_proposal_id: int) -> FollowUpState:
    """Pure read of the combined approval/send state for one proposal —
    GET /follow-ups/{id}/state. Raises `FollowUpProposalNotFoundError` if
    the proposal does not exist (or belongs to a different account).
    """
    proposal = get_follow_up_proposal_by_id(db, account_key, follow_up_proposal_id)
    if proposal is None:
        raise FollowUpProposalNotFoundError(
            f"No follow_up_proposals row for account_key={account_key!r} "
            f"id={follow_up_proposal_id!r}"
        )

    approval = get_approval_for_proposal(db, account_key, proposal.id)
    send = get_send_for_proposal(db, account_key, proposal.id)

    return FollowUpState(
        follow_up_proposal_id=proposal.id,
        approval=to_follow_up_approval(approval) if approval is not None else None,
        send=to_follow_up_send_status(send) if send is not None else None,
    )
