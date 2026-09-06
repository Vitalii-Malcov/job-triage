"""Pydantic DTOs for Stage 7E follow-up proposals + human approval/send
state.

INFORMATION/ACTION BOUNDARY — see app/services/follow_up.py and
app/services/follow_up_send.py's module docstrings for the full "NO
APPROVAL = NO FOLLOW-UP SEND" hard invariant this subsystem enforces,
mirroring Stage 7C/7D exactly (`FollowUpProposal` is a stored suggestion;
`FollowUpApproval` records a human decision; `FollowUpSendStatus` records
the outcome of an attempted send, with the same
PENDING/SENT/FAILED/UNCERTAIN semantics as `ResponseDraftSendStatus` —
see that model's docstring for what each value means).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

FollowUpLanguage = Literal["de", "en"]
FollowUpEligibility = Literal["ELIGIBLE", "NOT_ELIGIBLE"]
FollowUpApprovalDecision = Literal["APPROVED", "REJECTED"]
FollowUpSendStatusValue = Literal["PENDING", "SENT", "FAILED", "UNCERTAIN"]


class FollowUpProposal(BaseModel):
    """GET /follow-ups/{id} and the `proposal` field of
    `FollowUpEvaluationResult` — one immutable follow-up proposal (see
    app.db.models.FollowUpProposalRecord's docstring).
    """

    id: int
    job_id: int
    gmail_thread_id: int
    anchor_gmail_message_id: int
    eligibility_reason: str
    due_at: datetime
    subject: str
    body: str
    language: FollowUpLanguage
    missing_fields: list[str]
    provider: str
    generator_version: str
    status: Literal["PROPOSED"]
    requires_human_review: bool
    created_at: datetime


class FollowUpEvaluationResult(BaseModel):
    """One job's eligibility outcome — returned by both
    POST /follow-ups/evaluate (one entry per scanned APPLIED job) and
    GET /jobs/{job_id}/follow-up/evaluate (a single job). `proposal` is
    populated only when `eligibility == "ELIGIBLE"`; `created` further
    distinguishes a brand-new proposal (`True`) from an idempotent
    re-evaluation of an already-proposed anchor (`False`) — see
    app.db.models.FollowUpProposalRecord's docstring for why re-evaluating
    the same anchor is always safe and never duplicates a row.
    """

    job_id: int
    eligibility: FollowUpEligibility
    reason: str
    anchor_gmail_message_id: int | None
    due_at: datetime | None
    proposal: FollowUpProposal | None
    created: bool | None


class FollowUpScanSummary(BaseModel):
    """POST /follow-ups/evaluate's response — a bounded scan across
    tracked APPLIED jobs (see app.services.follow_up.FOLLOW_UP_JOB_SCAN_LIMIT).
    No background scheduler exists in this project (spec requirement) —
    this endpoint must be triggered manually, and `results` reports the
    ELIGIBLE/NOT_ELIGIBLE outcome for every job it actually scanned.
    """

    scanned: int
    eligible: int
    proposals_created: int
    results: list[FollowUpEvaluationResult]


class FollowUpApproval(BaseModel):
    """POST /follow-ups/{id}/decision's response — one immutable
    approval/rejection decision (see
    app.db.models.FollowUpApprovalRecord's docstring).
    """

    id: int
    follow_up_proposal_id: int
    gmail_message_id: int
    decision: FollowUpApprovalDecision
    decision_note: str | None
    pinned_subject: str
    pinned_body: str
    decided_at: datetime


class FollowUpApprovalRequest(BaseModel):
    """POST /follow-ups/{id}/decision body."""

    decision: FollowUpApprovalDecision
    note: str | None = Field(default=None, max_length=2000)


class FollowUpSendStatus(BaseModel):
    """POST /follow-ups/{id}/send and part of GET /follow-ups/{id}/state's
    response — the outcome of the most recent send attempt for one
    follow-up proposal (see app.db.models.FollowUpSendRecord's docstring
    for the exact PENDING/SENT/FAILED/UNCERTAIN state machine).
    `status == "UNCERTAIN"` is terminal and never auto-retried; a further
    POST here for the same proposal is refused.
    """

    id: int
    follow_up_proposal_id: int
    gmail_message_id: int
    status: FollowUpSendStatusValue
    attempt_count: int
    provider_message_id: str | None
    last_error: str | None
    sent_at: datetime | None


class FollowUpState(BaseModel):
    """GET /follow-ups/{id}/state's response — the combined
    approval/send state for one follow-up proposal, so a caller never has
    to issue two separate lookups. `approval`/`send` are `None` when no
    decision/send attempt has been recorded yet.
    """

    follow_up_proposal_id: int
    approval: FollowUpApproval | None
    send: FollowUpSendStatus | None
