"""ORM models: gmail (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    false as sa_false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Codex mailbox-width finding: the persisted Gmail mailbox identity is
# `app.providers.email.imap_mailbox.canonical_mailbox_identity(name)`
# (modified UTF-7), not the decoded name the config limits to
# MAX_MAILBOX_NAME_LENGTH (100) code points. Worst case: one run of 100
# supplementary code points = 400 UTF-16 bytes -> "&" + ceil(1600 / 3) =
# 534 base64 characters + "-" = 536 ASCII characters (a literal "&" is
# only 2, a BMP character in a run at most 8/3, and run boundaries cost
# more input than they add). 600 leaves headroom; a test re-derives the
# bound from MAX_MAILBOX_NAME_LENGTH so raising that limit fails loudly.
# Shared by every column storing that identity: gmail_messages.mailbox,
# gmail_message_id_claims.claimant_mailbox, gmail_permanent_skips.mailbox.
GMAIL_MAILBOX_IDENTITY_MAX_LENGTH = 600


# gmail_threads.lock_holder embeds the identity too:
# `app.db.gmail_repository.upsert_message` holds
# "gmail_sync:<identity>:<uid_validity>:<uid>:<32 hex>" -- at most
# 11 + 600 + 1 + 10 + 1 + 10 + 1 + 32 = 666 characters (a test re-derives it).
GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH = 700


class GmailThreadRecord(Base):
    """A neutral (non-Gmail-native) correspondence thread grouping (Stage
    7A) — see app/db/gmail_repository.py's `resolve_thread_anchor` for how
    `thread_key` is derived from Message-ID/In-Reply-To/References
    headers, and app/providers/email/imap.py's module docstring for the
    documented limitation this implies (a message with In-Reply-To but no
    References can end up anchored to its immediate parent rather than
    the true thread root).

    `thread_key` is not a Gmail thread id — standard IMAP does not expose
    Gmail's X-GM-THRID extension via this project's read-only ImapClient
    Protocol, so none is ever fabricated. It is either the RFC 5322
    Message-ID this thread is anchored to (the oldest ancestor referenced
    by any message seen so far), or a synthetic
    "synthetic:<mailbox>:<uid_validity>:<uid>" key for a message with no
    Message-ID/In-Reply-To/References at all (an unlinkable singleton
    thread of one).

    **`account_key`.** `thread_key` alone is scoped to
    `account_key` — a raw Message-ID/In-Reply-To/References value is
    trusted only within one configured mailbox account. Without this, a
    later switch of `GMAIL_USERNAME` to a different account could
    silently join threads with (or collide identity against) an entirely
    different account's history purely because both happen to reference
    the same Message-ID string. See `normalize_account_key` in
    app/providers/email/base.py.

    **Message-ID collision policy.** A `thread_key` equal to
    a message's own Message-ID (the "this message is a thread root"
    case — see `resolve_thread_anchor`) is not treated as trustworthy
    proof of shared conversation if that same Message-ID string is *also*
    already used by a different, already-persisted message in this
    account: app/db/gmail_repository.py's `upsert_message` routes that
    case to a separate synthetic thread instead of silently merging two
    unrelated messages that happen to share a (possibly malformed or
    replayed) Message-ID. A message that *references* an existing thread
    via `References`/`In-Reply-To` is unaffected by this guard — that is
    the legitimate, protocol-intended use of Message-ID.
    """

    __tablename__ = "gmail_threads"
    __table_args__ = (
        UniqueConstraint("account_key", "thread_key", name="uq_gmail_threads_account_thread_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Normalized GMAIL_USERNAME (never a password/secret) — see
    # app.providers.email.base.normalize_account_key.
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    thread_key: Mapped[str] = mapped_column(String(998), nullable=False)
    subject: Mapped[str] = mapped_column(String(998), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # a generic, Gmail-thread-
    # scoped mutual-exclusion primitive — see
    # app.db.gmail_repository.acquire_thread_lock/wait_for_thread_lock for
    # the CAS mechanics. Deliberately just "who currently holds this
    # thread, until when" with NO knowledge of WHY (Stage 7A's
    # `upsert_message` and Stage 7E's `send_follow_up` are its only two
    # callers today, but neither this table nor gmail_repository.py
    # imports or reasons about job/application concepts — see
    # app/services/gmail_inbox.py's "zero job/application linkage"
    # constraint). `lock_expires_at` bounds how long a crashed holder can
    # block everyone else — a lock past its expiry is treated as free by
    # `acquire_thread_lock`, never held forever.
    lock_holder: Mapped[str | None] = mapped_column(
        String(GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH), nullable=True
    )
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list["GmailMessageRecord"]] = relationship(back_populates="thread")


class GmailMessageRecord(Base):
    """One inbound/outbound Gmail mailbox message, persisted read-only
    (Stage 7A Gmail Inbox Foundation) — see app/services/gmail_inbox.py
    for sync orchestration and app/providers/email/imap.py for the IMAP
    fetch/MIME-parsing this is populated from.

    Deliberately a separate table from `ProcessedEmailMessage`:
    ProcessedEmailMessage is a minimal per-source Message-ID
    acknowledgment marker used by job-digest collectors (see
    app/collectors/xing_email.py) to avoid re-parsing an email into `Job`
    rows; this table is the actual normalized correspondence record
    future stages (7B-7E) read from, and stores real message content.

    **Dedup identity is `(account_key, mailbox, uid_validity, uid)`, not
    `message_id_header`.** An IMAP UID is only guaranteed stable while
    UIDVALIDITY for that mailbox hasn't changed, AND is only meaningful
    within the one account whose mailbox it belongs to — so
    all four must be compared together, never the bare UID alone.
    `message_id_header` is kept for threading only (see
    GmailThreadRecord) and is deliberately NOT the dedup identity: it can
    be absent (a message with no Message-ID header at all is still
    deduplicated correctly via its UID), and in principle a malformed
    mail could repeat one.

    **Privacy.** Every field here is personal correspondence content.
    app/services/gmail_inbox.py's sync logging never includes subject,
    body, addresses, or names — only internal id/counts/status. Nothing
    in this project logs `body_plain`, `subject`, `from_address`,
    `from_display_name`, `to_addresses_json`, or `cc_addresses_json`.

    **Invariants enforced at the DB layer, not just in
    application code**: `uid`/`uid_validity` must be positive (0 and
    negative values are never valid IMAP identifiers), and `direction`
    must be one of the two known values — defense in depth against any
    insert path that bypasses app/db/gmail_repository.py.

    **`automation_processed_at`: a durable PER-MESSAGE
    completion marker, not a global monotonic `id` watermark.** Stage 8D's
    `gmail_response_drafts` step used to track progress as a single
    per-account `AutomationMailProgressRecord.gmail_after_message_id`
    integer cursor and select `WHERE id > :cursor` — this assumed `id`
    allocation order equals commit-visibility order, which PostgreSQL does
    NOT guarantee: transaction A can obtain a LOWER `id` but commit AFTER
    transaction B, which obtained a HIGHER `id` and committed first. If the
    cursor had already advanced to B's `id` before A's row became visible,
    `id > cursor` would PERMANENTLY skip A — a real data-loss bug, not a
    cosmetic one. `automation_processed_at` fixes this by making inclusion
    depend on nothing but this row's own state: NULL means "not yet fully
    handled by Stage 8D" and is eligible for every future scan for this
    account regardless of `id`, no matter what any OTHER row's `id` or
    processing history looks like. See
    `app.db.gmail_repository.list_unprocessed_messages_for_automation`/
    `mark_message_automation_processed` for the selection query and the
    atomic per-message CAS that sets it exactly once. `id.asc()` is still
    used to ORDER the scan (oldest-first is a nice property, never a
    correctness requirement) but never to EXCLUDE a row.
    """

    __tablename__ = "gmail_messages"
    __table_args__ = (
        UniqueConstraint(
            "account_key",
            "mailbox",
            "uid_validity",
            "uid",
            name="uq_gmail_messages_account_provider_identity",
        ),
        CheckConstraint("uid > 0", name="ck_gmail_messages_uid_positive"),
        CheckConstraint("uid_validity > 0", name="ck_gmail_messages_uid_validity_positive"),
        CheckConstraint(
            "direction IN ('INBOUND', 'OUTBOUND')", name="ck_gmail_messages_direction_valid"
        ),
        Index(
            "ix_gmail_messages_account_key_automation_processed_at",
            "account_key",
            "automation_processed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Normalized GMAIL_USERNAME (never a password/secret) — see
    # app.providers.email.base.normalize_account_key.
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    mailbox: Mapped[str] = mapped_column(String(GMAIL_MAILBOX_IDENTITY_MAX_LENGTH), nullable=False)
    # IMAP UID and UIDVALIDITY are 32-bit unsigned values (up to
    # app.providers.email.base.IMAP_NZ_NUMBER_MAX = 4294967295), past signed
    # INTEGER's 2147483647 -- BigInteger, like XingScanProgressRecord and
    # GmailApplicationDraftRecord (migration e8b1c4f7a2d9).
    uid_validity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # RFC 5322 Message-ID / In-Reply-To / References headers. Indexed
    # (not unique) — see the class docstring for why this is never the
    # dedup identity.
    message_id_header: Mapped[str | None] = mapped_column(String(998), nullable=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(998), nullable=True)
    references_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    from_address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    from_display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    to_addresses_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    cc_addresses_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    subject: Mapped[str] = mapped_column(String(998), default="", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this sync run persisted the message — distinct from `sent_at`
    # (the email's own Date header, which may be absent/malformed).
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    # S7E-011 (Codex re-review, Gmail chronology): the mail server's own
    # IMAP INTERNALDATE — assigned by Gmail itself at real arrival time,
    # trusted for correspondence ORDERING in place of `received_at` (see
    # app.db.follow_up_repository.get_thread_message_infos). Distinct from
    # BOTH other timestamps on this row: unlike `sent_at`, it is never
    # sender-controlled; unlike `received_at`, it does not depend on which
    # order (INBOX vs. Sent) THIS project's own sync run happened to fetch
    # mailboxes in — a dual-mailbox sync that persists an OLDER real
    # message strictly after a NEWER one (e.g. a first-time historical
    # sync importing a whole thread in one run) would otherwise reverse
    # `received_at` order for messages imported together. `server_default`
    # (never relied on by application code, which always passes an
    # explicit value — see app.db.gmail_repository.upsert_message) exists
    # only so a raw INSERT that omits this column (e.g. a pre-S7E-011
    # migration/test fixture) still gets a value instead of failing NOT
    # NULL; historical rows backfilled by this column's own migration are
    # necessarily an honest best-effort (see that migration's docstring),
    # not a retroactively-accurate INTERNALDATE this project never
    # recorded for them.
    provider_arrival_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    # True ONLY when
    # `provider_arrival_at` above came from a real, successfully-parsed
    # IMAP INTERNALDATE (see app/providers/email/imap.py's
    # `_parse_internal_date`) — False for BOTH of `provider_arrival_at`'s
    # own fallback cases: a server response with no parseable
    # INTERNALDATE (app.db.gmail_repository.upsert_message's wall-clock
    # fallback), and every row that predates this column's migration
    # (backfilled from the OLD `received_at`-based behavior, never a real
    # INTERNALDATE this project ever recorded for them). Defaults to
    # False (fail-closed by construction — a raw INSERT that omits this
    # column, or a future call site that forgets to set it, is never
    # silently trusted). `app.services.follow_up_eligibility` refuses to
    # determine follow-up eligibility from any message whose chronology
    # isn't True here — see `ThreadMessageInfo.timestamp_is_trusted`.
    provider_arrival_is_trusted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_false()
    )
    # "INBOUND" | "OUTBOUND" — derived purely from comparing the From
    # address against the configured mailbox account address (see
    # app/providers/email/imap.py's `_direction`). Never an interpretation
    # of message meaning/content.
    direction: Mapped[str] = mapped_column(String(10), nullable=False)

    # Plaintext only — HTML is never rendered/executed/fetched, see
    # app/providers/email/imap.py's module docstring. Bounded to
    # MAX_BODY_LENGTH chars; body_truncated records whether it was cut.
    body_plain: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_html: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # JSON list of {"filename": str | None, "content_type": str, "size":
    # int | None} — metadata only. Attachment content is never persisted,
    # opened, or analyzed; the underlying bytes may still be transferred
    # from IMAP as part of the bounded BODY.PEEK[] fetch (see
    # app.providers.email.base.ParsedAttachment's docstring, GMAIL-006).
    attachments_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    # see the class docstring's own section on this
    # column for the full rationale. NULL = not yet fully processed by
    # Stage 8D's gmail_response_drafts step (analysis + response-draft-or
    # -NO_RESPONSE_RECOMMENDED-or-OUTBOUND-skip) — eligible for every
    # future scan regardless of `id`. Set exactly once, atomically, by
    # `app.db.gmail_repository.mark_message_automation_processed`.
    automation_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    thread: Mapped["GmailThreadRecord"] = relationship(back_populates="messages")


class GmailMessageIdClaimRecord(Base):
    """The DB-enforced atomic arbiter of "who owns this Message-ID"
    within one account.

    **Why this table exists.** The original Message-ID collision guard
    (a Python `SELECT ... WHERE message_id_header = :anchor` followed by
    a decision) was itself racy: two concurrent messages sharing a
    reused/malformed Message-ID could both observe "not found yet" and
    both proceed to treat themselves as the legitimate owner, silently
    merging two unrelated conversations. A check-then-act Python
    decision can never close that window — only a real DB UNIQUE
    constraint, contended for via an INSERT + IntegrityError-catch, can.

    `UNIQUE(account_key, message_id_header)` is that arbiter: exactly one
    provider message identity can ever hold the claim for a given
    Message-ID within an account. Whichever concurrent INSERT commits
    first wins; every other concurrent (or later) attempt to claim the
    same (account_key, message_id_header) fails on this constraint —
    what happens next depends on WHO the existing claim actually belongs
    to (see app/db/gmail_repository.py's
    `_claim_message_id_or_get_collision_thread`):

    - **Same provider identity** (same `claimant_mailbox`/
      `claimant_uid_validity`/`claimant_uid` as the losing attempt): not
      a collision at all — this is a concurrent or later retry of the
      exact same message racing against itself (e.g. two overlapping
      sync runs). The existing claim's thread is reused untouched;
      `contested` is never set.
    - **Different provider identity**: a genuinely different message
      reused/replayed this Message-ID. Routed to its own synthetic
      "collision" thread instead of the winner's, and the winning claim
      is marked `contested`.

    **`contested`** is set True the first time a claim loses this race to
    a genuinely *different* provider identity (never for a same-identity
    retry). Once set, it is permanent (mirrors this project's "immutable
    historical" bias elsewhere — e.g. CandidateCVDraftRecord): a
    Message-ID that has ever been proven ambiguous stays untrusted for
    every future message that merely *references* it too (see
    `_resolve_thread_for_message`'s reply branch) — an ambiguous anchor
    is never later treated as if it had turned out fine after all.

    Deliberately not a UNIQUE(thread_id) — one thread can legitimately be
    the target of exactly one claim (the root's own Message-ID), but
    nothing here needs to look up "which claim belongs to this thread",
    only "who owns this Message-ID".
    """

    __tablename__ = "gmail_message_id_claims"
    __table_args__ = (
        UniqueConstraint(
            "account_key", "message_id_header", name="uq_gmail_message_id_claims_account_message_id"
        ),
        CheckConstraint("claimant_uid > 0", name="ck_gmail_message_id_claims_uid_positive"),
        CheckConstraint(
            "claimant_uid_validity > 0", name="ck_gmail_message_id_claims_uid_validity_positive"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False)
    message_id_header: Mapped[str] = mapped_column(String(998), nullable=False)
    # The provider identity that WON this claim — traceability only, not
    # itself part of any uniqueness (mirrors CandidateJobMatchRecord's own
    # "traceability, not identity" columns elsewhere in this file).
    claimant_mailbox: Mapped[str] = mapped_column(
        String(GMAIL_MAILBOX_IDENTITY_MAX_LENGTH), nullable=False
    )
    claimant_uid_validity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimant_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class GmailPermanentSkipRecord(Base):
    """a durable, per-message "never fetch this
    UID again" marker for a Gmail IMAP message whose CONTENT itself makes
    it permanently unpersistable -- oversized past MAX_RAW_MESSAGE_SIZE,
    or a MIME structure that fails to parse -- as distinct from a
    transient FETCH/transport failure (deadline mid-fetch, a malformed
    FETCH response shape, a non-OK `typ`), which is never recorded here
    and stays eligible for retry on the next sync.

    **Why this table exists.** `app.db.gmail_repository.get_known_uids`
    already excludes already-PERSISTED UIDs from a sync's candidate list
    before `app.providers.email.base.MAX_MESSAGES_PER_SYNC` is applied
     — but a message that is skipped every single run
    (never persisted, since its content itself is what's wrong) was NEVER
    excluded that way: it remained a "new" candidate on every future sync
    forever. Combined with oldest-UID-first prioritization (see
    `app.providers.email.imap.GmailImapProvider._fetch_sync_body`'s own
    comment on why oldest-first is deliberate), a prefix of permanently
    -bad messages at least as large as `MAX_MESSAGES_PER_SYNC` would
    consume the ENTIRE budget of every future sync re-attempting (and
    re-failing) the exact same oldest messages, permanently starving
    every genuinely valid message newer than that prefix — a real,
    silent, unrecoverable data-loss bug, not a cosmetic one. This table
    closes that: `get_known_uids` (see that function's own docstring) now
    also excludes UIDs recorded here, so a permanent skip behaves exactly
    like an already-persisted message for the purpose of never consuming
    sync budget again.

    Same dedup identity shape as `GmailMessageRecord`
    (`account_key`, `mailbox`, `uid_validity`, `uid`) for the SAME reason
    (GMAIL-002 account isolation, GMAIL-009 UIDVALIDITY-scoped identity):
    a UIDVALIDITY change on the mailbox silently makes every row for the
    OLD `uid_validity` irrelevant (never matched again by a query scoped
    to the CURRENT `uid_validity`) — identical safety property to
    `GmailMessageRecord`'s own dedup identity, no separate cleanup logic
    needed.

    `reason` is diagnostic only (e.g. "OVERSIZED", "PARSE_FAILED") — never
    itself part of the uniqueness/lookup identity.
    """

    __tablename__ = "gmail_permanent_skips"
    __table_args__ = (
        UniqueConstraint(
            "account_key",
            "mailbox",
            "uid_validity",
            "uid",
            name="uq_gmail_permanent_skips_account_provider_identity",
        ),
        CheckConstraint("uid > 0", name="ck_gmail_permanent_skips_uid_positive"),
        CheckConstraint("uid_validity > 0", name="ck_gmail_permanent_skips_uid_validity_positive"),
        Index(
            "ix_gmail_permanent_skips_account_mailbox_uid_validity",
            "account_key",
            "mailbox",
            "uid_validity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False)
    mailbox: Mapped[str] = mapped_column(String(GMAIL_MAILBOX_IDENTITY_MAX_LENGTH), nullable=False)
    uid_validity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class GmailMessageAnalysisRecord(Base):
    """Immutable Stage 7B analysis result: deterministic, evidence-based
    job/application matching + correspondence classification for one
    already-persisted `GmailMessageRecord` — see
    app/services/gmail_message_analysis.py for the orchestration and
    app/agents/email_classifier.py / app/services/email_matching.py for
    the pure-function classification/matching logic itself.

    **INFORMATION ONLY.** Nothing that reads this table sends email,
    creates a draft/reply, mutates mailbox state, mutates
    `JobRecord.status`, or performs any other external action — see the
    module docstrings above for the full hard boundary this whole
    subsystem (and CLAUDE.md) enforces. `requires_human_review=False`
    means "the deterministic evidence for THIS reading was strong", never
    "authorized to act automatically".

    **Immutable, versioned, never UPDATEd** — mirrors
    CandidateCVDraftRecord's "immutable historical" convention elsewhere
    in this file. Re-analyzing a message (a bumped `analysis_version`
    after an algorithm change, a changed `input_fingerprint`, OR a
    changed `context_fingerprint` — see below) inserts a NEW row; the
    prior revision is never overwritten and stays queryable.
    `(gmail_message_id, analysis_version, input_fingerprint,
    context_fingerprint)` is the idempotency identity (UNIQUE
    constraint): repeating the exact same analysis of the exact same
    bounded input AND effective context under the exact same algorithm
    version returns the existing row, never inserts a duplicate.

    **`matched_job_id` is deliberately not a ForeignKey** — same
    "traceability, not identity" rationale as
    `CandidateJobMatchRecord.company_research_id`: this analysis result
    must remain a legible historical record even if the referenced
    `JobRecord` is later deleted; nothing here cascades from or is
    blocked by a `JobRecord` deletion.

    **`input_fingerprint`** is a SHA-256 hex digest over only the
    message's own fields the classifier/matcher actually read (subject,
    from_address, body_plain — see
    app.services.gmail_message_analysis.compute_input_fingerprint).

    **`context_fingerprint` (Codex remediation round 1, 7B-003/004)** is
    a SHA-256 digest over the EFFECTIVE candidate `JobRecord` pool and
    thread prior-match context an analysis run actually considered — see
    app.db.gmail_analysis_repository.compute_context_fingerprint. This
    column exists because `input_fingerprint` alone let a STALE analysis
    silently masquerade as current: e.g. a message analyzed as UNMATCHED
    before its correct `JobRecord` was ever tracked would keep returning
    that same stale UNMATCHED row forever after the correct job was
    added, since the message's OWN content (what `input_fingerprint`
    covers) never changed. `context_fingerprint` makes such an
    externally-changed-context re-analysis produce a genuinely NEW
    revision instead of reusing a now-outdated cached result, while the
    OLD revision remains queryable (never overwritten) — an accurate
    historical record of what the evidence looked like at the time.

    **Evidence is bounded, structured, and PII-minimal** — see
    MATCH_EVIDENCE_MAX_ITEMS / EVIDENCE_FRAGMENT_MAX_LENGTH in
    app/services/email_matching.py and
    CLASSIFICATION_EVIDENCE_MAX_ITEMS in app/agents/email_classifier.py.
    Never the full email body or full recipient/sender addresses.
    """

    __tablename__ = "gmail_message_analyses"
    __table_args__ = (
        UniqueConstraint(
            "gmail_message_id",
            "analysis_version",
            "input_fingerprint",
            "context_fingerprint",
            name="uq_gmail_message_analyses_identity",
        ),
        CheckConstraint("analysis_version > 0", name="ck_gmail_message_analyses_version_positive"),
        CheckConstraint(
            "match_type IN ('APPLICATION', 'JOB_ONLY', 'AMBIGUOUS', 'UNMATCHED')",
            name="ck_gmail_message_analyses_match_type_valid",
        ),
        CheckConstraint(
            "match_confidence IN ('HIGH', 'MEDIUM', 'LOW')",
            name="ck_gmail_message_analyses_match_confidence_valid",
        ),
        CheckConstraint(
            "classification_confidence IN ('HIGH', 'MEDIUM', 'LOW')",
            name="ck_gmail_message_analyses_classification_confidence_valid",
        ),
        CheckConstraint(
            "classification IN ("
            "'APPLICATION_RECEIVED', 'REQUEST_FOR_INFORMATION', 'INTERVIEW_INVITATION', "
            "'INTERVIEW_RESCHEDULE', 'REJECTION', 'OFFER', "
            "'WITHDRAWAL_OR_POSITION_CLOSED', 'GENERAL_RECRUITER_MESSAGE', "
            "'AUTOMATED_NOTIFICATION', 'OTHER', 'UNKNOWN')",
            name="ck_gmail_message_analyses_classification_valid",
        ),
        CheckConstraint("match_score >= 0", name="ck_gmail_message_analyses_match_score_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Denormalized from the parent gmail_messages row for scoping
    # symmetry with every other Gmail table (GMAIL-002-style account
    # isolation) — every read additionally filters by this, never trusted
    # merely because a caller already holds a numeric gmail_message_id.
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    gmail_message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")

    match_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Not a ForeignKey — see class docstring.
    matched_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    match_score: Mapped[int] = mapped_column(Integer, nullable=False)
    # Bounded JSON list of {"kind", "value", "weight"} — see class docstring.
    match_evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # Bounded JSON list of {"job_id", "score"} — populated only for
    # match_type == "AMBIGUOUS" (the tied top-scoring candidates).
    candidate_matches_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    classification_confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    # Bounded JSON list of {"kind", "value", "weight"}.
    classification_evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    is_automated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Safe-by-default: see app.services.gmail_message_analysis's
    # determine_requires_human_review for the exact rule. Never read as
    # authorization to act externally — see class docstring.
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
