"""Pydantic DTOs for Stage 7C response draft proposals.

INFORMATION ONLY — see app/services/response_draft.py's module docstring
for the full hard boundary (no send, no Gmail draft creation, no
mailbox mutation, no ApplicationStatus mutation, no external action of
any kind). A `ResponseDraft` is a stored SUGGESTION for a human to
review, edit, and send manually (Stage 7D, not this stage, owns
approval/send) — never authorization to act automatically.
`requires_human_review` is always `True` for every row this stage
creates.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ResponseDraftStatus = Literal["PROPOSED", "NO_RESPONSE_RECOMMENDED"]
ResponseDraftLanguage = Literal["de", "en"]


class ResponseDraft(BaseModel):
    """POST /gmail/messages/{id}/response-draft and
    GET /gmail/messages/{id}/response-draft's response — one immutable
    response-draft revision (see app.db.models.ResponseDraftRecord's
    docstring).

    `subject`/`body`/`language` are populated only when
    `status == "PROPOSED"`; `reason` is populated only when
    `status == "NO_RESPONSE_RECOMMENDED"`. `missing_fields` lists every
    fact the generator could not determine and therefore represented as
    a placeholder in `body` rather than inventing — always empty for
    `NO_RESPONSE_RECOMMENDED`.
    """

    id: int
    gmail_message_id: int
    analysis_id: int
    analysis_version: int
    matched_job_id: int | None
    classification: str
    status: ResponseDraftStatus
    reason: str | None
    subject: str | None
    body: str | None
    language: ResponseDraftLanguage | None
    missing_fields: list[str]
    provider: str
    generator_version: str
    requires_human_review: bool
    created_at: datetime
