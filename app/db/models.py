from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

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
