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
  and no OTHER concurrent request currently holds the send claim;
- S7E-002 (Codex remediation): the proposal is STILL eligible at the
  instant immediately before transmission — see
  `_revalidate_or_fail_closed` and this module's "Send-time revalidation"
  section below.

**Recipient/threading trust boundary — different anchor than Stage 7D.**
Stage 7D replies to an INBOUND message and trusts that message's own
`from_address` as the recipient. A Stage 7E follow-up is candidate-
initiated — there is no fresh inbound message to reply to. The recipient
is instead the SINGLE canonical address `app.services.follow_up_recipient
.derive_canonical_recipient` validated from the anchor **OUTBOUND**
message's own `to_addresses` (the address the candidate's own prior
message was already sent to — already-parsed, already-persisted Stage 7A
structural metadata, never text re-derived from a message body) at
PROPOSAL-build time, and PINNED on the approval
(`FollowUpApprovalRecord.pinned_recipient`) — this module always sends to
that pinned value, never a live re-read (see `_build_outbound_message`).
`In-Reply-To`/`References` are built from the anchor message's own
threading headers. Nothing in this module ever parses `body_plain`
looking for a recipient/instruction.

**Send-time revalidation (S7E-002, Codex remediation).** Winning the send
claim only proves no other request is CURRENTLY attempting this exact
proposal — it says nothing about whether the proposal is still valid NOW,
possibly long after it was approved. Immediately before the outbound
provider is ever called, `_revalidate_or_fail_closed` re-derives the
job's CURRENT eligibility from scratch (same
`app.services.follow_up.compute_fresh_follow_up_state` helper
`app.services.follow_up.evaluate_follow_up_for_job` itself uses) and
requires ALL of: the job is still APPLIED; the job's Stage 7B match is
still decisive and resolves to the SAME single thread; that thread's
latest-OUTBOUND/latest-INBOUND state is still ELIGIBLE (no newer inbound
reply, delay still elapsed); the anchor this proposal was built from is
STILL the thread's latest OUTBOUND message (an unchanged identity); and
the anchor's freshly-re-derived canonical recipient still EXACTLY matches
the pinned one. Any mismatch fails closed
(`FollowUpProposalStaleAtSendTimeError`) and marks the send attempt
FAILED (not UNCERTAIN — no transmission was attempted) rather than ever
silently sending stale/re-targeted content.

**Race safety between revalidation and send (S7E-002/010).** Revalidation
and the provider call are only ever performed by the request that won
`begin_transmission` — a CAS `send_attempted: False -> True` that is
mutually exclusive across concurrent requests for the same proposal (see
`app.db.follow_up_approval_repository.begin_transmission`'s docstring).
This closes the TOCTOU window a plain "revalidate, then send" sequence
would otherwise have BETWEEN TWO SEND REQUESTS: two concurrent requests
can no longer both pass revalidation and then both call the provider,
because only one of them ever reaches the revalidation step at all for a
given proposal.

**`begin_transmission` does NOT protect against a Gmail sync writer
(S7E-012, Codex re-review, MEDIUM).** The CAS above is scoped entirely to
`follow_up_sends` rows — it says nothing about, and is never touched by,
`POST /gmail/sync` (`app.services.gmail_inbox.GmailInboxService`), which
persists new `GmailMessageRecord` rows on its own, completely independent
schedule/connection. A Gmail sync can legitimately commit a brand-new
INBOUND reply for this exact thread in the window between
`_revalidate_or_fail_closed` reading "no reply yet" and `provider.send`
actually being invoked — `send_follow_up` cannot lock the mailbox, and a
message, once sent, cannot be unsent, so this must be caught BEFORE the
provider call, not after. `_fail_closed_if_reply_raced_dispatch` is a
second, deliberately minimal re-check — not a repeat of the full
revalidation — positioned as the LITERAL LAST statement before
`provider.send`, specifically for this one remaining risk: it re-reads
only the thread's current latest-OUTBOUND/latest-INBOUND state (the same
bounded, direct queries `app.db.follow_up_repository.get_thread_message_infos`
always uses) and fails closed (marks the send `FAILED`, never calls the
provider) if a reply has landed since the main revalidation ran. Kept
intentionally tiny — one bounded read, no job/thread-ambiguity/recipient
re-derivation — so it adds as little of its own latency (and therefore as
little of its own residual race window) as possible between itself and
the call it guards. This narrows, rather than mathematically eliminates,
the window: a Gmail sync commit landing in the sub-millisecond gap
between THIS check's read returning and `provider.send` actually starting
is not mechanically prevented (this project holds no lock spanning an
outbound network call across process/connection boundaries) — accepted
because closing it further would require Gmail sync itself to
participate in a Stage-7E-specific lock, which app.db.gmail_repository.py
deliberately never does (Stage 7A's "zero job/application linkage" — see
app/services/gmail_inbox.py's module docstring).

**Crash/CAS recovery (S7E-010, Codex remediation).** `send_attempted`
durably distinguishes "transmission was never attempted for this claim"
(safe to hand to a later request) from "transmission may already be
underway" (never blindly retried — see
`FollowUpSendRecord.send_attempted`'s docstring and
`_resolve_existing_send_record` below). A process crash between claiming
PENDING and calling `begin_transmission` leaves a row that is PROVABLY
still safe to retake; a crash AFTER `begin_transmission` succeeded is
indistinguishable from a still-live concurrent attempt and is always
resolved to the fail-closed terminal `UNCERTAIN` state, never retried
automatically.

**No other external side effect.** This module never mutates
`JobRecord.status`/`ApplicationStatus`, never calls Telegram, and never
fetches a URL. The ONLY external action anywhere in this module is the
one explicitly-approved `OutboundEmailProvider.send` call in
`send_follow_up`.
"""

import json
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.follow_up_approval_repository import (
    begin_transmission,
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
from app.db.follow_up_repository import get_thread_message_infos
from app.db.gmail_repository import get_message_by_id
from app.db.models import FollowUpApprovalRecord, FollowUpProposalRecord, FollowUpSendRecord
from app.db.repositories import get_job_by_id
from app.models.follow_up import FollowUpState
from app.providers.email.outbound_base import (
    EmailSendError,
    EmailSendOutcomeUnknownError,
    OutboundEmailProvider,
    OutboundMessage,
)
from app.services.follow_up import compute_fresh_follow_up_state
from app.services.follow_up_recipient import (
    FollowUpRecipientInvalidError,
    derive_canonical_recipient,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FollowUpAlreadyDecidedError",
    "FollowUpAlreadySentError",
    "FollowUpMissingRecipientError",
    "FollowUpNotApprovedError",
    "FollowUpProposalNotFoundError",
    "FollowUpProposalStaleAtSendTimeError",
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


class FollowUpProposalStaleAtSendTimeError(Exception):
    """S7E-002 (Codex remediation): send-time revalidation found the
    proposal is no longer eligible — the job's status changed, its Stage
    7B match/thread is no longer decisive or has changed, a newer reply
    arrived, the correspondence anchor is no longer current, or the
    recipient no longer matches the pinned, approved value. Mapped to 409.
    The underlying send attempt has already been marked FAILED (no
    transmission was attempted); this is never raised after the provider
    has actually been called.
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
        pinned_recipient=proposal.recipient,
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
    """S7E-008: the recipient is ALWAYS the approval's own PINNED value —
    never re-derived from `anchor_message` here. See module docstring for
    why (and `_revalidate_or_fail_closed` for the send-time re-check that
    the pinned value still matches a fresh derivation).
    """
    if not approval.pinned_recipient:
        raise FollowUpMissingRecipientError(
            f"follow_up_proposal_id={approval.follow_up_proposal_id!r} has no "
            "pinned_recipient on record"
        )
    references = tuple(json.loads(anchor_message.references_json))
    if anchor_message.message_id_header and anchor_message.message_id_header not in references:
        references = (*references, anchor_message.message_id_header)
    return OutboundMessage(
        to_address=approval.pinned_recipient,
        subject=approval.pinned_subject,
        body=approval.pinned_body,
        in_reply_to=anchor_message.message_id_header,
        references=references,
    )


def _resolve_existing_send_record(
    db: Session, *, account_key: str, proposal: FollowUpProposalRecord, record: FollowUpSendRecord
) -> FollowUpSendRecord:
    """S7E-010 (Codex remediation, crash/CAS recovery): dispatch on an
    ALREADY-EXISTING `FollowUpSendRecord` found by `claim_send_attempt`'s
    losing INSERT. See module docstring's "Crash/CAS recovery" section.
    """
    if record.status == "SENT":
        raise FollowUpAlreadySentError(
            f"follow_up_proposal_id={proposal.id!r} has already been sent"
        )
    if record.status == "UNCERTAIN":
        raise FollowUpSendOutcomeUncertainError(
            f"follow_up_proposal_id={proposal.id!r} has an uncertain prior send outcome; "
            "manual reconciliation is required, not an automatic retry"
        )
    if record.status == "PENDING":
        if not record.send_attempted:
            # PROVABLY pre-transmission (see FollowUpSendRecord.send_attempted's
            # docstring) — always safe for this request to take over. The
            # actual mutual-exclusion gate is begin_transmission, called by
            # the caller right after this returns.
            return record
        # Transmission may already be underway (a live concurrent request,
        # or a crash mid-send) — indistinguishable from here, so this is
        # NEVER retried. Fail closed to the terminal UNCERTAIN state and
        # check what the CAS actually did (it may lose to whichever
        # request genuinely owns this attempt finishing first).
        mark_send_uncertain(db, record, last_error="StrandedPendingTransmissionAttempted")
        current = get_send_for_proposal(db, account_key, proposal.id)
        if current is not None and current.status == "SENT":
            raise FollowUpAlreadySentError(
                f"follow_up_proposal_id={proposal.id!r} has already been sent"
            )
        if current is not None and current.status == "UNCERTAIN":
            raise FollowUpSendOutcomeUncertainError(
                f"follow_up_proposal_id={proposal.id!r} has an uncertain prior send outcome; "
                "manual reconciliation is required, not an automatic retry"
            )
        raise FollowUpSendInProgressError(
            f"A send attempt for follow_up_proposal_id={proposal.id!r} is already in progress"
        )
    # status == "FAILED": a legitimate retry — try to win the CAS back to
    # PENDING (send_attempted reset to False by retry_send_attempt).
    won_retry = retry_send_attempt(db, record)
    if not won_retry:
        raise FollowUpSendInProgressError(
            f"A concurrent retry for follow_up_proposal_id={proposal.id!r} is already in progress"
        )
    return record


def _claim_or_retry_send(
    db: Session,
    *,
    account_key: str,
    proposal: FollowUpProposalRecord,
    approval: FollowUpApprovalRecord,
) -> FollowUpSendRecord:
    """Wins (or refuses) the right to CONTEND for actually calling the
    outbound provider for this proposal — returns a `FollowUpSendRecord`
    guaranteed `status='PENDING', send_attempted=False` at read time, or
    raises. The caller MUST still win `begin_transmission` (S7E-002/010's
    real exclusivity gate) before revalidating or calling the provider —
    see module docstring's "Race safety" section.
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
    return _resolve_existing_send_record(
        db, account_key=account_key, proposal=proposal, record=record
    )


def _revalidate_or_fail_closed(
    db: Session,
    *,
    account_key: str,
    proposal: FollowUpProposalRecord,
    approval: FollowUpApprovalRecord,
    anchor_message,
    send_record: FollowUpSendRecord,
    settings: Settings,
    now: datetime,
) -> None:
    """S7E-002 (Codex remediation): the send-time revalidation gate — see
    module docstring's "Send-time revalidation" section for exactly what
    is re-checked and why. Called ONLY after this request has exclusively
    won `begin_transmission` (no TOCTOU race with another concurrent
    request — see "Race safety"), and strictly BEFORE the outbound
    provider is ever invoked. On any mismatch, marks `send_record` FAILED
    (transmission was never attempted) and raises
    `FollowUpProposalStaleAtSendTimeError` — never silently proceeds.
    """
    stale_reason: str | None = None

    job = get_job_by_id(db, proposal.job_id)
    if job is None:
        stale_reason = "the underlying job no longer exists"
    else:
        state = compute_fresh_follow_up_state(db, account_key, job, settings=settings, now=now)
        result = state.eligibility
        if result.eligibility != "ELIGIBLE":
            stale_reason = f"job is no longer follow-up eligible ({result.reason})"
        elif result.anchor_gmail_message_id != proposal.anchor_gmail_message_id:
            stale_reason = (
                "the thread's correspondence anchor has changed since this proposal was built"
            )
        elif state.thread_id != proposal.gmail_thread_id:
            stale_reason = (
                "the job's matched Gmail thread has changed since this proposal was built"
            )

    if stale_reason is None:
        try:
            fresh_recipient = derive_canonical_recipient(
                json.loads(anchor_message.to_addresses_json), account_key=account_key
            )
        except FollowUpRecipientInvalidError as exc:
            stale_reason = f"recipient is no longer safely derivable ({exc})"
        else:
            if fresh_recipient != approval.pinned_recipient:
                stale_reason = "recipient no longer matches the pinned, approved value"

    if stale_reason is not None:
        mark_send_failed(db, send_record, last_error="StaleAtSendTime")
        logger.warning(
            "follow_up_send_stale_at_send_time follow_up_proposal_id=%s",
            proposal.id,
        )
        raise FollowUpProposalStaleAtSendTimeError(
            f"follow_up_proposal_id={proposal.id!r} is no longer eligible to send: {stale_reason}"
        )


def _fail_closed_if_reply_raced_dispatch(
    db: Session,
    *,
    account_key: str,
    proposal: FollowUpProposalRecord,
    send_record: FollowUpSendRecord,
) -> None:
    """S7E-012 (Codex re-review, MEDIUM): the LAST check before
    `provider.send` — see module docstring's "`begin_transmission` does
    NOT protect against a Gmail sync writer" section for the full
    rationale. Re-reads the thread's current latest-OUTBOUND/latest-
    INBOUND state ONE more time (the same bounded query
    `_revalidate_or_fail_closed` uses via `compute_fresh_follow_up_state`)
    and fails closed if either the anchor is no longer the latest
    OUTBOUND message or a reply has arrived since — never silently
    proceeds. Deliberately does NOT re-check job status/thread ambiguity/
    recipient (already covered by `_revalidate_or_fail_closed` moments
    earlier); re-deriving those again here would only add latency to the
    exact window this function exists to shrink.
    """
    infos = get_thread_message_infos(db, account_key, proposal.gmail_thread_id)
    outbound = next((info for info in infos if info.direction == "OUTBOUND"), None)
    inbound = next((info for info in infos if info.direction == "INBOUND"), None)

    stale_reason: str | None = None
    if outbound is None or outbound.gmail_message_id != proposal.anchor_gmail_message_id:
        stale_reason = "the thread's correspondence anchor changed immediately before dispatch"
    elif inbound is not None and inbound.timestamp > outbound.timestamp:
        stale_reason = (
            "a reply was received immediately before dispatch "
            f"(gmail_message_id={inbound.gmail_message_id}); follow-up send aborted"
        )

    if stale_reason is not None:
        mark_send_failed(db, send_record, last_error="ReplyRacedDispatch")
        logger.warning(
            "follow_up_send_reply_raced_dispatch follow_up_proposal_id=%s",
            proposal.id,
        )
        raise FollowUpProposalStaleAtSendTimeError(
            f"follow_up_proposal_id={proposal.id!r} is no longer eligible to send: {stale_reason}"
        )


def send_follow_up(
    db: Session,
    account_key: str,
    follow_up_proposal_id: int,
    provider: OutboundEmailProvider,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> FollowUpSendRecord:
    """Send an APPROVED follow-up as a real Gmail message. See module
    docstring for the full send-gate contract. Raises one of
    `FollowUpProposalNotFoundError` / `FollowUpNotApprovedError` /
    `FollowUpMissingRecipientError` / `FollowUpAlreadySentError` /
    `FollowUpSendInProgressError` / `FollowUpProposalStaleAtSendTimeError`
    / `FollowUpSendFailedError` / `FollowUpSendOutcomeUncertainError`.
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

    # S7E-002/010: the exclusive gate — only the request that wins this
    # CAS may revalidate/send. See module docstring's "Race safety".
    won_attempt = begin_transmission(db, send_record)
    if not won_attempt:
        raise FollowUpSendInProgressError(
            f"A concurrent send attempt for follow_up_proposal_id={proposal.id!r} is "
            "already in progress"
        )

    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    _revalidate_or_fail_closed(
        db,
        account_key=account_key,
        proposal=proposal,
        approval=approval,
        anchor_message=anchor_message,
        send_record=send_record,
        settings=settings,
        now=now,
    )

    # S7E-012: the LAST gate before the outbound provider is ever called
    # — see module docstring and `_fail_closed_if_reply_raced_dispatch`'s
    # own docstring for why this is a distinct, separately-positioned
    # check from `_revalidate_or_fail_closed` above rather than the same
    # call repeated.
    _fail_closed_if_reply_raced_dispatch(
        db, account_key=account_key, proposal=proposal, send_record=send_record
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
