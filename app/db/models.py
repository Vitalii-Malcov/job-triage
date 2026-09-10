from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
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


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_jobs_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    company: Mapped[str] = mapped_column(String(300), nullable=False)
    location: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    skills_json: Mapped[str] = mapped_column(Text, default="[]")
    data_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    skill_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    must_have_skills_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    nice_to_have_skills_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class JobReferenceTokenRecord(Base):
    """Normalized job/application reference tokens for exact-equality
    lookup (Stage 7B Codex remediation round 2, Blocker 3).

    **Why this table exists.** Stage 7B's `get_job_candidates` used to
    fall back to a `LIKE '%token%'` substring query over `jobs.url`/
    `jobs.title` when a bounded recency scan missed a JobRecord matching
    an email's explicit reference. A Codex review reproduced that with
    enough OTHER jobs whose url/title happened to substring-contain
    pieces of the searched token, that broad recall query's own
    `REFERENCE_TARGETED_SCAN_LIMIT` cap filled up with false partial
    collisions before the real exact match was ever retrieved — a larger
    LIMIT only moves the same bug further out. This table replaces
    substring recall with real indexed EQUALITY: tokens are extracted
    deterministically (same `extract_reference_tokens` function used for
    email-side extraction — see app/services/email_matching.py) once, at
    write time, and an email's own extracted tokens are looked up via
    `token IN (...)` — an index scan whose result size depends only on
    how many jobs genuinely share that exact token (normally 0 or 1),
    never on how many OTHER unrelated jobs happen to exist in the table.

    **Synchronization.** Tokens are (re)computed in exactly one place —
    `app.db.repositories.sync_job_reference_tokens` — called from
    `upsert_job` after every JobRecord create/update, so this table can
    never drift from the `JobRecord.title`/`JobRecord.url` it was derived
    from. Never written to from any other call site.

    `UNIQUE(job_id, token)` prevents duplicate rows on re-sync (delete +
    reinsert, not update-in-place — there is no meaningful "identity" for
    one token row beyond the (job_id, token) pair itself). Plain
    `INDEX(token)` (not unique) is the actual query-performance target:
    two different jobs CAN legitimately share one token (e.g. a reused
    generic reference format), which is exactly the case
    `get_job_candidates` must still surface as multiple candidates for
    `match_email_to_job`'s own AMBIGUOUS handling — this table only
    guarantees FAST exact lookup, never uniqueness of the token itself
    across jobs.
    """

    __tablename__ = "job_reference_tokens"
    __table_args__ = (
        UniqueConstraint("job_id", "token", name="uq_job_reference_tokens_job_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    skills_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class CompanyResearchRecord(Base):
    """Cached, evidence-backed research about a company, keyed by a
    portable, DB-enforced-unique `identity_key` — see
    app/db/repositories.py's `_identity_key` / `get_company_research_by_identity`
    / `upsert_company_research` for how it's derived and resolved (domain
    preferred when known, normalized company name otherwise) and why a
    plain SELECT-then-INSERT is not sufficient on its own (race handling).

    Deliberately not linked to JobRecord via a foreign key: one company can
    appear on many jobs, and research is reused/cached across all of them
    rather than duplicated per job (see app/services/company_research.py).
    """

    __tablename__ = "company_research"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "domain:<normalized_domain>" if a trusted domain is known, else
    # "name:<normalized_company_name>" — the actual, DB-enforced identity.
    # normalized_domain/normalized_company_name below remain as separate,
    # indexed columns because identity resolution needs to query by each
    # independently (e.g. "is there a same-named record with no domain of
    # its own yet" — see get_company_research_by_identity), not because they
    # duplicate identity_key's job.
    identity_key: Mapped[str] = mapped_column(String(350), nullable=False, unique=True)
    normalized_company_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    normalized_domain: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    # Always None in v1 (Company Research Agent v1 has no trusted source of
    # a company's own domain — Job.url is a job-posting/job-board URL, never
    # the company's website, see app/services/company_research.py). Kept for
    # a future provider with a genuine domain source.
    company_domain: Mapped[str | None] = mapped_column(String(300), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(200), nullable=True)
    headquarters: Mapped[str | None] = mapped_column(String(300), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(100), nullable=True)
    short_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    products_or_services_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    technologies_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    hiring_signals_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # Vacancy-scoped observations (e.g. "This vacancy is located in
    # Berlin.") — deliberately NOT promoted to company-level fields like
    # `headquarters`/`technologies` above, which stay None/[] unless backed
    # by genuine company-level evidence. See app/providers/job_data_provider.py.
    relevant_facts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    positive_signals_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    risk_signals_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    source_urls_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # JSON list of {"type": "FACT"|"INFERENCE"|"UNKNOWN", "claim": str,
    # "source_url": str | None, "source_title": str | None} — provenance for
    # the fields above, so nothing here is presented as fact without a
    # traceable source. See app/models/company_research.py's Evidence model.
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # "PENDING" | "PARTIAL" | "FAILED" — see app/models/company_research.py's
    # ResearchStatus. "COMPLETE" does not exist in v1: a job-data-only,
    # zero-network provider can never honestly claim complete research.
    research_status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Set only on a *successful* research run — the actual research content
    # above reflects this timestamp. None if research has never succeeded.
    researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Attempt metadata, updated on every run (success or failure) —
    # independent of researched_at/the content fields above, so a failed
    # refresh attempt is visible without disturbing previously-good research
    # (see CompanyResearchService.get_or_run's failure-isolation contract).
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Bounded, sanitized (str(exc), never a traceback/secrets) — see
    # app/db/repositories.py's record_failed_attempt.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Optimistic-concurrency counter: incremented on every successful
    # content update. A concurrent refresh whose read predates a newer write
    # detects the mismatch and discards its own (now-stale) result instead
    # of clobbering the newer one — see upsert_company_research's
    # version-checked UPDATE and the concurrent-refresh regression test.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class CompanyResearchIdentityAlias(Base):
    """DB-backed atomic coordination point for the "mixed name/domain
    creation race" (Codex re-review finding RR-M-01).

    CompanyResearchRecord.identity_key's UNIQUE constraint only stops two
    *identical* identity_key values from both being inserted. It does
    nothing when two concurrent callers resolve two genuinely *different*
    identity_key values for what is actually the same, brand-new company —
    one caller has no domain yet ("name:acme gmbh"), the other already has
    one ("domain:acme.com") — because those are different strings and the
    constraint never fires. This table's UNIQUE normalized_company_name
    column is the real coordination point: whichever concurrent creator's
    insert here commits first is the one every other racing creator for
    that same name must defer/join to, rather than each successfully
    inserting its own separate company_research row. See
    app/db/repositories.py's `_create_company_research` /
    `_join_or_diverge_after_alias_conflict`.

    This is a coordination anchor, not a claim that every record sharing a
    display name is the same company: a same-named company that already
    carries a *different known* domain than the alias's target is never
    merged into it (Case C in `_join_or_diverge_after_alias_conflict`) —
    only the "no domain yet" / "same domain" cases join.
    """

    __tablename__ = "company_research_identity_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_company_name: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    company_research_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("company_research.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ProcessedEmailMessage(Base):
    """Tracks which inbound emails an email-based collector has already
    parsed, so re-running fetch() doesn't re-parse the same message.

    Deliberately separate from mutating the mailbox itself (e.g. marking a
    message read) — collectors must have read-only IMAP access (see
    app/collectors/xing_email.py).
    """

    __tablename__ = "processed_email_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # RFC 5322 Message-ID header, e.g. "<abc123@mail.xing.com>". 998 is the
    # RFC 5322 recommended max header line length.
    message_id: Mapped[str] = mapped_column(String(998), unique=True, nullable=False, index=True)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    uid_validity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_upto_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


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

    **`account_key` (GMAIL-002).** `thread_key` alone is scoped to
    `account_key` — a raw Message-ID/In-Reply-To/References value is
    trusted only within one configured mailbox account. Without this, a
    later switch of `GMAIL_USERNAME` to a different account could
    silently join threads with (or collide identity against) an entirely
    different account's history purely because both happen to reference
    the same Message-ID string. See `normalize_account_key` in
    app/providers/email/base.py.

    **Message-ID collision policy (GMAIL-011).** A `thread_key` equal to
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

    # S7E-013 (Codex re-review, final safety fix): a generic, Gmail-thread-
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
    lock_holder: Mapped[str | None] = mapped_column(String(200), nullable=True)
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
    within the one account whose mailbox it belongs to (GMAIL-002) — so
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

    **Invariants enforced at the DB layer (GMAIL-009), not just in
    application code**: `uid`/`uid_validity` must be positive (0 and
    negative values are never valid IMAP identifiers), and `direction`
    must be one of the two known values — defense in depth against any
    insert path that bypasses app/db/gmail_repository.py.

    **`automation_processed_at` (AUD-004, Astra R3): a durable PER-MESSAGE
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
    mailbox: Mapped[str] = mapped_column(String(100), nullable=False)
    uid_validity: Mapped[int] = mapped_column(Integer, nullable=False)
    uid: Mapped[int] = mapped_column(Integer, nullable=False)

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
    # S7E-013 (Codex re-review, final safety fix): True ONLY when
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

    # AUD-004 (Astra R3): see the class docstring's own section on this
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
    within one account (GMAIL-011 concurrency fix).

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
    claimant_mailbox: Mapped[str] = mapped_column(String(100), nullable=False)
    claimant_uid_validity: Mapped[int] = mapped_column(Integer, nullable=False)
    claimant_uid: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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


class ResponseDraftRecord(Base):
    """An immutable, versioned Stage 7C response-draft PROPOSAL for one
    Stage 7B `GmailMessageAnalysisRecord` — see
    app/services/response_draft.py for orchestration and
    app/agents/response_draft_generator.py for the deterministic,
    template-based, no-LLM content generation itself.

    **INFORMATION ONLY — a stored suggestion, never an action.** Nothing
    that reads this table sends email, creates a Gmail draft, replies,
    forwards, or mutates mailbox/`JobRecord`/`ApplicationStatus` state —
    same hard boundary as `GmailMessageAnalysisRecord` (see that class's
    docstring), one stage further downstream. `requires_human_review` is
    always `True` for every row Stage 7C writes (spec-mandated; Stage 7D,
    not this stage, owns approval/send). `status='PROPOSED'` never means
    "ready to send" — only that a template produced draft text a human
    can review, edit, and send manually.

    **`status`.** `'PROPOSED'` (subject/body populated) for the bounded
    set of classifications a reply plausibly makes sense for (see
    `app.agents.response_draft_generator.SUPPORTED_RESPONSE_CLASSIFICATIONS`).
    `'NO_RESPONSE_RECOMMENDED'` (subject/body/language NULL, `reason`
    populated) for every other classification (REJECTION,
    APPLICATION_RECEIVED, WITHDRAWAL_OR_POSITION_CLOSED,
    AUTOMATED_NOTIFICATION, OTHER, UNKNOWN) — a safe, explicit,
    always-persisted "no" rather than silently generating nothing.

    **Never invents facts.** Generated `subject`/`body` text is built
    exclusively from already-trusted stored facts — `CandidateProfileRecord`
    fields that pass `is_top_level_fact_usable_for_generation`, and the
    matched `JobRecord`'s own title/company — never from the analyzed
    email's own subject/body/from_address (that content is untrusted
    correspondence text; see `app.agents.response_draft_generator`'s
    module docstring for why it is never even passed in). Anything the
    generator could not determine (candidate name, matched job, specific
    information a recruiter asked for, availability, salary/offer
    decisions) is listed in `missing_fields_json` and represented as an
    explicit bracketed placeholder in the body — never guessed.

    **Immutable, versioned, never UPDATEd** — same convention as
    `GmailMessageAnalysisRecord`. `UNIQUE(gmail_message_id, analysis_id,
    candidate_profile_version, generator_version)` is the idempotency
    identity: repeating the exact same generation request against the
    same analysis revision and the same candidate profile version returns
    the existing row; a later re-analysis (new `analysis_id`) or a
    candidate profile edit (`candidate_profile_version` bumped) produces
    a NEW revision instead, while every prior revision remains queryable.

    **Accepted limitation (documented, not engineered around in v1):**
    identity does not pin a `JobRecord` content fingerprint the way
    `CandidateCVDraftRecord`/`CandidateJobMatchRecord` pin
    `job_snapshot_fingerprint` — an edit to the matched job's
    title/company between two identical-looking generation calls will not
    by itself trigger a new revision. Job title/company change after
    initial collection is rare in practice (see `app.db.repositories.upsert_job`,
    which does not even update `url` on existing rows); a full
    content-fingerprint pin is deferred rather than spec'd speculatively.

    **`analysis_id`/`matched_job_id` are deliberately not ForeignKeys** —
    same "traceability, not identity" rationale as
    `GmailMessageAnalysisRecord.matched_job_id`'s own docstring: this row
    must remain a legible historical record even if the referenced
    analysis/job is later removed by some future maintenance path.
    `gmail_message_id` IS a real ForeignKey (`ondelete="CASCADE"`) —
    mirrors `GmailMessageAnalysisRecord.gmail_message_id`: a response
    draft has no meaning independent of the message it responds to.
    """

    __tablename__ = "response_drafts"
    __table_args__ = (
        UniqueConstraint(
            "gmail_message_id",
            "analysis_id",
            "candidate_profile_version",
            "generator_version",
            name="uq_response_drafts_identity",
        ),
        CheckConstraint(
            "status IN ('PROPOSED', 'NO_RESPONSE_RECOMMENDED')",
            name="ck_response_drafts_status_valid",
        ),
        CheckConstraint(
            "language IS NULL OR language IN ('de', 'en')",
            name="ck_response_drafts_language_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Denormalized from the parent gmail_messages row — same GMAIL-002-style
    # account-isolation convention as GmailMessageAnalysisRecord.account_key.
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    gmail_message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Not a ForeignKey — see class docstring.
    analysis_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Not a ForeignKey — see class docstring. None when the analysis this
    # draft is based on has no matched job (match_type != APPLICATION/JOB_ONLY).
    matched_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Copied from the pinned analysis for cheap filtering without a join.
    classification: Mapped[str] = mapped_column(String(40), nullable=False)

    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # Populated only for status == 'NO_RESPONSE_RECOMMENDED'.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated only for status == 'PROPOSED'.
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Bounded JSON list of str — see class docstring's "never invents facts".
    missing_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # Provenance metadata (spec requirement) — always the deterministic
    # local generator in v1; no external/LLM provider exists yet.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # Always True for every row this stage writes — see class docstring.
    # Not DB-CHECK-enforced (a fixed-True boolean CHECK is unusual in this
    # codebase's conventions, which reserve CHECK for enums/ranges); the
    # boundary is enforced by construction in
    # app/services/response_draft.py, which never passes False.
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ResponseDraftApprovalRecord(Base):
    """An immutable human APPROVE/REJECT decision on one exact,
    already-persisted `ResponseDraftRecord` revision (Stage 7D) — see
    app/services/response_draft_send.py for the orchestration that
    creates these and enforces "NO APPROVAL = NO SEND".

    **Pins the exact content being authorized.** `pinned_subject`/
    `pinned_body` are copied verbatim from the target `ResponseDraftRecord`
    at decision time — never re-read live from that row when a send is
    later attempted (see `ResponseDraftSendRecord`). `ResponseDraftRecord`
    rows are already immutable (never UPDATEd — see that model's
    docstring), so in the normal case these copies can never drift from
    the source; the pin exists as an explicit, auditable "this is exactly
    what the human saw and approved" anchor, and as defense in depth
    against a hypothetical future bug that reads the wrong revision at
    send time.

    **One decision per draft revision, ever — `UNIQUE(response_draft_id)`.**
    A human cannot "change their mind" on an already-decided revision;
    approving/rejecting an already-decided `response_draft_id` fails (see
    `app.db.response_draft_approval_repository.create_approval`) rather
    than silently overwriting the prior decision. This is deliberate, not
    a missing feature: Stage 7C's response-draft generation is itself
    idempotent-per-identity but produces a NEW revision whenever the
    underlying analysis/candidate-profile/generator version changes (see
    `ResponseDraftRecord`'s own docstring) — "I want to reconsider" is
    always expressed by generating and approving a NEW revision, which
    naturally gets its own fresh approval, never by mutating a past
    decision. This also means a REJECTED decision is permanent for that
    exact revision — never later flipped to APPROVED.

    Deliberately NOT linked to `GmailMessageAnalysisRecord`/`JobRecord`
    via a ForeignKey (same "traceability, not identity" rationale as
    `ResponseDraftRecord.analysis_id`/`matched_job_id`) — `gmail_message_id`
    here is a denormalized copy for query/account-scoping convenience
    only.
    """

    __tablename__ = "response_draft_approvals"
    __table_args__ = (
        UniqueConstraint("response_draft_id", name="uq_response_draft_approvals_response_draft"),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_response_draft_approvals_decision_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    response_draft_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("response_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized traceability only — see class docstring.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Verbatim copies of the approved ResponseDraftRecord's own
    # subject/body at decision time — see class docstring.
    pinned_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    pinned_body: Mapped[str] = mapped_column(Text, nullable=False)

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ResponseDraftSendRecord(Base):
    """The DB-enforced atomic arbiter of "has this approved response
    draft already been sent, or is a send currently in flight" (Stage
    7D) — see app/services/response_draft_send.py's module docstring for
    the full send-gate/idempotency contract this table is the concurrency
    backbone of.

    **Why this table exists (mirrors `GmailMessageIdClaimRecord`'s own
    "atomic arbiter via UNIQUE + INSERT/IntegrityError" pattern exactly).**
    A Python check-then-act ("is there already a SENT/PENDING row for
    this draft? if not, send") is racy: two concurrent send requests for
    the same approved draft could both observe "no row yet" and both
    proceed to call the outbound provider, resulting in the recruiter
    receiving the reply twice. `UNIQUE(response_draft_id)` closes that
    window structurally — only ONE row can ever exist for a given
    `response_draft_id`, and whichever concurrent request's INSERT
    commits first is the one that may actually call the provider; every
    other concurrent (or later) request instead reads the existing row
    and acts on ITS state (see `app.db.response_draft_approval_repository`'s
    `claim_send_attempt`/`retry_send_attempt`).

    **`status` is a real, CAS-guarded state machine — mirrors
    `ApplicationPackageReviewRecord`'s own "deliberately mutable" precedent
    (the only other place in this project a row is UPDATEd in place rather
    than only ever inserted):**

    - `PENDING` — a send attempt is currently claimed/in flight. Set only
      by the INSERT that wins the UNIQUE-constraint race, or by a
      FAILED -> PENDING retry CAS (`WHERE id=:id AND status='FAILED'`,
      requiring exactly 1 row affected).
    - `SENT` — the outbound provider CONFIRMED success. Set only by a
      PENDING -> SENT CAS (`WHERE id=:id AND status='PENDING'`) executed
      AFTER `OutboundEmailProvider.send` returns without raising — never
      before (spec: "Do not mark SENT before provider success"). Once
      SENT, permanent: no code path ever transitions out of it.
    - `FAILED` — a DEFINITE pre-transmission failure only (auth/
      connection/message-construction — see
      `app.providers.email.outbound_base.EmailSendConnectionError`/
      `EmailSendAuthError`) — `send_message()` was never actually
      invoked, so "not sent" is provably true. Set only by a
      PENDING -> FAILED CAS, alongside `last_error`. A FAILED row does
      NOT consume the approval permanently — it may be retried
      (FAILED -> PENDING, `attempt_count` incremented), so one transient
      pre-send failure can never permanently block a legitimately
      approved reply from ever being sent, while still making it
      impossible for two concurrent retries to both win the same PENDING
      claim (the CAS `WHERE status='FAILED'` guard only ever lets one
      concurrent UPDATE affect a row).
    - `UNCERTAIN` — transmission was ATTEMPTED (the provider's
      `send_message()`-equivalent call was actually invoked) but an
      exception occurred before delivery could be confirmed OR ruled
      out — see `app.providers.email.outbound_base.EmailSendOutcomeUnknownError`'s
      docstring for the "safest-acceptable" classification rule this
      follows. Set only by a PENDING -> UNCERTAIN CAS, alongside
      `last_error`; `attempt_count` is left untouched (this is not a
      "failed attempt to be retried", it is a terminal, ambiguous
      outcome). **Fail-closed and terminal: NO code path ever
      transitions a row OUT of `UNCERTAIN`** — never automatically, and
      never via a later send request for the same draft (which is
      refused, provider never called again, before reaching this table's
      claim logic — see `app.services.response_draft_send`'s
      `ResponseDraftSendOutcomeUncertainError`). Manual reconciliation
      (checking whether the recruiter actually received the reply, and
      deciding what to do next) is intentionally out of scope for Stage
      7D — this table's job is only to make the ambiguity visible and
      prevent an automated duplicate-send risk, not to resolve it.

    Deliberately NOT linked to `GmailMessageAnalysisRecord`/`JobRecord`
    via a ForeignKey, same rationale as `ResponseDraftApprovalRecord`.
    `approval_id` IS a real ForeignKey — a send attempt is meaningless
    without the approval that authorized it.
    """

    __tablename__ = "response_draft_sends"
    __table_args__ = (
        UniqueConstraint("response_draft_id", name="uq_response_draft_sends_response_draft"),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_response_draft_sends_status_valid",
        ),
        CheckConstraint("attempt_count > 0", name="ck_response_draft_sends_attempt_count_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    response_draft_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("response_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approval_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("response_draft_approvals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized traceability only — see ResponseDraftApprovalRecord's
    # class docstring for the same convention.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # RFC 5322 Message-ID the provider reported for the sent message, if
    # any — traceability only, never an identity/dedup key (mirrors
    # GmailMessageRecord.message_id_header's own convention). Set only on
    # SENT.
    provider_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    # Bounded, sanitized (str(exc), never a traceback/secret) — mirrors
    # CompanyResearchRecord.last_error's convention. Set only on FAILED.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Set only on a CONFIRMED successful send — see class docstring.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FollowUpProposalRecord(Base):
    """An immutable, auditable Stage 7E follow-up PROPOSAL for one tracked
    `JobRecord` — see app/services/follow_up.py for the eligibility engine
    and app/agents/follow_up_generator.py for the deterministic,
    template-based content generation.

    **INFORMATION ONLY — a stored suggestion, never an action.** Creating
    a row here never sends email, never mutates `JobRecord.status`, and
    never mutates mailbox state — same hard boundary as
    `ResponseDraftRecord` (see that model's docstring), applied to a
    candidate-initiated follow-up instead of a reply. `requires_human_review`
    is always `True` for every row this stage writes — approval/send are
    owned by `FollowUpApprovalRecord`/`FollowUpSendRecord` below, exactly
    mirroring the Stage 7C/7D split.

    **`anchor_gmail_message_id` is the correspondence anchor** — the
    latest real `OUTBOUND` `GmailMessageRecord` in the job's matched
    thread this follow-up is chasing (see
    app.services.follow_up_eligibility's eligibility rule for exactly how
    it is chosen — never `JobRecord.first_seen_at`/`last_seen_at`, per
    CLAUDE.md). `UNIQUE(account_key, anchor_gmail_message_id)` is the
    dedup identity: at most one follow-up proposal can ever exist per
    correspondence anchor, so re-running the eligibility scan is
    idempotent (a second evaluation of the same still-due anchor returns
    the existing row, never a duplicate) — mirrors
    `ResponseDraftRecord`'s own idempotent-revision convention, except a
    follow-up has exactly one anchor-scoped identity rather than a
    version-bumped one, since nothing about a follow-up's own inputs is
    expected to change between evaluations the way a re-analyzed message
    or edited candidate profile can for Stage 7C.

    **`job_id`/`gmail_thread_id` are deliberately not ForeignKeys** — same
    "traceability, not identity" rationale as
    `GmailMessageAnalysisRecord.matched_job_id`'s own docstring.
    `anchor_gmail_message_id` IS a real ForeignKey (`ondelete="CASCADE"`)
    — a follow-up proposal has no meaning independent of the message it
    anchors to.

    **Never invents facts.** Generated `subject`/`body` are built
    exclusively from already-trusted stored facts (candidate name only if
    provenance-confirmed, job title/company only from a trusted
    `JobRecord.source` — see app.services.follow_up.TRUSTED job-source
    reuse of app.services.response_draft.TRUSTED_JOB_SOURCES) — never from
    email content itself, which is only ever used to pick a DE/EN
    template set (mirrors app.agents.response_draft_generator's own
    trust boundary).
    """

    __tablename__ = "follow_up_proposals"
    __table_args__ = (
        # S7E-009 (Codex remediation): identity now includes
        # `input_fingerprint`, not just the anchor — see that column's
        # docstring below. A still-matching re-evaluation of the same
        # anchor with UNCHANGED trusted inputs is idempotent exactly as
        # before; CHANGED inputs (job facts, candidate profile revision,
        # language, generator/config version) now produce a NEW proposal
        # revision instead of silently reusing a stale one.
        UniqueConstraint(
            "account_key",
            "anchor_gmail_message_id",
            "input_fingerprint",
            name="uq_follow_up_proposals_anchor_fingerprint",
        ),
        CheckConstraint("language IN ('de', 'en')", name="ck_follow_up_proposals_language_valid"),
        CheckConstraint("status IN ('PROPOSED')", name="ck_follow_up_proposals_status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    # Not a ForeignKey — see class docstring.
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Denormalized traceability copy (derivable from anchor_gmail_message_id
    # via gmail_messages.thread_id) — kept for cheap querying without a
    # join, not part of this row's identity. Not a ForeignKey — see class
    # docstring.
    gmail_thread_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    anchor_gmail_message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )

    eligibility_reason: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    missing_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # S7E-008 (Codex remediation): the single canonical external recipient
    # this follow-up would be sent to, derived and VALIDATED (non-empty,
    # unambiguous, not self, well-formed, no CRLF) from the anchor
    # OUTBOUND message's own `to_addresses` at proposal-build time — see
    # app.services.follow_up_recipient.derive_canonical_recipient. Never
    # re-derived from the anchor at send time; see
    # FollowUpApprovalRecord.pinned_recipient for why the approval pins a
    # verbatim copy instead of a live re-read.
    recipient: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")

    # S7E-009 (Codex remediation): SHA-256 hex digest over every trusted
    # input this proposal's content depends on (job_id, thread_id, anchor,
    # trusted job title/company, candidate name/profile_version, recipient,
    # language, provider, generator_version) — see
    # app.services.follow_up.compute_follow_up_input_fingerprint. Part of
    # this row's UNIQUE identity (see __table_args__ above): re-evaluating
    # the same anchor after any of these inputs changed produces a NEW row
    # rather than returning a stale one, and an approval recorded against
    # an old fingerprint can never be reinterpreted as authorizing new
    # content (approvals key off `follow_up_proposal_id`, which changes
    # too).
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PROPOSED")
    # Always True for every row this stage writes — see class docstring.
    # Not DB-CHECK-enforced (mirrors ResponseDraftRecord.requires_human_review's
    # own convention) — enforced by construction in app/services/follow_up.py.
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class FollowUpApprovalRecord(Base):
    """An immutable human APPROVE/REJECT decision on one exact
    `FollowUpProposalRecord` (Stage 7E) — mirrors
    `ResponseDraftApprovalRecord` exactly (see that model's docstring for
    the full "pins the exact content" / "one decision per revision, ever"
    rationale, both of which apply here unchanged). `HUMAN APPROVAL = NO
    FOLLOW-UP SEND` is enforced the same way: see
    app/services/follow_up_send.py.
    """

    __tablename__ = "follow_up_approvals"
    __table_args__ = (
        UniqueConstraint("follow_up_proposal_id", name="uq_follow_up_approvals_follow_up_proposal"),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')", name="ck_follow_up_approvals_decision_valid"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    follow_up_proposal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_up_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized traceability only (the proposal's own anchor_gmail_message_id)
    # — same convention as ResponseDraftApprovalRecord.gmail_message_id.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    pinned_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    pinned_body: Mapped[str] = mapped_column(Text, nullable=False)
    # S7E-008 (Codex remediation): verbatim copy of the approved proposal's
    # own validated `recipient` at decision time — same "pin what the human
    # saw" rationale as pinned_subject/pinned_body above, and the exact
    # value app.services.follow_up_send sends to (never re-derived live at
    # send time) — see that module's module docstring.
    pinned_recipient: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class FollowUpSendRecord(Base):
    """The DB-enforced atomic arbiter of "has this approved follow-up
    already been sent, or is a send currently in flight" (Stage 7E) —
    mirrors `ResponseDraftSendRecord` exactly, including the CAS-guarded
    PENDING/SENT/FAILED/UNCERTAIN state machine and the fail-closed,
    never-auto-retried `UNCERTAIN` terminal state for an ambiguous SMTP
    outcome — see that model's docstring for the full rationale, which
    applies here unchanged. See app/services/follow_up_send.py for the
    orchestration that drives these transitions.
    """

    __tablename__ = "follow_up_sends"
    __table_args__ = (
        UniqueConstraint("follow_up_proposal_id", name="uq_follow_up_sends_follow_up_proposal"),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_follow_up_sends_status_valid",
        ),
        CheckConstraint("attempt_count > 0", name="ck_follow_up_sends_attempt_count_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    follow_up_proposal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_up_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approval_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_up_approvals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized traceability only — see FollowUpApprovalRecord's own
    # convention.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # S7E-010 (Codex remediation, crash/CAS recovery): False for the entire
    # window between winning the PENDING claim and the instant right
    # before `OutboundEmailProvider.send` is actually invoked; flipped to
    # True by a dedicated CAS (`app.db.follow_up_approval_repository
    # .begin_transmission`) immediately before that call, in the same
    # request that will make it. A row found PENDING with
    # `send_attempted=False` is PROVABLY pre-transmission — the process
    # that claimed it crashed (or never got that far) before any network
    # call was made, so it is always safe to let a later request take over
    # (see `app.services.follow_up_send._resolve_existing_send_record`).
    # A row found PENDING with `send_attempted=True` means transmission may
    # already be underway (either a live concurrent request, or a crash
    # mid-send) — indistinguishable from here, so it is NEVER retried;
    # instead it is moved to the fail-closed terminal `UNCERTAIN` state.
    # `begin_transmission`'s own CAS (`WHERE send_attempted=False`) is what
    # makes "who gets to actually call the provider" mutually exclusive
    # between concurrent requests — this column is the single source of
    # truth for that exclusivity, not `status` alone.
    send_attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class CandidateProfileRecord(Base):
    """The single, canonical Candidate Profile — the factual authority for
    every candidate-side claim a future CV/Bewerbung agent (Stage 6B+) may
    use (see app/db/candidate_profile_repository.py's module docstring for
    the full evidence-domain-separation rule: Candidate Profile / Job data
    / Company Research must never be mixed implicitly).

    Singleton, DB-enforced (Stage 6A section 20): `id` is fixed to 1 via a
    CHECK constraint, not just application convention — a second row can
    never be inserted (id=1 collides with the existing PK; any other id
    value violates the CHECK). This is a local, single-user tool with no
    multi-user requirement today; a deterministic singleton row is the
    minimal robust design rather than either a bare `PROFILE_ID = 1`
    Python constant (no DB enforcement) or a speculative multi-profile
    schema nothing in this project needs yet.

    `professional_summary`/`career_goal`/`target_roles` live here (not on
    CandidateJobPreferencesRecord) — they describe who the candidate *is*
    (a résumé-adjacent self-description), not a job-search *preference*
    like salary/relocation/remote work, which get their own table (see
    CandidateJobPreferencesRecord's docstring).
    """

    __tablename__ = "candidate_profiles"
    __table_args__ = (CheckConstraint("id = 1", name="ck_candidate_profiles_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Deliberately no email/phone/ID-number/banking/birth-date fields —
    # Stage 6A's spec scopes identity to what's listed here; contact-detail
    # fields are a documented future addition, not guessed at now (section 3).
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    professional_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    location_city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location_country: Mapped[str | None] = mapped_column(String(200), nullable=True)

    professional_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    career_goal: Mapped[str] = mapped_column(Text, default="", nullable=False)
    target_roles_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # CP-M-02: per-field provenance for the top-level fields above — JSON
    # dict of {field_name: {"source": ..., "confidence": ...}}, only ever
    # containing entries for fields that have actually been set via PATCH
    # (see app/db/candidate_profile_repository.py's apply_candidate_profile_patch
    # and app/models/candidate_profile.py's TOP_LEVEL_TRUST_FIELDS /
    # FieldTrust / is_top_level_fact_usable_for_generation).
    field_trust_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    skills: Mapped[list["CandidateSkillRecord"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    experiences: Mapped[list["CandidateExperienceRecord"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    education: Mapped[list["CandidateEducationRecord"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    certifications: Mapped[list["CandidateCertificationRecord"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    projects: Mapped[list["CandidateProjectRecord"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    languages: Mapped[list["CandidateLanguageRecord"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    job_preferences: Mapped["CandidateJobPreferencesRecord | None"] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )


class CandidateSkillRecord(Base):
    """A single structured skill claim (Stage 6A section 5) — never a
    free-text blob. `normalized_name` (NFKC + whitespace-collapse + strip +
    casefold, see app/db/candidate_profile_repository.py's
    normalize_text_identity) is the DB-enforced dedup identity within one
    profile; `name` keeps the candidate's own display casing/spelling.
    """

    __tablename__ = "candidate_skills"
    __table_args__ = (
        UniqueConstraint(
            "candidate_profile_id", "normalized_name", name="uq_candidate_skills_profile_name"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(20), default="OTHER", nullable=False)
    # Never inferred from mere keyword appearance — UNKNOWN unless the
    # candidate explicitly states a proficiency level.
    proficiency: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    years_experience: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_used_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Provenance (Stage 6A section 12/13) — see
    # app/models/candidate_profile.py's SourceType/FactConfidence/
    # is_usable_for_generation for the single rule future CV generation
    # must apply before treating this fact as usable.
    source: Mapped[str] = mapped_column(String(30), default="MANUAL_ENTRY", nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), default="CONFIRMED", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="skills")


class CandidateExperienceRecord(Base):
    """A single work-experience entry (Stage 6A section 6).
    responsibilities/achievements/technologies are JSON-as-Text lists
    (matching this project's established pattern — see
    CompanyResearchRecord) containing only what was explicitly entered for
    *this* entry; nothing here is auto-populated from a skill or
    technology recorded elsewhere in the profile.
    """

    __tablename__ = "candidate_experiences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False
    )
    company: Mapped[str] = mapped_column(String(300), nullable=False)
    job_title: Mapped[str] = mapped_column(String(300), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsibilities_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    achievements_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    technologies_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="MANUAL_ENTRY", nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), default="CONFIRMED", nullable=False)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="experiences")


class CandidateEducationRecord(Base):
    """Supports incomplete education (Stage 6A section 7) — `completed` is
    a plain, independently-provided boolean, never inferred from the
    presence/absence of end_date.
    """

    __tablename__ = "candidate_education"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False
    )
    institution: Mapped[str] = mapped_column(String(300), nullable=False)
    program: Mapped[str | None] = mapped_column(String(300), nullable=True)
    degree: Mapped[str | None] = mapped_column(String(200), nullable=True)
    field_of_study: Mapped[str | None] = mapped_column(String(300), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="MANUAL_ENTRY", nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), default="CONFIRMED", nullable=False)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="education")


class CandidateCertificationRecord(Base):
    """Stage 6A section 8. `status` defaults to UNKNOWN — completion is
    never assumed merely because a certification name was entered.
    """

    __tablename__ = "candidate_certifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(300), nullable=True)
    issued_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    credential_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="MANUAL_ENTRY", nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), default="CONFIRMED", nullable=False)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="certifications")


class CandidateProjectRecord(Base):
    """Portfolio project claims (Stage 6A section 9). Nothing here is
    populated by inspecting a candidate's actual GitHub/other repositories
    — every field is candidate-approved information entered through the
    API. Automated repository ingestion is an explicitly out-of-scope
    future feature, not part of Stage 6A.
    """

    __tablename__ = "candidate_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(String(300), nullable=True)
    technologies_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    repository_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    demo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    highlights_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="MANUAL_ENTRY", nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), default="CONFIRMED", nullable=False)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="projects")


class CandidateLanguageRecord(Base):
    """Stage 6A section 10. `level` defaults to UNKNOWN and is never
    upgraded automatically.
    """

    __tablename__ = "candidate_languages"
    __table_args__ = (
        UniqueConstraint(
            "candidate_profile_id",
            "normalized_language",
            name="uq_candidate_languages_profile_language",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_language: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    certificate: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="MANUAL_ENTRY", nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), default="CONFIRMED", nullable=False)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="languages")


class CandidateJobPreferencesRecord(Base):
    """Job-search preferences (Stage 6A section 11) — deliberately a
    separate table from CandidateProfileRecord's résumé-fact fields:
    salary/relocation/remote-work preferences describe what the candidate
    is *looking for*, not a factual claim about who they are or what
    they've done, and future CV/Bewerbung generation must never treat the
    two the same way (a "preference" is never itself a résumé fact to
    state as true). 1:1 with the profile — enforced by the UNIQUE
    constraint on candidate_profile_id below, not just by only ever
    creating one row in practice.
    """

    __tablename__ = "candidate_job_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    preferred_locations_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    remote_preference: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    employment_types_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    minimum_salary: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Tri-state (nullable Boolean): None = never stated, distinct from an
    # explicit True/False — never invent an unstated preference.
    relocation: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    travel: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="job_preferences")


class CandidateJobMatchRecord(Base):
    """A computed, cached Candidate Profile <-> Job match analysis (Stage
    6B) — see app/agents/candidate_job_matcher.py for the deterministic
    algorithm and app/db/candidate_job_match_repository.py for cache
    identity / concurrency handling.

    Deliberately not linked to JobRecord or CandidateProfileRecord via a
    ForeignKey — same rationale as CompanyResearchRecord's own docstring:
    this table's cache identity (job_id + candidate_profile_version +
    job_snapshot_fingerprint + algorithm_version) already needs to survive
    the *current* CandidateProfileRecord moving on to a later version
    without invalidating history (section 17: an old match analysis must
    keep showing which profile version produced it), which is the opposite
    of what an ON DELETE CASCADE FK relationship is for.

    `analysis_json` holds the full serialized CandidateJobMatchData (every
    requirement/relevant-entity/claim/warning) — the several duplicated
    scalar columns below exist purely so cache-identity lookups and score
    filtering don't require deserializing that blob (spec section 20:
    "normalized core metadata + JSON structured analysis").
    """

    __tablename__ = "candidate_job_matches"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "candidate_profile_version",
            "job_snapshot_fingerprint",
            "algorithm_version",
            name="uq_candidate_job_matches_cache_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    candidate_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Content fingerprint of the job fields that feed matching (title,
    # description, skill lists) — NOT JobRecord.fingerprint (that is a
    # dedup *identity* key, a different concept; see
    # app/db/repositories.py's _fingerprint). See
    # app/db/candidate_job_match_repository.py's
    # compute_job_snapshot_fingerprint.
    job_snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    # Traceability only (Stage 6B section 19) — never a FK, mirroring
    # CompanyResearchRecord's own "deliberately not linked" precedent;
    # company research content never feeds scoring in v1.
    company_research_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    overall_score: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_score: Mapped[int] = mapped_column(Integer, nullable=False)
    required_skill_score: Mapped[int] = mapped_column(Integer, nullable=False)
    preferred_skill_score: Mapped[int] = mapped_column(Integer, nullable=False)

    analysis_json: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class CandidateCVDraftRecord(Base):
    """An immutable, computed Tailored CV Draft snapshot (Stage 6C) — see
    app/agents/cv_adapter.py for the deterministic selection/ordering
    algorithm and app/db/candidate_cv_draft_repository.py for cache
    identity / concurrency handling.

    **Immutability (section 32).** A draft is never updated in place —
    when the profile, job, match, or cv_adapter_version changes, a NEW row
    is created (see the UNIQUE constraint below); old drafts remain
    exactly as generated, forever showing which profile version/job
    snapshot/match/adapter algorithm produced them. No UPDATE statement
    anywhere in this project ever targets this table.

    Deliberately not linked to JobRecord or CandidateJobMatchRecord via a
    ForeignKey — same rationale as CandidateJobMatchRecord's own
    docstring: this table's cache identity must survive the referenced
    match/profile/job moving on to a later state without cascading
    deletes, which is the opposite of what an FK relationship is for.
    """

    __tablename__ = "candidate_cv_drafts"
    __table_args__ = (
        UniqueConstraint(
            "match_id",
            "cv_adapter_version",
            name="uq_candidate_cv_drafts_cache_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # The one specific persisted CandidateJobMatchRecord.id this draft is
    # pinned to (Stage 6C section 5) — never "whatever GET .../match
    # currently returns."
    match_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Snapshot pins, copied from the pinned match at draft-generation time
    # (sections 6/7/8/9) — the match itself already pins job content and
    # profile version, so these are traceability copies, not independent
    # identity components (the UNIQUE constraint above deliberately keys
    # only on match_id + cv_adapter_version, per section 33's explicit
    # "do not duplicate redundant identity components" instruction).
    candidate_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    job_snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    match_algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    cv_adapter_version: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="DRAFT", nullable=False)
    draft_json: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class BewerbungDraftRecord(Base):
    """An immutable, provider-generated Bewerbung (cover letter) draft
    snapshot (Stage 6D) — see app/services/bewerbung.py for orchestration
    and app/agents/bewerbung_generator.py for the evidence-packet/
    validation rules a provider's output must satisfy before a row is ever
    written here.

    **No cache-identity UNIQUE constraint (unlike CandidateJobMatchRecord/
    CandidateCVDraftRecord) — deliberate (Stage 6D section 35).**
    LLM/provider output can legitimately vary between calls with identical
    pinned inputs, and regeneration is intentional; every successful
    BewerbungService.generate() call always inserts a new row rather than
    reusing one keyed by a cache identity.

    Deliberately not linked to JobRecord/CandidateCVDraftRecord/
    CandidateJobMatchRecord via a ForeignKey — same rationale as
    CandidateCVDraftRecord's own docstring: this table's snapshot pins must
    survive the referenced draft/match/profile moving on to a later state
    without cascading deletes.
    """

    __tablename__ = "bewerbung_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # The one specific persisted CandidateCVDraftRecord.id this Bewerbung is
    # pinned to (Stage 6D section 3) — never "whatever GET .../cv-draft
    # currently returns."
    cv_draft_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Traceability copy of the pinned CV draft's own match_id (section 4) —
    # the CV draft already pins job content/profile version transitively,
    # so this is not an independent identity component.
    match_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    candidate_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    job_snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    match_algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    cv_adapter_version: Mapped[str] = mapped_column(String(20), nullable=False)
    bewerbung_generator_version: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="DRAFT", nullable=False)
    draft_json: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ApplicationPackageReviewRecord(Base):
    """The human-in-the-loop review of one exact, pinned pair of Stage
    6C/6D drafts (Stage 6E) — see app/services/review_package.py for
    orchestration and app/agents/review_package_builder.py for the
    pin/consistency rules a (cv_draft_id, bewerbung_draft_id) pair must
    satisfy before a row is ever written here.

    **Deliberately mutable — unlike every other Stage 6B/6C/6D table.**
    `status`/`review_version`/`has_manual_overrides`/decision columns are
    updated in place via CAS (compare-and-swap) UPDATEs conditioned on
    `id` + `status='PENDING_REVIEW'` + `review_version=<expected>` (see
    app/db/review_package_repository.py's `create_revision`/
    `decide_review`) — Stage 6E explicitly requires real state transitions
    (PENDING_REVIEW -> APPROVED/REJECTED), unlike 6B/6C/6D's pure
    insert-only immutable snapshots. The actual reviewed content lives in
    ApplicationPackageReviewRevisionRecord rows, which ARE insert-only and
    immutable — this row is only ever a status/version header pointing at
    the current state of that history.

    Deliberately not linked to JobRecord/CandidateCVDraftRecord/
    BewerbungDraftRecord via a ForeignKey — same rationale as
    BewerbungDraftRecord's own docstring: this table's snapshot pins must
    survive the referenced draft/match/profile moving on to a later state
    without cascading deletes. An approved review is a permanent audit
    artifact (spec section 54) and must never be cascade-deleted merely
    because a later job/draft/match/profile change/deletion occurs
    upstream.
    """

    __tablename__ = "application_package_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    cv_draft_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    bewerbung_draft_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    match_id: Mapped[int] = mapped_column(Integer, nullable=False)

    candidate_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    job_snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    match_algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    cv_adapter_version: Mapped[str] = mapped_column(String(20), nullable=False)
    bewerbung_generator_version: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING_REVIEW")
    review_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    has_manual_overrides: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Set exactly once, at approval time — see
    # ApplicationPackageReviewRevisionRecord's docstring for why this
    # pins one specific immutable revision rather than "whatever the
    # latest revision happens to be".
    approved_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ApplicationPackageReviewRevisionRecord(Base):
    """One immutable snapshot of reviewed CV/Bewerbung content (Stage 6E
    section 21/22) — a review's full history is the ordered set of these
    rows for its `review_id`, never overwritten. Revision 1 is always an
    exact copy of the pinned 6C/6D drafts' human-visible fields (all
    `origin="MACHINE"`, see app.agents.review_package_builder.
    build_initial_reviewed_cv/build_initial_reviewed_bewerbung); each
    subsequent revision is produced by one accepted PATCH.

    No FK to ApplicationPackageReviewRecord — same historical-snapshot
    rationale as that table's own docstring.
    """

    __tablename__ = "application_package_review_revisions"
    __table_args__ = (
        UniqueConstraint(
            "review_id",
            "revision_number",
            name="uq_application_package_review_revisions_number",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)

    reviewed_cv_json: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_bewerbung_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Redundant with per-field `origin` tags inside reviewed_cv_json/
    # reviewed_bewerbung_json — persisted separately anyway (spec section
    # 14) for cheap inspection without deserializing either blob.
    manual_override_paths_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    edit_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AutomationRunRecord(Base):
    """Stage 8A: one persisted, end-to-end orchestrated job-search cycle
    for one account — the audit trail of WHEN an automation run happened,
    WHICH existing steps (collectors) it coordinated, and what each one's
    outcome was. This table owns none of the actual collection/scoring
    logic — `app.services.automation.run_automation_cycle` orchestrates
    the SAME `app.api.routes._run_bundesagentur`/`_run_xing` helpers the
    individual `/collectors/*/run` endpoints and the Telegram control
    center already call, so this is coordination bookkeeping only, never
    a second implementation of collection/dedup/scoring.

    **`account_key` (mirrors GMAIL-002's convention elsewhere).** The
    normalized `GMAIL_USERNAME` this run was scoped to — every read is
    filtered by it, so a later account change can never leak or mix a
    previous account's run history, exactly like Gmail/follow-up records.

    **`status`** is one of `RUNNING` / `COMPLETED` / `PARTIAL` / `FAILED`:
    `RUNNING` from creation until the orchestrator finishes; `COMPLETED`
    if every coordinated step succeeded; `PARTIAL` if at least one
    succeeded and at least one did not; `FAILED` if none did. Terminal
    states are never left ambiguous — the caller always ends the run in
    exactly one of these three.

    **`results_json`** is a JSON object keyed by step name (e.g.
    `"bundesagentur"`, `"xing"`), each value shaped like
    `{"status": "ok"|"not_configured"|"failed", "counters": {...} |
    null, "error_type": str | null}` — `counters` is the step's own
    already-existing return shape (e.g. `{"fetched":.., "created":..,
    ...}`), never re-derived or duplicated here. `error_summary` is a
    short, human-readable, SANITIZED string built only from step names
    and `type(exc).__name__` — mirrors this project's GMAIL-003
    convention (app/providers/email/base.py's `GmailProviderError`
    docstring) of never persisting/returning raw upstream exception text,
    which could otherwise carry back a server-echoed detail.

    **Concurrency (fail-closed, not serialized).** `uq_automation_runs_one_running_per_account`
    is a PARTIAL unique index — `UNIQUE(account_key) WHERE status =
    'RUNNING'` — the sole arbiter of "at most one RUNNING run per
    account at a time", enforced by the database, not a Python
    check-then-act read. S8A-001 (Codex re-review): declared with BOTH
    `sqlite_where` and `postgresql_where` (identical predicate) so this
    project's SQLite deployment and a future PostgreSQL one describe the
    exact same semantics — see
    tests/test_automation_run_index_dialects.py for the dialect-level
    proof (both compile the same `WHERE status = 'RUNNING'` clause).
    `app.db.automation_repository.create_running_run` always attempts a
    plain INSERT first and lets a concurrent duplicate fail on this
    constraint (caught and translated into
    `AutomationRunAlreadyInProgressError`, mapped to 409) — the same
    INSERT + IntegrityError-catch idiom used throughout this project
    (e.g. `app.db.follow_up_approval_repository.claim_send_attempt`).

    **Crash recovery via an ownership-aware lease (S8A-002, Codex
    re-review).** `lease_holder`/`lease_expires_at` mirror Stage 7E's
    per-Gmail-thread lock (`GmailThreadRecord.lock_holder`/
    `lock_expires_at`, see that model's docstring) — a generic,
    time-bounded "who currently owns this RUNNING row, until when"
    primitive. `app.db.automation_repository.create_running_run` claims
    both atomically on INSERT; `app.services.automation`'s
    `_RunLeaseHeartbeat` renews `lease_expires_at` periodically for as
    long as `run_automation_cycle` is executing, via the DEDICATED
    `renew_run_lease` CAS (never `create_running_run`'s own claim path —
    a renewal must fail the instant the lease has expired, even if
    nobody else has taken it over yet; it must never silently resume as
    though ownership had been continuous — see `renew_run_lease`'s
    docstring). A process that crashes (or whose heartbeat otherwise
    stops) leaves the lease to expire on its own schedule rather than
    blocking that account's automation forever: the NEXT request to
    start a run finds a stale RUNNING row (`lease_expires_at` in the
    past) and atomically reconciles it to `FAILED` before claiming a
    fresh run — a LIVE lease (not yet expired) still fails closed
    (`AutomationRunAlreadyInProgressError`, 409), exactly like before;
    only a genuinely expired one is ever reclaimed, and never silently —
    the reconciliation itself is a CAS, so at most one concurrent
    requester ever wins it.
    """

    __tablename__ = "automation_runs"
    __table_args__ = (
        Index(
            "uq_automation_runs_one_running_per_account",
            "account_key",
            unique=True,
            sqlite_where=text("status = 'RUNNING'"),
            postgresql_where=text("status = 'RUNNING'"),
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')",
            name="ck_automation_runs_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    results_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # S8A-002: ownership-aware lease — see class docstring's "Crash
    # recovery via an ownership-aware lease" section.
    lease_holder: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AutomationScheduleRecord(Base):
    """Stage 8B: one persisted row per account tracking when its next
    scheduled `app.services.automation.run_automation_cycle` cycle is
    due. This table owns ONLY schedule-arbitration state -- it never
    duplicates anything already recorded by `AutomationRunRecord`
    (status/results/lease); `last_run_id` is a plain informational
    pointer to the most recent triggered run, not a foreign key this
    table depends on for correctness.

    **One row per account.** `account_key` is DB-enforced UNIQUE
    (`uq_automation_schedules_account_key`) -- the same normalized
    identity `AutomationRunRecord.account_key` already uses (mirrors
    GMAIL-002's convention elsewhere in this project).

    **Multi-process claim (fail-closed, at-most-once).** `next_run_at` is
    the sole arbiter of "is a slot due right now, and has it already been
    claimed". `app.db.automation_schedule_repository.claim_due_schedule`
    performs a single atomic `UPDATE ... WHERE account_key = :account_key
    AND next_run_at = :observed_next_run_at AND next_run_at <= :now` --
    exactly one concurrent claimer's UPDATE can ever match a given
    `next_run_at` value (every winning claim immediately moves
    `next_run_at` into the future as part of the SAME statement), so two
    scheduler processes racing the same due slot can never both trigger a
    cycle. This is a plain conditional UPDATE (portable SQL), not a
    SELECT ... FOR UPDATE row lock or any SQLite-only construct -- it
    compiles and behaves identically on SQLite and PostgreSQL (see
    tests/test_automation_schedule_migration.py).

    **Coalescing, not catch-up.** A winning claim advances `next_run_at`
    to `now + interval_seconds` (relative to the claim moment), never to
    `previous_next_run_at + interval_seconds` repeated N times -- so a
    worker that was offline for many missed intervals runs exactly ONE
    cycle on return, then resumes its normal cadence. There is
    deliberately no backlog/replay queue.

    **At-most-once, not exactly-once (accepted tradeoff).** `next_run_at`
    is advanced as part of the SAME claim UPDATE that "reserves" the
    slot, BEFORE `run_automation_cycle` is ever called. If the worker
    process crashes after a successful claim but before (or during) that
    call, this single slot is simply skipped -- there is no separate
    heartbeat/lease/retry subsystem for schedule claims in Stage 8B (the
    existing `AutomationRunRecord.lease_holder`/`lease_expires_at` from
    Stage 8A already protects the ACTUAL run's ownership once it starts;
    stacking a second lease system on top of the schedule slot itself
    would be redundant machinery for a failure mode already bounded by
    "wait for the next normal interval").

    **No first-run backlog.** `app.db.automation_schedule_repository.get_or_create_schedule`
    seeds a brand-new row with `next_run_at = now` (immediately due) --
    the deterministic "first scheduler start creates a due schedule, can
    run immediately" semantics this stage documents, not an arbitrary
    delay before the very first cycle.
    """

    __tablename__ = "automation_schedules"
    __table_args__ = (UniqueConstraint("account_key", name="uq_automation_schedules_account_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("automation_runs.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class AutomationMailProgressRecord(Base):
    """Stage 8D: one persisted row per account tracking how far the
    Gmail response-draft cycle and the follow-up proposal cycle have
    each progressed -- crash-safe resumption state for
    `app.services.automation_gmail`/`app.services.automation_follow_up`,
    analogous to `AutomationScheduleRecord`'s "one row per account"
    shape but for IN-RUN processing cursors rather than run-scheduling.

    **Why a persisted cursor, not an in-memory "messages touched this
    run" list.** `app.providers.email.imap.GmailImapProvider` (Stage 7A)
    deliberately skips already-persisted UIDs on every sync (see
    `get_known_uids`) -- so if a process crashes AFTER a Gmail message is
    persisted but BEFORE it is analyzed/drafted, an in-memory-only
    "touched this run" design would lose that message forever: no later
    sync would ever re-surface it. `gmail_after_message_id` instead
    anchors progress to `GmailMessageRecord.id` itself (already-persisted,
    monotonic, account-scoped), so a crash mid-cycle only ever costs
    re-attempting the SAME message range on the next run -- never a
    silently skipped one.

    **Two independent cursors, two independent semantics.** Gmail message
    processing is a one-directional catch-up scan (oldest-first, never
    reset -- see `gmail_after_message_id`'s own column comment) — new
    messages simply get larger ids and are naturally reached later. Follow
    -up scanning is a bounded ROUND-ROBIN over currently-`APPLIED` jobs
    (see `follow_up_after_job_id`'s own column comment) -- it must
    eventually wrap back to the oldest APPLIED job so a job that becomes
    newly due (purely because time passed) is periodically re-checked,
    which a one-directional cursor could never achieve on its own.

    **`account_key` (mirrors GMAIL-002's convention elsewhere).** DB
    -enforced UNIQUE (`uq_automation_mail_progress_account_key`) -- one
    row per account, same normalized identity
    `AutomationRunRecord.account_key` already uses.

    **Concurrency (CAS, not blind writes) — S8D-PROGRESS.**
    `app.db.automation_mail_progress_repository.advance_gmail_cursor`/
    `advance_follow_up_cursor` perform a single atomic `UPDATE ... WHERE
    account_key = :account_key AND <cursor column> = :expected_cursor`
    -- exactly like `claim_due_schedule`'s CAS. `AutomationRunRecord`'s
    own lease/heartbeat already prevents ordinary same-account
    concurrency, but a run that briefly continues after losing its lease
    (heartbeat renewal races a replacement run's claim) must never be
    able to silently clobber a NEWER owner's already-advanced progress --
    the CAS's `expected_cursor` mismatch makes that fail closed instead,
    exactly mirroring the run-level lease's own fail-closed contract.

    No email content lives here -- only technical integer cursors.
    """

    __tablename__ = "automation_mail_progress"
    __table_args__ = (
        UniqueConstraint("account_key", name="uq_automation_mail_progress_account_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False)

    # AUD-004 (Astra R3): NO LONGER READ OR WRITTEN by
    # app.services.automation_gmail.prepare_gmail_response_drafts -- an
    # `id`-ordered watermark is unsafe under PostgreSQL's
    # commit-visibility semantics (a lower-`id` row can commit after a
    # higher-`id` one, permanently skipping it under `id > watermark`).
    # Message completion tracking moved to a durable PER-MESSAGE marker,
    # `GmailMessageRecord.automation_processed_at` (see that column's own
    # docstring). This column/its CAS primitive (`advance_gmail_cursor`)
    # are kept, unchanged and still tested, purely as a still-valid
    # generic building block -- dropping either would be a destructive
    # migration for zero benefit; nothing in this project's runtime path
    # relies on this column's value anymore.
    gmail_after_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The largest JobRecord.id (status=APPLIED) fully evaluated for
    # follow-up eligibility so far, in the CURRENT round-robin pass, for
    # this account. NULL means "start the pass from the oldest currently
    # -APPLIED job". UNLIKE gmail_after_message_id, this IS periodically
    # reset back to NULL (via the same CAS primitive) once a full pass
    # reaches the end of currently-APPLIED jobs -- see
    # app.services.automation_follow_up's module docstring for the
    # wrap-around rationale (a job's follow-up eligibility can become due
    # purely because time passed, with no new Gmail activity to "wake" it,
    # so periodic re-scanning from the top is required).
    follow_up_after_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class TelegramDigestDeliveryRecord(Base):
    """Stage 8E: one persisted delivery attempt of the optional DAILY
    Telegram digest, for one account, for one LOCAL calendar date (in
    `settings.telegram_daily_digest_timezone`) -- the sole idempotency
    primitive that keeps a restart, or more than one standalone
    `python -m app.scheduler` worker process, from sending the same
    day's digest twice. Mirrors `ResponseDraftSendRecord`'s claim/CAS
    shape (see `app.db.response_draft_approval_repository`) applied to
    "send one Telegram message" instead of "send one email".

    **Identity is `(account_key, digest_date)` — `UNIQUE`, enforced by
    the database, not a Python check-then-act read.**
    `app.db.telegram_digest_repository.claim_delivery` always attempts a
    plain INSERT (status=`PENDING`) first and lets a concurrent duplicate
    fail on this constraint -- the same INSERT + IntegrityError-catch
    idiom used throughout this project (e.g.
    `app.db.response_draft_approval_repository.claim_send_attempt`). Only
    the caller that wins the INSERT may attempt the actual Telegram send
    for that date; every other caller (a second worker process, or the
    same worker's next poll tick before the date rolls over) observes the
    existing row and does not resend.

    **`status`.** `PENDING` from the winning claim until the send
    attempt resolves. `SENT` once Telegram's API has POSITIVELY
    confirmed delivery (2xx response) -- terminal, never resent for this
    date. `FAILED` for a send that DEFINITELY did not reach Telegram (a
    connection-level error, or a non-2xx response Telegram itself
    returned) -- the one non-terminal state: a LATER poll tick on the
    SAME still-current date may retry it via `retry_delivery`'s CAS
    (`FAILED -> PENDING`), mirroring
    `app.db.response_draft_approval_repository.retry_send_attempt`.
    `UNCERTAIN` for a send whose outcome could not be proven either way
    (e.g. a network timeout after the request may already have reached
    Telegram) -- terminal, exactly like `ResponseDraftSendRecord.UNCERTAIN`
    (see that model's docstring): retrying an uncertain send risks a
    real duplicate message landing in the operator's chat, so this
    project's conservative default is to never automatically retry it.
    The digest simply resumes normally on the NEXT calendar date.

    **No email content, no job data, no draft/response text lives
    here** -- only `account_key` (an identity, not a secret -- mirrors
    every other `account_key` column in this project), `digest_date`,
    delivery bookkeeping, and a truncated, sanitized `last_error` (never
    a raw Telegram response body or bot token).
    """

    __tablename__ = "telegram_digest_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "account_key", "digest_date", name="uq_telegram_digest_deliveries_account_date"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_telegram_digest_deliveries_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    digest_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
