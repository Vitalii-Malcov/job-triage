from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Direction = Literal["INBOUND", "OUTBOUND"]


class GmailAttachment(BaseModel):
    """Attachment metadata only (Stage 7A) — content is never downloaded,
    stored, or opened. See app.providers.email.base.ParsedAttachment.
    """

    filename: str | None = None
    content_type: str
    size: int | None = None


class GmailMessage(BaseModel):
    """One persisted, read-only-synced Gmail message.

    Contains personal correspondence content (subject/body/addresses) —
    never logged (see app.services.gmail_inbox's module docstring) and
    only ever returned behind API-key auth.
    """

    id: int
    thread_id: int
    message_id_header: str | None
    in_reply_to: str | None
    references: list[str]
    from_address: str | None
    from_display_name: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    sent_at: datetime | None
    received_at: datetime
    direction: Direction
    body_plain: str
    body_truncated: bool
    has_html: bool
    attachments: list[GmailAttachment]
    created_at: datetime


class GmailThread(BaseModel):
    """A neutral (non-Gmail-native) thread grouping — see
    app.db.models.GmailThreadRecord's docstring for `thread_key`'s
    derivation and documented limitation.
    """

    id: int
    thread_key: str
    subject: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class GmailSyncResult(BaseModel):
    """Structured summary of one POST /gmail/sync run. Never includes any
    message content — only counts (spec section 14).
    """

    fetched: int
    created: int
    duplicates: int
    skipped: int
    failed: int
