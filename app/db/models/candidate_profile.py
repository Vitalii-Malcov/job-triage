"""ORM models: candidate profile (import via `app.db.models`)."""

from datetime import UTC, date, datetime

from sqlalchemy import (
    DDL,
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
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


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
    # Stage 17B: the confirmed contact details used in Bewerbungen. Kept out of
    # `field_trust_json` (whose keys are the Stage 6A top-level fields and are
    # mirrored by the Stage 13B transfer snapshot); their own provenance is
    # `contact_trust_json` ({field: {"source", "confidence"}}).
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    contact_website_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_trust_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    professional_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    career_goal: Mapped[str] = mapped_column(Text, default="", nullable=False)
    target_roles_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # per-field provenance for the top-level fields above — JSON
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

    # Stage 17B: search configuration (see app/models/candidate_search.py's
    # CandidateSearchSettings). Every column is optional: an unset value means
    # "never stated" and the job-fit assessment reports UNKNOWN, never a guess.
    home_city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    home_postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    home_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    commute_origin_label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    commute_origin_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    commute_origin_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_commute_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commute_measure: Mapped[str | None] = mapped_column(String(20), nullable=True)
    relocation_regions_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    remote_work_scope: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    remote_work_from_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    maximum_salary: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_negotiable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    earliest_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    primary_roles_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    secondary_roles_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # JUNIOR = only entry-level postings are targeted (feeds the Stage 11A
    # collector seniority gate); UNKNOWN = not stated.
    target_seniority: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    # Bundesagentur `was` terms; empty = BUNDESAGENTUR_SEARCH_KEYWORDS.
    search_keywords_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    search_trust_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    profile: Mapped["CandidateProfileRecord"] = relationship(back_populates="job_preferences")


class CandidateProfileRevisionRecord(Base):
    """Stage 17B: append-only audit of Candidate Profile changes made by the
    Stage 17 configuration CLI -- who, why, which fields, and the before/after
    snapshot of the changed values. UPDATE/DELETE are refused by database
    triggers (migration a9d3f6b2c4e8), like application_events."""

    __tablename__ = "candidate_profile_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    changed_fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)


# The Stage 17 migration (a9d3f6b2c4e8) creates exactly these objects.
CANDIDATE_PROFILE_REVISION_IMMUTABILITY_DDL: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "CREATE TRIGGER trg_candidate_profile_revisions_no_update BEFORE UPDATE ON "
        "candidate_profile_revisions "
        "BEGIN SELECT RAISE(ABORT, 'candidate_profile_revisions is append-only'); END",
        "CREATE TRIGGER trg_candidate_profile_revisions_no_delete BEFORE DELETE ON "
        "candidate_profile_revisions "
        "BEGIN SELECT RAISE(ABORT, 'candidate_profile_revisions is append-only'); END",
    ),
    "postgresql": (
        "CREATE FUNCTION candidate_profile_revisions_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
        "'candidate_profile_revisions is append-only' "
        "USING ERRCODE = 'restrict_violation'; END $$",
        "CREATE TRIGGER trg_candidate_profile_revisions_append_only BEFORE UPDATE OR DELETE "
        "ON candidate_profile_revisions FOR EACH ROW "
        "EXECUTE FUNCTION candidate_profile_revisions_append_only()",
    ),
}


_CANDIDATE_PROFILE_REVISION_IMMUTABILITY_DROP: dict[str, tuple[str, ...]] = {
    "postgresql": ("DROP FUNCTION IF EXISTS candidate_profile_revisions_append_only()",),
}


for _dialect, _statements in CANDIDATE_PROFILE_REVISION_IMMUTABILITY_DDL.items():
    for _statement in _statements:
        event.listen(
            CandidateProfileRevisionRecord.__table__,
            "after_create",
            DDL(_statement).execute_if(dialect=_dialect),
        )


for _dialect, _statements in _CANDIDATE_PROFILE_REVISION_IMMUTABILITY_DROP.items():
    for _statement in _statements:
        event.listen(
            CandidateProfileRevisionRecord.__table__,
            "after_drop",
            DDL(_statement).execute_if(dialect=_dialect),
        )


class CandidateProfileRevisionImmutableError(RuntimeError):
    """An UPDATE or DELETE of a CandidateProfileRevisionRecord was attempted."""


@event.listens_for(CandidateProfileRevisionRecord, "before_update")
def _refuse_revision_update(mapper, connection, target) -> None:
    raise CandidateProfileRevisionImmutableError("candidate profile revisions are append-only")


@event.listens_for(CandidateProfileRevisionRecord, "before_delete")
def _refuse_revision_delete(mapper, connection, target) -> None:
    raise CandidateProfileRevisionImmutableError("candidate profile revisions are append-only")
