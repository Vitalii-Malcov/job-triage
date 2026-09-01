"""Gmail inbox persistence (Stage 7A) — idempotent, account-scoped
message/thread storage.

**Dedup identity is `(account_key, mailbox, uid_validity, uid)`** (see
GmailMessageRecord's docstring for why `account_key` is part of it —
GMAIL-002), enforced by a DB UNIQUE constraint, not just a Python
SELECT-then-INSERT: `upsert_message` always re-queries after an
`IntegrityError` rather than assuming its own insert won, so two
concurrent syncs racing to persist the same message can never create two
rows for it (spec section 22).

**Threading is neutral, not Gmail-native, and account-scoped** —
`resolve_thread_anchor` derives a thread grouping purely from
Message-ID/In-Reply-To/References headers, and every thread lookup is
scoped to `account_key` (GMAIL-002) so two different configured mailbox
accounts can never merge or collide threads merely because they
reference the same Message-ID string. See app/providers/email/imap.py's
module docstring for the documented References/In-Reply-To limitation,
and `upsert_message`'s docstring for the additional Message-ID
collision guard (GMAIL-011).

Nothing in this module logs message content (subject/body/addresses) —
see app/services/gmail_inbox.py for the sync orchestration and its
privacy-safe logging.
"""

import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import GmailMessageRecord, GmailThreadRecord
from app.models.gmail import (
    GmailAttachment,
    GmailMessage,
    GmailMessageSummary,
    GmailThread,
    GmailThreadDetail,
)
from app.providers.email.base import ParsedGmailMessage

# GMAIL-013 (thread detail readiness): a bounded default/maximum for how
# many of a thread's messages GET /gmail/threads/{id} returns inline —
# mirrors the GMAIL_DEFAULT_LIST_LIMIT/GMAIL_MAX_LIST_LIMIT pattern used
# for the top-level list endpoints (app/api/routes.py), scoped smaller
# since a single thread is expected to hold far fewer messages than the
# whole mailbox.
THREAD_DETAIL_DEFAULT_MESSAGE_LIMIT = 50
THREAD_DETAIL_MAX_MESSAGE_LIMIT = 200


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


def is_message_known(
    db: Session, account_key: str, mailbox: str, uid_validity: int, uid: int
) -> bool:
    """True if this exact provider identity is already persisted.

    Bound into `GmailImapProvider` via closure as `is_uid_known` (see
    app/api/routes.py's `_run_gmail_sync`) so the provider can filter
    already-synced UIDs out BEFORE applying MAX_MESSAGES_PER_SYNC
    (GMAIL-005 starvation fix — see app/providers/email/imap.py's
    `_fetch_sync`) — mirrors
    app.collectors.xing_email.XingEmailCollector's own
    `is_message_processed` callable/rationale.
    """
    return get_message_by_identity(db, account_key, mailbox, uid_validity, uid) is not None


def get_message_by_identity(
    db: Session, account_key: str, mailbox: str, uid_validity: int, uid: int
) -> GmailMessageRecord | None:
    """Pure read by the DB-enforced dedup identity — never mutates."""
    return db.scalar(
        select(GmailMessageRecord).where(
            GmailMessageRecord.account_key == account_key,
            GmailMessageRecord.mailbox == mailbox,
            GmailMessageRecord.uid_validity == uid_validity,
            GmailMessageRecord.uid == uid,
        )
    )


def _message_id_used_by_another_message(
    db: Session, account_key: str, message_id_header: str
) -> bool:
    """True if some already-persisted message in this account already
    carries this exact Message-ID header (GMAIL-011).

    Only ever called from the self-anchoring branch of `upsert_message`
    (a message whose own Message-ID is being used as a *new* thread
    root), and only after `upsert_message` has already confirmed this
    message's own provider identity is not yet persisted — so any match
    found here necessarily belongs to a *different* message, never this
    same one re-synced.
    """
    return (
        db.scalar(
            select(GmailMessageRecord.id).where(
                GmailMessageRecord.account_key == account_key,
                GmailMessageRecord.message_id_header == message_id_header,
            )
        )
        is not None
    )


def get_or_create_thread(
    db: Session, account_key: str, thread_key: str, subject: str
) -> GmailThreadRecord:
    """Race-safe get-or-create on `GmailThreadRecord`'s
    `(account_key, thread_key)` UNIQUE constraint: a plain SELECT first,
    then INSERT, then — if a concurrent creator won the race in between —
    catch the IntegrityError and reload the winning row instead of
    raising. Mirrors the create/catch/reload idiom used throughout
    app.db.repositories (e.g. CompanyResearchIdentityAlias).
    """
    existing = db.scalar(
        select(GmailThreadRecord).where(
            GmailThreadRecord.account_key == account_key,
            GmailThreadRecord.thread_key == thread_key,
        )
    )
    if existing is not None:
        return existing

    record = GmailThreadRecord(account_key=account_key, thread_key=thread_key, subject=subject)
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(GmailThreadRecord).where(
                GmailThreadRecord.account_key == account_key,
                GmailThreadRecord.thread_key == thread_key,
            )
        )
        if existing is None:
            raise GmailRepositoryConsistencyError(
                f"Expected a gmail_threads row for account_key={account_key!r} "
                f"thread_key={thread_key!r} after a UNIQUE constraint collision, "
                "but none was found."
            ) from None
        return existing

    db.refresh(record)
    return record


def _resolve_anchor(db: Session, parsed: ParsedGmailMessage) -> str:
    """The thread_key `upsert_message` should use for `parsed` — wraps
    `resolve_thread_anchor` with the GMAIL-011 Message-ID collision guard
    and the synthetic-key fallback.

    Three cases:
    1. No usable Message-ID/In-Reply-To/References at all: an
       unlinkable singleton, given its own synthetic key.
    2. The resolved anchor came from `references[0]`/`in_reply_to` (this
       message explicitly points at another message as its
       parent/ancestor): trusted as the legitimate, protocol-intended use
       of Message-ID — joins that thread.
    3. The resolved anchor is this message's OWN Message-ID (no
       References/In-Reply-To of its own — it is establishing itself as a
       would-be thread root): only trusted if no *other*, already
       distinct message in this account has already used that exact
       Message-ID. If one has, this is a reused/malformed/malicious
       Message-ID, not proof of shared conversation (GMAIL-011) — routed
       to its own synthetic thread instead of silently merging two
       unrelated messages.
    """
    anchor = resolve_thread_anchor(parsed.message_id_header, parsed.in_reply_to, parsed.references)
    synthetic_key = f"synthetic:{parsed.mailbox}:{parsed.uid_validity}:{parsed.uid}"
    if anchor is None:
        return synthetic_key

    self_anchored = (
        anchor == parsed.message_id_header and not parsed.references and not parsed.in_reply_to
    )
    if self_anchored and _message_id_used_by_another_message(db, parsed.account_key, anchor):
        return f"synthetic-collision:{synthetic_key}"

    return anchor


def upsert_message(db: Session, parsed: ParsedGmailMessage) -> tuple[GmailMessageRecord, bool]:
    """Persist one parsed message idempotently. Returns (record, created) —
    created=False for an already-persisted
    (account_key, mailbox, uid_validity, uid) identity, in which case
    `record` is the pre-existing row and nothing is written.

    Concurrency: if two syncs race to insert the same new message, the
    loser's INSERT fails on the UNIQUE(account_key, mailbox, uid_validity,
    uid) constraint; caught below, rolled back, and resolved by
    re-reading the winner's row rather than raising or double-inserting.
    """
    existing = get_message_by_identity(
        db, parsed.account_key, parsed.mailbox, parsed.uid_validity, parsed.uid
    )
    if existing is not None:
        return existing, False

    anchor = _resolve_anchor(db, parsed)
    thread = get_or_create_thread(db, parsed.account_key, anchor, parsed.subject)

    record = GmailMessageRecord(
        thread_id=thread.id,
        account_key=parsed.account_key,
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
        existing = get_message_by_identity(
            db, parsed.account_key, parsed.mailbox, parsed.uid_validity, parsed.uid
        )
        if existing is None:
            raise GmailRepositoryConsistencyError(
                f"Expected a gmail_messages row for identity "
                f"(account_key={parsed.account_key!r}, mailbox={parsed.mailbox!r}, "
                f"uid_validity={parsed.uid_validity}, uid={parsed.uid}) after a "
                "UNIQUE constraint collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def get_message_by_id(db: Session, account_key: str, message_id: int) -> GmailMessageRecord | None:
    return db.scalar(
        select(GmailMessageRecord).where(
            GmailMessageRecord.id == message_id,
            GmailMessageRecord.account_key == account_key,
        )
    )


def list_messages(
    db: Session, account_key: str, limit: int, offset: int
) -> list[GmailMessageRecord]:
    stmt = (
        select(GmailMessageRecord)
        .where(GmailMessageRecord.account_key == account_key)
        .order_by(GmailMessageRecord.received_at.desc(), GmailMessageRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def get_thread_by_id(db: Session, account_key: str, thread_id: int) -> GmailThreadRecord | None:
    return db.scalar(
        select(GmailThreadRecord).where(
            GmailThreadRecord.id == thread_id,
            GmailThreadRecord.account_key == account_key,
        )
    )


def list_threads_with_counts(
    db: Session, account_key: str, limit: int, offset: int
) -> list[tuple[GmailThreadRecord, int]]:
    """GMAIL-008: one grouped/joined aggregate query for the whole page —
    never a separate COUNT query per thread. Returns `(record,
    message_count)` pairs; the number of SQL statements this executes
    stays constant regardless of how many threads are returned.
    """
    stmt = (
        select(GmailThreadRecord, func.count(GmailMessageRecord.id))
        .outerjoin(GmailMessageRecord, GmailMessageRecord.thread_id == GmailThreadRecord.id)
        .where(GmailThreadRecord.account_key == account_key)
        .group_by(GmailThreadRecord.id)
        .order_by(GmailThreadRecord.updated_at.desc(), GmailThreadRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(record, count) for record, count in db.execute(stmt).all()]


def get_thread_message_count(db: Session, account_key: str, thread_id: int) -> int:
    """One COUNT query for exactly one thread — used by the single-thread
    detail endpoint (GET /gmail/threads/{id}), which is not the N+1
    pattern GMAIL-008 addresses (that was the *list* endpoint issuing one
    such query per row; a single detail read issuing one query for its
    one thread is normal and unavoidable).
    """
    return (
        db.scalar(
            select(func.count(GmailMessageRecord.id)).where(
                GmailMessageRecord.thread_id == thread_id,
                GmailMessageRecord.account_key == account_key,
            )
        )
        or 0
    )


def list_messages_for_thread(
    db: Session, account_key: str, thread_id: int, limit: int
) -> list[GmailMessageRecord]:
    """Bounded message list for one thread (GET /gmail/threads/{id},
    section 13 "thread detail API readiness") — never unbounded, and
    always account-scoped.
    """
    stmt = (
        select(GmailMessageRecord)
        .where(
            GmailMessageRecord.thread_id == thread_id,
            GmailMessageRecord.account_key == account_key,
        )
        .order_by(GmailMessageRecord.received_at.asc(), GmailMessageRecord.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def to_gmail_message(record: GmailMessageRecord) -> GmailMessage:
    """Full detail conversion — GET /gmail/messages/{id} only (GMAIL-007).
    Includes body_plain/full recipients/references; never used for the
    bulk list endpoint.
    """
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


def to_gmail_message_summary(record: GmailMessageRecord) -> GmailMessageSummary:
    """Compact conversion — GET /gmail/messages (list) and
    GET /gmail/threads/{id} (GMAIL-007). Never includes body_plain, full
    recipient arrays, references, or per-attachment detail.
    """
    attachment_count = len(json.loads(record.attachments_json))
    return GmailMessageSummary(
        id=record.id,
        thread_id=record.thread_id,
        direction=record.direction,
        from_address=record.from_address,
        subject=record.subject,
        sent_at=record.sent_at,
        received_at=record.received_at,
        has_html=record.has_html,
        body_truncated=record.body_truncated,
        attachment_count=attachment_count,
    )


def to_gmail_thread(record: GmailThreadRecord, message_count: int) -> GmailThread:
    """GMAIL-008: `message_count` is supplied by the caller (from
    `list_threads_with_counts`'s single grouped query) — this function
    itself never issues a DB query.
    """
    return GmailThread(
        id=record.id,
        thread_key=record.thread_key,
        subject=record.subject,
        message_count=message_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_gmail_thread_detail(
    db: Session,
    record: GmailThreadRecord,
    message_count: int,
    *,
    message_limit: int = THREAD_DETAIL_DEFAULT_MESSAGE_LIMIT,
) -> GmailThreadDetail:
    """GET /gmail/threads/{id}'s full response — thread header plus a
    bounded, chronologically-ordered list of its messages in summary form
    (section 13). `message_limit` is caller-bounded (see
    THREAD_DETAIL_MAX_MESSAGE_LIMIT in app/api/routes.py).
    """
    messages = list_messages_for_thread(db, record.account_key, record.id, message_limit)
    thread = to_gmail_thread(record, message_count)
    return GmailThreadDetail(
        **thread.model_dump(),
        messages=[to_gmail_message_summary(message) for message in messages],
    )
