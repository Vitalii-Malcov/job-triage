"""Application Package Review DTOs (Stage 6E) — the human-in-the-loop
control layer over Stage 6C (Tailored CV Draft) and Stage 6D (Bewerbung
Draft).

**Not a submission layer (spec section 2/44).** An `APPROVED` review
package is a recorded human decision about one exact, pinned pair of
immutable source drafts — never an application action. Nothing in this
module or `app.services.review_package`/`app.agents.review_package_builder`
sends anything, opens anything, or mutates `ApplicationStatus`. A future
Stage (submission) must consume an `APPROVED` package explicitly via
`GET /jobs/{job_id}/approved-package` — never a raw "latest" CV/Bewerbung
draft, and never a `PENDING_REVIEW` package.

**Narrower v1 editable surface (spec section 18), by design.** Editing the
full structured CV (skills/experience/projects/education/certifications/
languages as evidence-bound lists) safely is out of scope for v1 — the
editable surface is deliberately limited to free-text framing fields
(`professional_title`, `professional_summary`, `section_order` for the CV;
`subject`/`salutation`/`opening`/`body_paragraphs`/`closing`/
`signature_name` for the Bewerbung). Everything else is shown read-only in
the review from the pinned source draft itself.

**Origin tracking, not fake evidence (spec section 16/17).** Every
editable field carries its own `origin: "MACHINE" | "USER_EDIT"` — a human
edit is stored and shown exactly as a human edit, never disguised as
6A/6B/6C/6D-verified evidence. `ReviewedBewerbungParagraph.
original_source_claim_ids` is retained even after an edit specifically so
an edited paragraph's claim ids are never mistaken for still proving the
edited text (spec section 20).

**Immutable revision history (spec section 21).** Every accepted PATCH
creates a new `ApplicationPackageReviewRevisionRecord` row; older revisions
are never overwritten. The review record itself is the one genuinely
mutable row this project introduces (`status`/`review_version`/
`has_manual_overrides`/decision metadata) — unlike 6B/6C/6D's pure
insert-only snapshot tables — because Stage 6E explicitly requires real
state transitions (`PENDING_REVIEW -> APPROVED`/`REJECTED`).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ReviewStatus = Literal["PENDING_REVIEW", "APPROVED", "REJECTED"]
ContentOrigin = Literal["MACHINE", "USER_EDIT"]
VerificationState = Literal["EVIDENCE_BOUND", "HUMAN_OVERRIDDEN"]


class ReviewTextField(BaseModel):
    """One editable free-text field, tagged with its own provenance
    (spec section 16) — never a bare string. `origin="MACHINE"` means this
    value still equals what the pinned source draft produced; any edit
    flips it to `"USER_EDIT"` permanently (never silently reset back).
    """

    value: str | None = None
    origin: ContentOrigin = "MACHINE"


class ReviewSectionOrderField(BaseModel):
    """The CV's editable section order, tagged with provenance the same
    way as `ReviewTextField` (it's a list, not text, hence a separate
    type)."""

    value: list[str] = Field(default_factory=list)
    origin: ContentOrigin = "MACHINE"


class ReviewedBewerbungParagraph(BaseModel):
    """One Bewerbung body paragraph in review space. `original_source_claim_ids`
    is copied from the pinned Stage 6D draft's own
    `BewerbungParagraph.source_claim_ids` at review-creation time and is
    **never updated** by a later edit (spec section 20) — it always
    answers "what evidence did the *original machine-generated* text for
    this position rely on", never "what evidence backs the current text".
    Once `origin` is `"USER_EDIT"`, `original_source_claim_ids` must not be
    read as proof of the current `text`.
    """

    text: str
    original_source_claim_ids: list[str] = Field(default_factory=list)
    origin: ContentOrigin = "MACHINE"


class ReviewedCVContent(BaseModel):
    """The v1 editable CV review surface — see module docstring for why
    this is deliberately narrower than the full `TailoredCVDraft`."""

    professional_title: ReviewTextField
    professional_summary: ReviewTextField
    section_order: ReviewSectionOrderField


class ReviewedBewerbungContent(BaseModel):
    """The v1 editable Bewerbung review surface (spec section 19) — every
    human-visible letter field."""

    subject: ReviewTextField
    salutation: ReviewTextField
    opening: ReviewTextField
    body_paragraphs: list[ReviewedBewerbungParagraph] = Field(default_factory=list)
    closing: ReviewTextField
    signature_name: ReviewTextField


class ReviewPackageData(BaseModel):
    """The computed content of one review package's current state —
    everything `app.services.review_package.ReviewPackageService` produces
    for a GET/POST/PATCH response, before `id`/timestamps are attached.
    Mirrors the `<X>Data -> <X>` split used throughout 6B/6C/6D.
    """

    job_id: int
    cv_draft_id: int
    bewerbung_draft_id: int
    match_id: int
    # Snapshot pins (spec section 4/5), copied verbatim from the pinned CV
    # draft at review-creation time — never recomputed independently.
    candidate_profile_version: int
    job_snapshot_fingerprint: str
    match_algorithm_version: str
    cv_adapter_version: str
    bewerbung_generator_version: str

    status: ReviewStatus
    review_version: int
    has_manual_overrides: bool
    verification_state: VerificationState

    reviewed_cv: ReviewedCVContent
    reviewed_bewerbung: ReviewedBewerbungContent
    # Flat, mechanically-derived list of every field path currently in
    # USER_EDIT state (spec section 14) — redundant with the per-field
    # `origin` tags above, kept for convenient API-consumer inspection.
    manual_override_paths: list[str] = Field(default_factory=list)

    decision_note: str | None = None
    decided_at: datetime | None = None
    # Set exactly once, at approval time — never recomputed, never changed
    # by a later action (spec section 33; no later action is possible
    # anyway, since PATCH is rejected once a review leaves PENDING_REVIEW).
    approved_revision_id: int | None = None


class ReviewPackage(ReviewPackageData):
    """GET/POST/PATCH response shape for
    /api/v1/jobs/{job_id}/review-package,
    /api/v1/review-packages/{review_id}, .../approve, .../reject, and
    /api/v1/jobs/{job_id}/approved-package.
    """

    id: int
    # The revision this response's `reviewed_cv`/`reviewed_bewerbung`
    # content actually came from — `approved_revision_id` once APPROVED,
    # otherwise the latest revision (see
    # app.db.review_package_repository.get_current_revision).
    current_revision_id: int
    created_at: datetime
    updated_at: datetime


class ReviewPackageCreateRequest(BaseModel):
    """POST /api/v1/jobs/{job_id}/review-package body. Both ids are
    required — no implicit "latest CV"/"latest Bewerbung" (spec section
    4); the exact pair is part of the review's identity.
    """

    cv_draft_id: int
    bewerbung_draft_id: int


class BewerbungParagraphPatch(BaseModel):
    """One targeted paragraph edit, by position. `index` must reference an
    existing paragraph in the current revision — PATCH edits paragraph
    text in place, it does not add or remove paragraphs (spec section 19's
    "narrower v1 editable surface" applied to the Bewerbung body too).
    """

    index: int = Field(ge=0)
    text: str


class CVContentPatch(BaseModel):
    """PATCH sub-document for the CV review surface. Uses
    `exclude_unset`-style semantics via `model_fields_set` (same convention
    as `CandidateProfilePatchRequest`): a field omitted from the request
    body is left completely unchanged; a field explicitly present
    (including explicit `null`) is applied and flips that field's `origin`
    to `USER_EDIT`.
    """

    professional_title: str | None = None
    professional_summary: str | None = None
    section_order: list[str] | None = None


class BewerbungContentPatch(BaseModel):
    """PATCH sub-document for the Bewerbung review surface. Same
    `model_fields_set` semantics as `CVContentPatch`. `body_paragraphs`, if
    present, is a sparse list of `{index, text}` edits applied to the
    current revision's paragraphs — not a wholesale list replacement (see
    `BewerbungParagraphPatch`'s docstring for why).
    """

    subject: str | None = None
    salutation: str | None = None
    opening: str | None = None
    body_paragraphs: list[BewerbungParagraphPatch] | None = None
    closing: str | None = None
    signature_name: str | None = None


class ReviewPackagePatchRequest(BaseModel):
    """PATCH /api/v1/review-packages/{review_id} body. `expected_review_version`
    is required (spec section 11/35) — optimistic concurrency, checked
    atomically against the stored `review_version` before any new revision
    is created.
    """

    expected_review_version: int
    cv_changes: CVContentPatch | None = None
    bewerbung_changes: BewerbungContentPatch | None = None
    edit_note: str | None = None


class ReviewPackageApproveRequest(BaseModel):
    """POST /api/v1/review-packages/{review_id}/approve body.
    `acknowledge_manual_overrides` is required to be `true` whenever the
    review currently has `has_manual_overrides=true` (spec section 15) —
    otherwise approval is rejected with a controlled 422, never a silent
    approval of unverified human-authored content.
    """

    expected_review_version: int
    acknowledge_manual_overrides: bool = False
    decision_note: str | None = None


class ReviewPackageRejectRequest(BaseModel):
    """POST /api/v1/review-packages/{review_id}/reject body."""

    expected_review_version: int
    decision_note: str | None = None
