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
