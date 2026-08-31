"""Gmail inbox persistence (Stage 7A) — idempotent message/thread storage.

**Dedup identity is `(mailbox, uid_validity, uid)`** (see
GmailMessageRecord's docstring for why), enforced by a DB UNIQUE
constraint, not just a Python SELECT-then-INSERT: `upsert_message` always
re-queries after an `IntegrityError` rather than assuming its own insert
won, so two concurrent syncs racing to persist the same message can never
create two rows for it (spec section 22).

**Threading is neutral, not Gmail-native** — `resolve_thread_anchor`
derives a thread grouping purely from Message-ID/In-Reply-To/References
headers. See app/providers/email/imap.py's module docstring for the
documented limitation this implies, and GmailThreadRecord's docstring for
the synthetic-key fallback used when a message has none of those headers
at all.

Nothing in this module logs message content (subject/body/addresses) —
see app/services/gmail_inbox.py for the sync orchestration and its
privacy-safe logging.
"""

import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import GmailMessageRecord, GmailThreadRecord
from app.models.gmail import GmailAttachment, GmailMessage, GmailThread
from app.providers.email.base import ParsedGmailMessage


class GmailRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — e.g. reloading the row a UNIQUE constraint
    IntegrityError implies must exist comes back None. No code path in
    this project deletes GmailThreadRecord/GmailMessageRecord rows, so
    this should be unreachable; raised instead of silently returning None
    and letting a caller crash later with an unrelated AttributeError
    (mirrors app.db.repositories.CompanyResearchConsistencyError).
    """


def resolve_thread_anchor(
    message_id_header: str | None,
    in_reply_to: str | None,
    references: tuple[str, ...],
) -> str | None:
    """The thread-grouping key a message resolves to, or None if it has no
    usable Message-ID/In-Reply-To/References at all (caller falls back to
    a synthetic per-message key — see `upsert_message`).

    `references[0]` (the oldest ancestor a mail client recorded) is
    preferred over `in_reply_to` (the immediate parent only) so that,
    regardless of fetch order, every message in a thread that consistently
    carries References resolves to the SAME anchor: the root message's own
    Message-ID (its thread key, when the root itself is later parsed, is
    its own Message-ID — i.e. references[0] again). A message with
    In-Reply-To but no References anchors to its immediate parent's
    Message-ID instead — see the documented limitation in
    app/providers/email/imap.py's module docstring for what this means
    when a whole subtree of replies drops References.
    """
    if references:
        return references[0]
    if in_reply_to:
        return in_reply_to
    return message_id_header


def get_message_by_identity(
    db: Session, mailbox: str, uid_validity: int, uid: int
) -> GmailMessageRecord | None:
    """Pure read by the DB-enforced dedup identity — never mutates."""
    return db.scalar(
        select(GmailMessageRecord).where(
            GmailMessageRecord.mailbox == mailbox,
            GmailMessageRecord.uid_validity == uid_validity,
            GmailMessageRecord.uid == uid,
        )
    )


def get_or_create_thread(db: Session, thread_key: str, subject: str) -> GmailThreadRecord:
    """Race-safe get-or-create on `GmailThreadRecord.thread_key`'s UNIQUE
    constraint: a plain SELECT first, then INSERT, then — if a concurrent
    creator won the race in between — catch the IntegrityError and reload
    the winning row instead of raising. Mirrors the create/catch/reload
    idiom used throughout app.db.repositories (e.g.
    CompanyResearchIdentityAlias).
    """
    existing = db.scalar(
        select(GmailThreadRecord).where(GmailThreadRecord.thread_key == thread_key)
    )
    if existing is not None:
        return existing

    record = GmailThreadRecord(thread_key=thread_key, subject=subject)
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(GmailThreadRecord).where(GmailThreadRecord.thread_key == thread_key)
        )
        if existing is None:
            raise GmailRepositoryConsistencyError(
                f"Expected a gmail_threads row for thread_key={thread_key!r} after a "
                "UNIQUE constraint collision, but none was found."
            ) from None
        return existing

    db.refresh(record)
    return record


def upsert_message(db: Session, parsed: ParsedGmailMessage) -> tuple[GmailMessageRecord, bool]:
    """Persist one parsed message idempotently. Returns (record, created) —
    created=False for an already-persisted (mailbox, uid_validity, uid)
    identity, in which case `record` is the pre-existing row and nothing
    is written.

    Concurrency: if two syncs race to insert the same new message, the
    loser's INSERT fails on the UNIQUE(mailbox, uid_validity, uid)
    constraint; caught below, rolled back, and resolved by re-reading the
    winner's row rather than raising or double-inserting.
    """
    existing = get_message_by_identity(db, parsed.mailbox, parsed.uid_validity, parsed.uid)
    if existing is not None:
        return existing, False

    anchor = resolve_thread_anchor(parsed.message_id_header, parsed.in_reply_to, parsed.references)
    if anchor is None:
        anchor = f"synthetic:{parsed.mailbox}:{parsed.uid_validity}:{parsed.uid}"
    thread = get_or_create_thread(db, anchor, parsed.subject)

    record = GmailMessageRecord(
        thread_id=thread.id,
        mailbox=parsed.mailbox,
        uid_validity=parsed.uid_validity,
        uid=parsed.uid,
        message_id_header=parsed.message_id_header,
        in_reply_to=parsed.in_reply_to,
        references_json=json.dumps(list(parsed.references)),
        from_address=parsed.from_address,
        from_display_name=parsed.from_display_name,
        to_addresses_json=json.dumps(list(parsed.to_addresses)),
        cc_addresses_json=json.dumps(list(parsed.cc_addresses)),
        subject=parsed.subject,
        sent_at=parsed.sent_at,
        direction=parsed.direction,
        body_plain=parsed.body_plain,
        body_truncated=parsed.body_truncated,
        has_html=parsed.has_html,
        attachments_json=json.dumps(
            [
                {"filename": a.filename, "content_type": a.content_type, "size": a.size}
                for a in parsed.attachments
            ]
        ),
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_message_by_identity(db, parsed.mailbox, parsed.uid_validity, parsed.uid)
        if existing is None:
            raise GmailRepositoryConsistencyError(
                f"Expected a gmail_messages row for identity "
                f"(mailbox={parsed.mailbox!r}, uid_validity={parsed.uid_validity}, "
                f"uid={parsed.uid}) after a UNIQUE constraint collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def get_message_by_id(db: Session, message_id: int) -> GmailMessageRecord | None:
    return db.get(GmailMessageRecord, message_id)


def list_messages(db: Session, limit: int, offset: int) -> list[GmailMessageRecord]:
    stmt = (
        select(GmailMessageRecord)
        .order_by(GmailMessageRecord.received_at.desc(), GmailMessageRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def get_thread_by_id(db: Session, thread_id: int) -> GmailThreadRecord | None:
    return db.get(GmailThreadRecord, thread_id)


def list_threads(db: Session, limit: int, offset: int) -> list[GmailThreadRecord]:
    stmt = (
        select(GmailThreadRecord)
        .order_by(GmailThreadRecord.updated_at.desc(), GmailThreadRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def to_gmail_message(record: GmailMessageRecord) -> GmailMessage:
    attachments = [GmailAttachment(**item) for item in json.loads(record.attachments_json)]
    return GmailMessage(
        id=record.id,
        thread_id=record.thread_id,
        message_id_header=record.message_id_header,
        in_reply_to=record.in_reply_to,
        references=json.loads(record.references_json),
        from_address=record.from_address,
        from_display_name=record.from_display_name,
        to_addresses=json.loads(record.to_addresses_json),
        cc_addresses=json.loads(record.cc_addresses_json),
        subject=record.subject,
        sent_at=record.sent_at,
        received_at=record.received_at,
        direction=record.direction,
        body_plain=record.body_plain,
        body_truncated=record.body_truncated,
        has_html=record.has_html,
        attachments=attachments,
        created_at=record.created_at,
    )


def to_gmail_thread(db: Session, record: GmailThreadRecord) -> GmailThread:
    message_count = (
        db.scalar(
            select(func.count(GmailMessageRecord.id)).where(
                GmailMessageRecord.thread_id == record.id
            )
        )
        or 0
    )
    return GmailThread(
        id=record.id,
        thread_key=record.thread_key,
        subject=record.subject,
        message_count=message_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
