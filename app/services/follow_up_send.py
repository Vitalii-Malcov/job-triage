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

**`begin_transmission` does NOT protect against a Gmail sync writer —
closed for real by a shared thread guard (S7E-013, Codex re-review, final
safety fix).** The CAS above is scoped entirely to `follow_up_sends` rows
— it says nothing about, and is never touched by, `POST /gmail/sync`
(`app.services.gmail_inbox.GmailInboxService`), which persists new
`GmailMessageRecord` rows on its own, completely independent
schedule/connection. An earlier remediation round (S7E-012) tried to
close this with a second, minimal re-check positioned right before
`provider.send` — that only NARROWED the window to the gap between that
read returning and the provider call starting; it could never
mathematically close it, because nothing actually stopped a Gmail sync
from committing in that gap.

`send_follow_up` now acquires `proposal.gmail_thread_id`'s guard (
`app.db.gmail_repository.acquire_thread_lock`/`wait_for_thread_lock` — a
generic, Gmail-thread-scoped mutual-exclusion primitive that knows
nothing about follow-ups or jobs) BEFORE revalidating, holds it across
revalidation AND the provider call, and releases it in a `finally` no
matter how the guarded section exits. `app.db.gmail_repository.upsert_message`
acquires the SAME lock (on the same `GmailThreadRecord.id`) before its
own INSERT + commit of a new message. The two are therefore mutually
exclusive by construction: a Gmail sync that reaches the lock first
blocks this request from ever starting its guarded window (fails closed
to `FollowUpSendInProgressError` after `lock_wait_seconds` — see
`begin_transmission`'s own crash-recovery discussion below for why a
timeout here safely resolves to `FAILED`, not `UNCERTAIN`: no
transmission was ever attempted); a send that reaches it first makes any
concurrent Gmail sync's INSERT for this thread block (bounded by the
lock's own TTL, so a crashed holder can never deadlock the mailbox
forever) until the send releases it, by which point the send has already
either transmitted or failed closed. `_fail_closed_if_reply_raced_dispatch`
(S7E-012) is kept as cheap, redundant defense-in-depth inside the guarded
section — no longer the primary defense.

**Generic, layer-respecting primitive — no job/application logic in
Stage 7A.** The lock lives on `GmailThreadRecord` and is implemented in
`app.db.gmail_repository` (Stage 7A) purely as "who currently holds this
thread, until when" — it has no concept of follow-ups, approvals, or
jobs. Stage 7E imports and uses it exactly like any other consumer would;
Stage 7A's own code never imports anything from `app.services.follow_up*`
(see app/services/gmail_inbox.py's "zero job/application linkage"
constraint, which this fix does not touch).

**Lease renewal / heartbeat — the lock must never expire under a still-
live sender (S7E-015, Codex re-review, final lock hardening).**
`app.providers.email.smtp.SMTP_OPERATION_TIMEOUT_SECONDS` bounds each
INDIVIDUAL blocking socket call, not the CUMULATIVE wall-clock time of
`provider.send()` as a whole (see that constant's own honestly-documented
limitation) — so a fixed `THREAD_LOCK_TTL_SECONDS` lease could still, in
principle, lapse while a legitimately still-running send holds it. Rather
than assume "send always finishes well within one TTL window",
`send_follow_up` runs a `_ThreadLockHeartbeat` background thread for the
ENTIRE guarded section (revalidation through `provider.send()`): every
`heartbeat_interval_seconds` (a fraction of the lease's own TTL, so
several renewal attempts happen per lease window), it calls
`app.db.gmail_repository.renew_thread_lock` for the SAME `holder` token —
a DEDICATED CAS, deliberately NOT `acquire_thread_lock` (S7E-016, Codex
re-review, correctness fix — see that function's own docstring for the
full rationale): `acquire_thread_lock` treats an EXPIRED lease as
free-for-the-taking, which is exactly right for INITIAL/recovery
acquisition but WRONG for a renewal — a heartbeat that "renewed" via that
function could silently succeed on a lease that had already lapsed
(nobody else happening to have grabbed it yet is not proof anyone was
protected during the gap). `renew_thread_lock` instead requires the
lease to still be LIVE (`lock_expires_at >= now`) at the moment of
renewal, on top of `lock_holder == holder` — it fails the instant the
lease has expired, even if no other holder ever took it. A renewal that
fails sets a `lock_lost` flag and the heartbeat stops immediately — it
never falls back to `acquire_thread_lock` to "reacquire", and never
keeps renewing on the assumption ownership might somehow still be
intact.

`send_follow_up` checks `lock_lost` immediately after `provider.send()`
returns SUCCESSFULLY (the only path where silently trusting exclusivity
would matter — see below) and, if set, does NOT call `mark_send_sent`:
it calls `mark_send_uncertain` and raises `FollowUpSendOutcomeUncertainError`
instead. This is deliberate, not paranoid: if the lease genuinely lapsed
while the provider call was still in flight, a concurrent Gmail sync
could have committed a new INBOUND reply for this exact thread during
that gap, unnoticed — the message may well have been delivered
correctly, but this project can no longer PROVE the correspondence state
it was approved against stayed exclusive for the whole window, so it
fails closed exactly like a genuinely ambiguous provider outcome would.
A `provider.send()` that raises `EmailSendConnectionError`/`EmailSendAuthError`
(a DEFINITE pre-transmission failure) is unaffected by `lock_lost` either
way — no transmission was ever attempted, so whether exclusivity lapsed
during that failed attempt is moot; `EmailSendOutcomeUnknownError` is
already the fail-closed terminal state regardless.

The heartbeat runs on its OWN `Session` (bound to the same engine as the
caller's `db` via `sessionmaker(bind=db.get_bind())`) in its OWN
background thread — SQLAlchemy Sessions are not safe to share across
threads. It is always a daemon thread and is always `stop()`-ped in the
outer `finally` alongside `release_thread_lock`, so it can never outlive
`send_follow_up` itself; if the whole process dies mid-send, the
heartbeat thread dies with it, nothing renews, and the lease simply
expires on its own schedule — exactly the crash-recovery behavior
`THREAD_LOCK_TTL_SECONDS` was already designed around.

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
import threading
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

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
from app.db.gmail_repository import (
    THREAD_LOCK_DEFAULT_MAX_WAIT_SECONDS,
    THREAD_LOCK_TTL_SECONDS,
    GmailThreadLockTimeoutError,
    get_message_by_id,
    new_thread_lock_holder_token,
    release_thread_lock,
    renew_thread_lock,
    wait_for_thread_lock,
)
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


class _ThreadLockHeartbeat:
    """S7E-015 (Codex re-review, final lock hardening): periodically
    renews `thread_id`'s guard on `holder`'s behalf for as long as
    `provider.send()` is running — see module docstring's "Lease renewal
    / heartbeat" section for the full rationale. Runs on its OWN Session
    (SQLAlchemy Sessions are never safe to share across threads) bound to
    the same engine as the caller's `db`, in its own daemon thread.

    `lock_lost` (a `threading.Event`) is set the moment a renewal attempt
    fails — i.e. `app.db.gmail_repository.renew_thread_lock` reports the
    lease was no longer live for `holder` at renewal time (S7E-016: NOT
    `acquire_thread_lock`, which would wrongly treat an already-expired
    lease as free-for-the-taking) — and the heartbeat stops trying
    immediately afterward. The caller MUST check
    `lock_lost` after `provider.send()` returns and must never treat a
    successful send as trustworthy exclusivity-wise if it is set.
    """

    def __init__(
        self,
        db: Session,
        thread_id: int,
        *,
        holder: str,
        ttl_seconds: float,
        interval_seconds: float,
    ) -> None:
        self._session_factory = sessionmaker(bind=db.get_bind())
        self._thread_id = thread_id
        self._holder = holder
        self._ttl_seconds = ttl_seconds
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self.lock_lost = threading.Event()
        self._thread = threading.Thread(target=self._run, name="follow-up-send-lock-heartbeat")
        self._thread.daemon = True

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        session = self._session_factory()
        try:
            # `Event.wait(timeout)` both sleeps AND doubles as the stop
            # signal check — returns True (skips the renewal below and
            # exits the loop) the instant `stop()` sets it, so this
            # thread never outlives the guarded section by more than a
            # single wait tick.
            while not self._stop_event.wait(self._interval_seconds):
                # AUD-003 (Astra R2): a renewal attempt that RAISES (a
                # transient DB error, a lost connection, anything) is
                # NOT distinguishable from "ownership lost" for this
                # heartbeat's purposes -- either way, this thread can no
                # longer PROVE the lease is still held, and an uncaught
                # exception here would otherwise just kill this daemon
                # thread silently (Python does not propagate a thread's
                # exception to the thread that started it), leaving
                # `lock_lost` never set and the caller wrongly believing
                # renewal was still happening. Fail closed exactly like
                # an explicit "not renewed" result below.
                try:
                    # S7E-016: `renew_thread_lock`, NEVER
                    # `acquire_thread_lock` — see that function's
                    # docstring for exactly why the two are not
                    # interchangeable here. A renewal must fail the
                    # instant the lease has expired, even if nobody else
                    # has taken it yet; it must never silently resume as
                    # though ownership had been continuous.
                    renewed = renew_thread_lock(
                        session,
                        self._thread_id,
                        holder=self._holder,
                        ttl_seconds=self._ttl_seconds,
                    )
                except Exception as exc:
                    # BOUND-XXX (api-boundaries hardening r1, privacy
                    # second-pass): this used to be
                    # `logger.warning(..., exc_info=True)` -- the exact
                    # leakage class already fixed for this class's own
                    # sibling, `app.services.automation._RunLeaseHeartbeat`
                    # (see that class's S8A-004 comment: `exc_info=True`
                    # logs the full traceback INCLUDING the exception's
                    # own str(exc), which for a DB-layer failure can embed
                    # a bound SQL parameter value, e.g. an account_key).
                    # Only the event name, thread id, and sanitized type
                    # name are logged now, matching that established
                    # convention.
                    logger.warning(
                        "follow_up_send_lock_heartbeat_renewal_error gmail_thread_id=%s "
                        "error_type=%s",
                        self._thread_id,
                        type(exc).__name__,
                    )
                    self.lock_lost.set()
                    return
                if not renewed:
                    # Ownership is gone — never keep renewing on the
                    # assumption it might come back; the caller's
                    # exclusivity guarantee for this send is already
                    # broken and must be reported, not silently retried.
                    logger.warning(
                        "follow_up_send_lock_heartbeat_lost_ownership gmail_thread_id=%s",
                        self._thread_id,
                    )
                    self.lock_lost.set()
                    return
        finally:
            session.close()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self._interval_seconds + 1.0)


def send_follow_up(
    db: Session,
    account_key: str,
    follow_up_proposal_id: int,
    provider: OutboundEmailProvider,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    lock_wait_seconds: float = THREAD_LOCK_DEFAULT_MAX_WAIT_SECONDS,
    lock_ttl_seconds: float = THREAD_LOCK_TTL_SECONDS,
    heartbeat_interval_seconds: float | None = None,
) -> FollowUpSendRecord:
    """Send an APPROVED follow-up as a real Gmail message. See module
    docstring for the full send-gate contract, including the S7E-015
    lease-renewal heartbeat that runs for the ENTIRE guarded section so
    `lock_ttl_seconds` never has to be gambled against `provider.send()`'s
    actual duration. `lock_ttl_seconds` defaults to the safe production
    value (`app.db.gmail_repository.THREAD_LOCK_TTL_SECONDS`) —
    overridable only so tests can exercise the lease-expiry boundary
    quickly (see tests/test_gmail_repository.py's
    `test_deliberately_slow_provider_still_completes_and_lease_recovers`);
    production callers should never lower it below what
    `app.providers.email.smtp.SMTP_OPERATION_TIMEOUT_SECONDS` needs as
    margin. `heartbeat_interval_seconds` defaults to a third of
    `lock_ttl_seconds` (several renewals per lease window) — overridable
    for the same test-speed reason. Raises one of
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

    # S7E-013 (Codex re-review, final safety fix): acquire the SAME
    # generic per-thread guard app.db.gmail_repository.upsert_message
    # holds while persisting a new message — see module docstring's
    # "begin_transmission does NOT protect against a Gmail sync writer"
    # section. Held across the ENTIRE revalidate-then-dispatch window
    # below (released in the `finally`), so a Gmail sync cannot commit a
    # new message for this thread until this request is done with it,
    # and a Gmail sync already mid-persist for this thread blocks this
    # request from ever starting its guarded window.
    lock_holder = new_thread_lock_holder_token(f"follow_up_send:{send_record.id}")
    try:
        wait_for_thread_lock(
            db,
            proposal.gmail_thread_id,
            holder=lock_holder,
            max_wait_seconds=lock_wait_seconds,
            ttl_seconds=lock_ttl_seconds,
        )
    except GmailThreadLockTimeoutError as exc:
        mark_send_failed(db, send_record, last_error="ThreadLockTimeout")
        logger.warning(
            "follow_up_send_thread_lock_timeout follow_up_proposal_id=%s",
            proposal.id,
        )
        raise FollowUpSendInProgressError(
            f"Could not acquire the Gmail thread guard for follow_up_proposal_id="
            f"{proposal.id!r}; a Gmail sync or another send is currently using it"
        ) from exc

    # S7E-015: renew the lease periodically for as long as the guarded
    # section runs — see module docstring's "Lease renewal / heartbeat"
    # section. `heartbeat_interval_seconds` intentionally derives from
    # whatever `lock_ttl_seconds` THIS call actually uses (not the
    # module default) so a test overriding `lock_ttl_seconds` to
    # something tiny gets a correspondingly fast heartbeat unless it
    # ALSO overrides `heartbeat_interval_seconds` explicitly.
    effective_heartbeat_interval = (
        heartbeat_interval_seconds
        if heartbeat_interval_seconds is not None
        else lock_ttl_seconds / 3
    )
    heartbeat = _ThreadLockHeartbeat(
        db,
        proposal.gmail_thread_id,
        holder=lock_holder,
        ttl_seconds=lock_ttl_seconds,
        interval_seconds=effective_heartbeat_interval,
    )
    heartbeat.start()
    try:
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

        # S7E-012: a cheap, redundant last check kept as defense-in-depth
        # alongside the S7E-013 thread guard above — see module docstring
        # and `_fail_closed_if_reply_raced_dispatch`'s own docstring.
        _fail_closed_if_reply_raced_dispatch(
            db, account_key=account_key, proposal=proposal, send_record=send_record
        )

        # AUD-003 (Astra R2): if the heartbeat already lost the lease
        # BEFORE the outbound provider is ever called (e.g. a slow
        # revalidation step above let a renewal tick fail first), the
        # outbound provider must never be invoked at all — calling it
        # now would be a real external side effect with NO exclusivity
        # guarantee behind it whatsoever, not even the "maybe it raced"
        # uncertainty the POST-send check below exists for. Nothing has
        # been sent yet, so this is safe to retry: FAILED (not
        # UNCERTAIN), mirroring the EmailSendError handling below.
        if heartbeat.lock_lost.is_set():
            mark_send_failed(db, send_record, last_error="ThreadLockOwnershipLostBeforeDispatch")
            logger.warning(
                "follow_up_send_lock_lost_before_dispatch follow_up_proposal_id=%s",
                proposal.id,
            )
            raise FollowUpSendFailedError(
                f"follow_up_proposal_id={follow_up_proposal_id!r}: lost exclusive "
                "ownership of the Gmail thread guard before the outbound provider "
                "was ever called; nothing was sent, safe to retry"
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

        # S7E-015: the outbound provider reported success, but if the
        # heartbeat ever lost ownership of the lease WHILE provider.send
        # was still in flight, exclusivity for this guarded window is no
        # longer provable — a concurrent Gmail sync could have committed
        # a new reply for this thread unnoticed during that gap. Fail
        # closed to UNCERTAIN rather than silently trusting SENT; a
        # DEFINITE pre-transmission failure above is unaffected (no
        # transmission was ever attempted either way).
        if heartbeat.lock_lost.is_set():
            mark_send_uncertain(db, send_record, last_error="ThreadLockOwnershipLost")
            logger.warning(
                "follow_up_send_lock_lost_during_dispatch follow_up_proposal_id=%s",
                proposal.id,
            )
            raise FollowUpSendOutcomeUncertainError(
                f"follow_up_proposal_id={follow_up_proposal_id!r}: lost exclusive "
                "ownership of the Gmail thread guard while the outbound provider was "
                "still running; the message may have been sent but exclusivity "
                "cannot be proven"
            )

        mark_send_sent(db, send_record, provider_message_id=result.provider_message_id)
        logger.info("follow_up_sent follow_up_proposal_id=%s", proposal.id)
        return send_record
    finally:
        heartbeat.stop()
        release_thread_lock(db, proposal.gmail_thread_id, holder=lock_holder)


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
