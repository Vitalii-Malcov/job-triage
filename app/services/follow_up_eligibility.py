"""Pure, deterministic Stage 7E follow-up eligibility rule — no DB/network
access, mirrors app.services.email_matching's "pure function, bounded
input supplied by the caller" style.

**Never infers application age from `JobRecord.first_seen_at`/
`last_seen_at` (CLAUDE.md hard requirement).** Every timestamp this module
reasons about is a real Gmail correspondence timestamp
(`GmailMessageRecord.sent_at`/`received_at`, see `ThreadMessageInfo`) —
`JobRecord` itself is never even passed in beyond its `status` string.

**If correspondence is missing or ambiguous, this returns NOT_ELIGIBLE —
it never guesses.** See `evaluate_follow_up_eligibility`'s docstring for
the exact ordered checks; each one is a distinct, honestly-reported
reason, never a silent fallback.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Direction = Literal["INBOUND", "OUTBOUND"]
FollowUpEligibility = Literal["ELIGIBLE", "NOT_ELIGIBLE"]

# A JobRecord at one of these statuses is either not yet applied, or has
# already moved past the point where a candidate-initiated "checking in"
# follow-up makes sense (spec: "job is not
# INTERVIEW/REJECTED/OFFER/WITHDRAWN"). Only APPLIED is followup-eligible.
_ELIGIBLE_JOB_STATUS = "APPLIED"


@dataclass(frozen=True)
class ThreadMessageInfo:
    """One already-persisted `GmailMessageRecord` from the job's matched
    Gmail thread — direction/timestamp only, the minimum this module
    needs. `timestamp` is the message's own `sent_at` if known, else the
    sync's `received_at` (see app.db.follow_up_repository for how this is
    derived) — never `JobRecord.first_seen_at`/`last_seen_at`.
    """

    gmail_message_id: int
    direction: Direction
    timestamp: datetime


@dataclass(frozen=True)
class FollowUpEligibilityResult:
    eligibility: FollowUpEligibility
    reason: str
    anchor_gmail_message_id: int | None
    due_at: datetime | None


def evaluate_follow_up_eligibility(
    *,
    job_status: str,
    matched_thread_count: int,
    thread_messages: list[ThreadMessageInfo],
    follow_up_delay: timedelta,
    now: datetime,
) -> FollowUpEligibilityResult:
    """Ordered, hard-stopping checks — the first one that fails decides
    the (honest, specific) NOT_ELIGIBLE reason:

    1. `job_status != "APPLIED"` — covers both "not yet applied" and every
       terminal/later-stage status (INTERVIEW/REJECTED/OFFER/WITHDRAWN)
       in one check, since APPLIED is the only followup-eligible status.
    2. `matched_thread_count != 1` — zero means no Stage 7B-matched Gmail
       thread/message exists for this job at all; more than one means the
       job's correspondence is ambiguously spread across multiple
       threads — never guess which one is authoritative.
    3. No `OUTBOUND` message in `thread_messages` — spec: "a real prior
       OUTBOUND message" is a hard precondition; a job with only inbound
       correspondence (or none at all) is never eligible.
    4. A later `INBOUND` message exists (`timestamp` strictly after the
       latest `OUTBOUND` message's own `timestamp`) — a reply was already
       received; the follow-up is suppressed.
    5. `now < due_at` (`due_at` = latest outbound message's `timestamp` +
       `follow_up_delay`) — the configured delay has not elapsed yet.

    Only when all five pass is the result `ELIGIBLE`, naming the latest
    outbound message as `anchor_gmail_message_id` — the correspondence
    anchor app.db.models.FollowUpProposalRecord's `UNIQUE(account_key,
    anchor_gmail_message_id)` dedups on. This function never checks
    whether a proposal already exists for that anchor — that idempotency
    is the caller's job (app.services.follow_up), via a DB-level
    get-or-create, exactly like every other idempotent write in this
    project.
    """
    if job_status != _ELIGIBLE_JOB_STATUS:
        return FollowUpEligibilityResult(
            eligibility="NOT_ELIGIBLE",
            reason=f"Job status is {job_status!r}, not {_ELIGIBLE_JOB_STATUS!r}.",
            anchor_gmail_message_id=None,
            due_at=None,
        )

    if matched_thread_count == 0:
        return FollowUpEligibilityResult(
            eligibility="NOT_ELIGIBLE",
            reason="No Stage 7B-matched Gmail thread/message exists for this job.",
            anchor_gmail_message_id=None,
            due_at=None,
        )
    if matched_thread_count > 1:
        return FollowUpEligibilityResult(
            eligibility="NOT_ELIGIBLE",
            reason=(
                "This job's correspondence is ambiguous: matched across "
                f"{matched_thread_count} different Gmail threads."
            ),
            anchor_gmail_message_id=None,
            due_at=None,
        )

    outbound_messages = sorted(
        (m for m in thread_messages if m.direction == "OUTBOUND"),
        key=lambda m: (m.timestamp, m.gmail_message_id),
    )
    if not outbound_messages:
        return FollowUpEligibilityResult(
            eligibility="NOT_ELIGIBLE",
            reason="No real prior OUTBOUND message exists in the job's matched thread.",
            anchor_gmail_message_id=None,
            due_at=None,
        )

    anchor = outbound_messages[-1]

    later_inbound_reply = any(
        m.direction == "INBOUND" and m.timestamp > anchor.timestamp for m in thread_messages
    )
    if later_inbound_reply:
        return FollowUpEligibilityResult(
            eligibility="NOT_ELIGIBLE",
            reason=(
                "A reply was received after the latest outbound message "
                f"(id={anchor.gmail_message_id}); follow-up is suppressed."
            ),
            anchor_gmail_message_id=anchor.gmail_message_id,
            due_at=None,
        )

    due_at = anchor.timestamp + follow_up_delay
    if now < due_at:
        return FollowUpEligibilityResult(
            eligibility="NOT_ELIGIBLE",
            reason=f"Follow-up delay has not elapsed yet; due at {due_at.isoformat()}.",
            anchor_gmail_message_id=anchor.gmail_message_id,
            due_at=due_at,
        )

    return FollowUpEligibilityResult(
        eligibility="ELIGIBLE",
        reason=(
            f"No reply since outbound message id={anchor.gmail_message_id} sent at "
            f"{anchor.timestamp.isoformat()}; follow-up delay elapsed at {due_at.isoformat()}."
        ),
        anchor_gmail_message_id=anchor.gmail_message_id,
        due_at=due_at,
    )
