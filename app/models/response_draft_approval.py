"""Pydantic DTOs for Stage 7D human approval + send state.

INFORMATION/ACTION BOUNDARY — see app/services/response_draft_send.py's
module docstring for the full "NO APPROVAL = NO SEND" hard invariant
this subsystem enforces. A `ResponseDraftApproval` records a human
decision; it is never itself authorization for anything beyond the exact
`response_draft_id` it pins. A `ResponseDraftSendStatus` records the
outcome of an attempted send:

- `status="SENT"` — the outbound provider CONFIRMED the message was
  accepted for delivery.
- `status="UNCERTAIN"` — transmission was ATTEMPTED and the message MAY
  already have been accepted/delivered, but this project has no way to
  confirm OR rule that out (see
  app.providers.email.outbound_base.EmailSendOutcomeUnknownError's
  docstring). `status="SENT"` is therefore NOT the only state in which an
  email may have actually reached the recipient — `UNCERTAIN` requires
  manual reconciliation outside this project and must never be treated
  as safe to retry.
- `status="PENDING"` — a send attempt is claimed/in progress; the
  outbound provider may not have been called yet, or a call to it may be
  in flight RIGHT NOW. Delivery outcome is not yet final either way.
- `status="FAILED"` — a DEFINITE pre-transmission failure (auth/
  connection/message-construction — see
  app.providers.email.outbound_base.EmailSendConnectionError/
  EmailSendAuthError's docstrings): transmission never started, so "not
  sent" is provably true, and a retry under the existing send gate is
  safe.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    """POST /response-drafts/{draft_id}/decision body. Stage 12:
    `extra="forbid"` -- an unknown field (e.g. a client believing it can
    pin a "version") is rejected with 422 rather than silently ignored;
    the draft id in the path is the only revision identity.
    """

    model_config = ConfigDict(extra="forbid")

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
    # Stage 12: False when a newer analysis, a candidate-profile edit or a
    # generator upgrade has superseded this revision -- it can then no
    # longer be APPROVED (an approval recorded while it was current still
    # authorizes its pinned content; see app.services.response_draft_send).
    is_current_revision: bool
    approval: ResponseDraftApproval | None
    send: ResponseDraftSendStatus | None


class PendingResponseDraft(BaseModel):
    """Stage 12: one entry of GET /response-drafts/pending -- the newest
    PROPOSED draft revision of an inbound message that has no human
    decision yet and whose message has no live (PENDING/SENT/UNCERTAIN)
    reply. Carries the Stage 7B association evidence a reviewer needs to
    judge it (`match_type` is APPLICATION/JOB_ONLY/AMBIGUOUS/UNMATCHED --
    an AMBIGUOUS or UNMATCHED draft never carries a `matched_job_id`).
    Never includes the draft's subject/body or any message content: read
    GET /gmail/messages/{id}/response-draft for that. Listing performs no
    generation, decision or send.
    """

    response_draft_id: int
    gmail_message_id: int
    analysis_id: int
    matched_job_id: int | None
    match_type: str | None
    match_confidence: str | None
    classification: str
    candidate_profile_version: int
    generator_version: str
    provider: str
    is_current_revision: bool
    created_at: datetime
