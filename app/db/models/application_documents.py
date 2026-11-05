"""ORM models: application documents (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


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
