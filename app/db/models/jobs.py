"""ORM models: jobs (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_jobs_fingerprint"),
        # lookup by the source's own reference. Not unique:
        # before the fingerprint moves to (source, source_reference), one BA
        # posting can still exist as several rows (different URLs).
        Index("ix_jobs_source_source_reference", "source", "source_reference"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # XING only: the base (company|title|location) fingerprint shared by every
    # distinguished sibling of one e-mail identity. NULL for every other source and for
    # rows stored before the column existed (those are members of their own base
    # group via `fingerprint`). Never an authority over `fingerprint`.
    identity_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
    # Stage 10 finding: source-reported posting type (e.g. Bundesagentur's
    # "stellenangebotsart") — persisted so a later re-score of the SAME
    # fingerprint (e.g. a manual POST /jobs/score call that doesn't know
    # to resupply this derived field) can never silently lose the
    # classification that originally excluded a non-employment listing
    # from APPLY. See app/db/repositories.py::_apply_job_update_fields
    # (only overwritten by a non-None value, exactly like `description`)
    # and app.agents.posting_classifier.
    posting_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # the source's own reference (BA `referenznummer`), as
    # sent by the source or derived from a BA job URL by the backfill. Set
    # once and never blanked or replaced by a later observation (see
    # app/db/repositories.py::_apply_job_update_fields).
    source_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Stage 22 P1: structured facts the source states (see app.models.job.Job
    # and app.collectors.bundesagentur.structured_facts). Refreshed by every
    # observation that carries them; an omitted value keeps the stored one.
    workplace_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    workplace_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    workplace_postal_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    workplace_region: Mapped[str | None] = mapped_column(String(40), nullable=True)
    workplace_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_office_possible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    home_office_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    home_office_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quereinstieg_suitable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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
    lookup.

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


class JobCommuteCheckRecord(Base):
    """Stage 17C: one verified car-route measurement from the candidate's
    commute origin to a job's workplace. Only a stored check makes a job's
    distance PASS or FAIL; without one the distance stays UNVERIFIED (a
    straight-line estimate is never treated as the car route). Rows are only
    ever inserted; the newest check of a job is authoritative."""

    __tablename__ = "job_commute_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    origin_label: Mapped[str] = mapped_column(String(300), nullable=False)
    # The origin coordinates the route was measured from: a route from other
    # coordinates (origin moved, label unchanged) is stale.
    origin_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_label: Mapped[str] = mapped_column(String(300), nullable=False)
    destination_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    car_route_km: Mapped[float] = mapped_column(Float, nullable=False)
    car_route_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class JobFitAssessmentRecord(Base):
    """Stage 21: the cached `assess_job` result of one vacancy (one current
    row per job). `input_key` hashes every input the assessment reads
    (`app/services/fit_cache.py`); a row whose key differs from the current
    one is stale and must never be shown as the vacancy's tier. Written only
    by the offline `scripts/stage21_assess_vacancies.py`; the UI only reads."""

    __tablename__ = "job_fit_assessments"
    __table_args__ = (
        CheckConstraint(
            # Stage 22 P1 (e3b7c9d1f4a6) adds STRETCH
            "fit_tier IN ('GOOD_MATCH', 'PARTIAL_MATCH', 'STRETCH', 'NEEDS_OPERATOR', "
            "'NOT_SUITABLE')",
            name="ck_job_fit_assessments_fit_tier",
        ),
        CheckConstraint(
            "decision IN ('BLOCKED', 'NEEDS_VERIFICATION', 'CANDIDATE')",
            name="ck_job_fit_assessments_decision",
        ),
        Index("ix_job_fit_assessments_tier_job", "fit_tier", "job_id"),
        Index("ix_job_fit_assessments_input_key", "input_key"),
    )

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    input_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fit_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    criteria_json: Mapped[str] = mapped_column(Text, nullable=False)
    tier_reasons_json: Mapped[str] = mapped_column(Text, nullable=False)
    work_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
