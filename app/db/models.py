from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
