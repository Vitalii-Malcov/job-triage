"""ORM models: email collectors (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessedEmailMessage(Base):
    """Tracks which inbound emails an email-based collector has already
    parsed, so re-running fetch() doesn't re-parse the same message.

    Deliberately separate from mutating the mailbox itself (e.g. marking a
    message read) — collectors must have read-only IMAP access (see
    app/collectors/xing_email.py).
    """

    __tablename__ = "processed_email_messages"
    # Review 2.1: the identity is (source, message_id) -- exactly what
    # is_message_processed / mark_message_processed look up. Two sources may
    # legitimately hold the same Message-ID.
    __table_args__ = (
        UniqueConstraint(
            "source", "message_id", name="uq_processed_email_messages_source_message_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # RFC 5322 Message-ID header, e.g. "<abc123@mail.xing.com>". 998 is the
    # RFC 5322 recommended max header line length.
    message_id: Mapped[str] = mapped_column(String(998), nullable=False, index=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class XingScanProgressRecord(Base):
    """Codex gate follow-up (Astra R4A, XING starvation MEDIUM): durable,
    bounded IMAP scan progress for `app.collectors.xing_email
    .XingEmailCollector` -- one row, keyed by `source` (mirrors
    `ProcessedEmailMessage.source`; XING has exactly one configured
    mailbox today, so no separate per-mailbox scoping exists yet).

    **The problem this closes.** Even after the earlier Message-ID
    header pre-check fix (skip the full RFC822 body transfer for an
    already-processed message), every run still issued ONE cheap
    header-only IMAP FETCH per already-processed message in the search
    window. A large processed prefix -- inevitable once a mailbox has
    months of digests -- could still consume the entire per-run session
    deadline on header FETCHes alone, before the loop (which walks
    candidates in ascending order) ever reached a genuinely new message
    at the end of the list. Because each run restarted the scan from the
    very first candidate, this starvation was not self-healing: it would
    recur on every single cycle.

    **Why IMAP UID, not a DB row id (AUD-004's lesson, deliberately NOT
    repeated here).** `AutomationMailProgressRecord.gmail_after_message_id`
    was retired (see its own docstring and migration aff3c7dc6349) because
    `GmailMessageRecord.id` allocation order does not equal PostgreSQL
    commit-visibility order -- a lower id can become visible after a
    higher one, so `id > cursor` can permanently skip a row. An IMAP UID
    is a fundamentally different kind of identifier: it is assigned by
    the mail SERVER, is guaranteed by RFC 3501 to strictly increase and
    never be reused within one `UIDVALIDITY` epoch, and this collector
    never writes anything whose id a concurrent transaction could still
    be in the middle of allocating -- there is no analogous
    commit-visibility race to reproduce here.

    **`confirmed_upto_uid` only ever advances past UIDs that are provably
    safe to never look at again** -- either confirmed already-processed
    (via the existing per-message-id acknowledgment in
    `ProcessedEmailMessage`, itself only ever written AFTER every job in
    a batch persists -- see `mark_message_processed`'s call site in
    `app.services.collector_runner.run_xing`) or confirmed not a job
    digest at all (wrong sender/subject/missing Message-ID). A UID whose
    batch failed to persist, or that was never reached this run (session
    deadline fired first), is never counted -- `run_xing` only advances
    the STORED value up to the first UID in ascending order that is NOT
    provably handled, so a gap can never be silently skipped over on a
    later run. This column therefore only ever lets FULLY resolved UIDs
    be skipped without any IMAP round trip at all on the next cycle --
    it never substitutes for the per-message acknowledgment itself.

    **`uid_validity`.** If the mailbox's UIDVALIDITY ever changes (e.g.
    the mailbox is recreated), previously stored UIDs are no longer
    comparable to new ones -- `run_xing` then ignores the stored
    `confirmed_upto_uid` entirely and starts scanning fresh, exactly like
    a brand-new installation, rather than risk skipping reused low UIDs
    that are actually unseen messages.
    """

    __tablename__ = "xing_scan_progress"
    __table_args__ = (
        UniqueConstraint("source", "mailbox_scope", name="uq_xing_scan_progress_source_mailbox"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # Codex gate follow-up (Astra R4A MEDIUM, mailbox scope): deterministic
    # non-secret hash of (imap_host, imap_port, username, mailbox) -- see
    # `app.db.xing_scan_progress_repository.compute_mailbox_scope`'s own
    # docstring. Part of the row's real identity together with `source`:
    # two mailboxes that happen to share a `UIDVALIDITY` must never share
    # a watermark row.
    mailbox_scope: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uid_validity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_upto_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
