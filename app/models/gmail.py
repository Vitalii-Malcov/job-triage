from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Direction = Literal["INBOUND", "OUTBOUND"]


class GmailAttachment(BaseModel):
    """Attachment metadata only (Stage 7A).

    True, unconditionally: attachment content is never PERSISTED, never
    OPENED/rendered, and never analyzed as business/correspondence
    content — only this bounded metadata is kept.

    NOT true unconditionally: attachment BYTES may still be TRANSFERRED
    from the IMAP server as part of the full-message `BODY.PEEK[]` fetch
    (bounded by MAX_RAW_MESSAGE_SIZE — see
    app.providers.email.base.ParsedAttachment's docstring, GMAIL-006).
    Stage 7A does not use section-level partial IMAP fetches to avoid
    that transfer, and computing `size` requires briefly decoding the
    part's payload; the decoded bytes are discarded immediately after
    measuring their length.
    """

    filename: str | None = None
    content_type: str
    size: int | None = None


class GmailMessage(BaseModel):
    """One persisted, read-only-synced Gmail message — the FULL detail
    representation (GET /gmail/messages/{id} only, GMAIL-007).

    Contains personal correspondence content (subject/body/addresses) —
    never logged (see app.services.gmail_inbox's module docstring) and
    only ever returned behind API-key auth. Deliberately NOT returned in
    bulk from the list endpoint — see `GmailMessageSummary`.
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


class GmailMessageSummary(BaseModel):
    """Compact representation for GET /gmail/messages (GMAIL-007).

    Deliberately excludes `body_plain`, full recipient arrays (`to`/`cc`),
    `references`, and per-attachment detail — a bulk list response (up to
    `GMAIL_MAX_LIST_LIMIT` rows) should not carry full correspondence
    content for every row. `GET /gmail/messages/{id}` remains the
    authenticated full-detail endpoint (`GmailMessage`).
    """

    id: int
    thread_id: int
    direction: Direction
    from_address: str | None
    subject: str
    sent_at: datetime | None
    received_at: datetime
    has_html: bool
    body_truncated: bool
    attachment_count: int


class GmailThread(BaseModel):
    """A neutral (non-Gmail-native) thread grouping — see
    app.db.models.GmailThreadRecord's docstring for `thread_key`'s
    derivation, account scoping, and documented limitations.
    """

    id: int
    thread_key: str
    subject: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class GmailThreadDetail(GmailThread):
    """GET /gmail/threads/{id}'s response: the thread header plus a
    bounded (never unbounded) list of its messages, so a Stage 7B
    consumer has one coherent, safe way to read a thread's correspondence
    context without an additional unbounded query of its own devising.
    Message content stays summary-level here too (GMAIL-007's rationale
    applies equally to a thread's message list) — fetch
    `GET /gmail/messages/{id}` for full detail on any one of them.
    """

    messages: list[GmailMessageSummary]


class GmailSyncResult(BaseModel):
    """Structured summary of one POST /gmail/sync run. Never includes any
    message content — only counts (spec section 14).
    """

    fetched: int
    created: int
    duplicates: int
    skipped: int
    failed: int
