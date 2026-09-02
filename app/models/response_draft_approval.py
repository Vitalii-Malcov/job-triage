"""Pydantic DTOs for Stage 7D human approval + send state.

INFORMATION/ACTION BOUNDARY — see app/services/response_draft_send.py's
module docstring for the full "NO APPROVAL = NO SEND" hard invariant
this subsystem enforces. A `ResponseDraftApproval` records a human
decision; it is never itself authorization for anything beyond the exact
`response_draft_id` it pins. A `ResponseDraftSendStatus` records the
outcome of an attempted send — `status="SENT"` is the only state in
which this project has ever transmitted an email on the user's behalf.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ApprovalDecision = Literal["APPROVED", "REJECTED"]
SendStatus = Literal["PENDING", "SENT", "FAILED", "UNCERTAIN"]


class ResponseDraftApproval(BaseModel):
    """POST /response-drafts/{draft_id}/decision's response — one
    immutable approval/rejection decision (see
    app.db.models.ResponseDraftApprovalRecord's docstring).
    """

    id: int
    response_draft_id: int
    gmail_message_id: int
    decision: ApprovalDecision
    decision_note: str | None
    pinned_subject: str
    pinned_body: str
    decided_at: datetime


class ResponseDraftApprovalRequest(BaseModel):
    """POST /response-drafts/{draft_id}/decision body."""

    decision: ApprovalDecision
    note: str | None = Field(default=None, max_length=2000)


class ResponseDraftSendStatus(BaseModel):
    """POST /response-drafts/{draft_id}/send and part of GET
    /response-drafts/{draft_id}/state's response — the outcome of the
    most recent send attempt for one response draft (see
    app.db.models.ResponseDraftSendRecord's docstring for the exact
    PENDING/SENT/FAILED/UNCERTAIN state machine). `status == "UNCERTAIN"`
    means transmission was attempted but delivery could not be confirmed
    OR ruled out — it is terminal and never auto-retried; a further
    POST .../send for the same draft is refused.
    """

    id: int
    response_draft_id: int
    gmail_message_id: int
    status: SendStatus
    attempt_count: int
    provider_message_id: str | None
    last_error: str | None
    sent_at: datetime | None


class ResponseDraftState(BaseModel):
    """GET /response-drafts/{draft_id}/state's response — the combined
    approval/send state for one exact response-draft revision, so a
    caller never has to issue two separate lookups to answer "can this
    be sent / has it been sent". `approval`/`send` are `None` when no
    decision/send attempt has been recorded yet for this draft.
    """

    response_draft_id: int
    gmail_message_id: int
    draft_status: str
    approval: ResponseDraftApproval | None
    send: ResponseDraftSendStatus | None
