"""Pure computation for Stage 6E review packages: initial content
construction from pinned 6C/6D drafts, patch application, manual-override
detection, and the pin/staleness/structural error classes
`app.services.review_package.ReviewPackageService` raises.

**No LLM, no network, no provider call (spec section 45/46).** Every
function here is a pure transformation over already-loaded Pydantic
models — exactly like `app.agents.cv_adapter`/`app.agents.candidate_job_matcher`.
Human edits come from the API request body only.

**Source consistency, not re-matching (spec section 5).** `verify_source_pair`
checks that a pinned CV draft and Bewerbung draft actually belong together
(same job, same match, same profile/job/algorithm/adapter versions) — it
never re-derives or re-validates the matching/CV-adaptation/generation
logic those stages already performed.
"""

from app.models.bewerbung import BewerbungDraft
from app.models.cv_draft import TailoredCVDraft
from app.models.review_package import (
    BewerbungContentPatch,
    CVContentPatch,
    ReviewedBewerbungContent,
    ReviewedBewerbungParagraph,
    ReviewedCVContent,
    ReviewSectionOrderField,
    ReviewTextField,
)


class ReviewCVDraftNotFoundError(Exception):
    """`cv_draft_id` does not correspond to any persisted
    `CandidateCVDraftRecord` — mapped to 404."""

    def __init__(self, cv_draft_id: int) -> None:
        self.cv_draft_id = cv_draft_id
        super().__init__(f"No CV draft found with id={cv_draft_id}.")


class ReviewCVDraftJobMismatchError(Exception):
    """`cv_draft_id` exists but its `job_id` does not equal the job_id in
    the request URL — 422, mirroring
    `app.agents.cv_adapter.CVDraftMatchJobMismatchError`'s exact rationale
    (a structurally inconsistent request, not a missing/stale resource).
    """

    def __init__(self, cv_draft_id: int, job_id: int) -> None:
        self.cv_draft_id = cv_draft_id
        self.job_id = job_id
        super().__init__(f"CV draft {cv_draft_id} does not belong to job {job_id}.")


class ReviewBewerbungDraftNotFoundError(Exception):
    """`bewerbung_draft_id` does not correspond to any persisted
    `BewerbungDraftRecord` — mapped to 404."""

    def __init__(self, bewerbung_draft_id: int) -> None:
        self.bewerbung_draft_id = bewerbung_draft_id
        super().__init__(f"No Bewerbung draft found with id={bewerbung_draft_id}.")


class ReviewBewerbungDraftJobMismatchError(Exception):
    """`bewerbung_draft_id` exists but its `job_id` does not equal the
    job_id in the request URL — 422."""

    def __init__(self, bewerbung_draft_id: int, job_id: int) -> None:
        self.bewerbung_draft_id = bewerbung_draft_id
        self.job_id = job_id
        super().__init__(f"Bewerbung draft {bewerbung_draft_id} does not belong to job {job_id}.")


class ReviewSourceMismatchError(Exception):
    """The CV draft and Bewerbung draft are each individually valid for
    this job but do not form a coherent pair (spec section 5) — e.g. the
    Bewerbung draft was generated from a *different* CV draft. Carries
    only field *names* that disagree, never their values — 422.
    """

    def __init__(self, mismatched_fields: list[str]) -> None:
        self.mismatched_fields = mismatched_fields
        super().__init__("CV draft and Bewerbung draft do not form a consistent pair.")


class ReviewProfileChangedError(Exception):
    """The current `CandidateProfileRecord.profile_version` no longer
    equals the pinned version (spec section 6/7) — 409. Carries only
    version numbers, never profile content. Raised both at review creation
    (against the CV draft's pinned version) and at approval time (against
    the review's own pinned version).
    """

    def __init__(self, pinned_profile_version: int, current_profile_version: int) -> None:
        self.pinned_profile_version = pinned_profile_version
        self.current_profile_version = current_profile_version
        super().__init__("Candidate profile changed since this snapshot was pinned.")


class ReviewCurrentProfileMissingError(Exception):
    """The current Candidate Profile authority (the singleton
    `CandidateProfileRecord`) does not exist at all — 409, distinct from
    `ReviewProfileChangedError` (which means "it exists but at a different
    version"). A missing profile can never be silently treated as "still
    fresh" nor "safe to recreate and compare" (blocker fix): the reviewed
    package was produced against a specific historical profile state that
    no longer has any current counterpart to verify against, so freshness
    is simply unknowable and the operation must fail closed. Raised at
    both review creation and approval — see
    `app.db.candidate_profile_repository.get_candidate_profile`, the
    create-on-miss-free lookup this check is built on.
    """

    def __init__(self) -> None:
        super().__init__("No current Candidate Profile exists to verify freshness against.")


class ReviewJobChangedError(Exception):
    """The job's current content fingerprint no longer equals the pinned
    fingerprint (spec section 6/7) — 409. No job content in the message.
    """

    def __init__(self) -> None:
        super().__init__("Job changed since this snapshot was pinned.")


class ReviewCurrentJobMissingError(Exception):
    """The current Job authority (`JobRecord`) does not exist at all —
    409, distinct from `ReviewJobChangedError` (which means "it exists but
    its content fingerprint changed"). Same fail-closed rationale as
    `ReviewCurrentProfileMissingError`: a deleted job has no current
    fingerprint to compare against, so freshness cannot be established and
    the operation must not proceed. Raised at both review creation and
    approval.
    """

    def __init__(self) -> None:
        super().__init__("No current Job exists to verify freshness against.")


class ReviewNotFoundError(Exception):
    """`review_id` does not correspond to any persisted review package —
    404."""

    def __init__(self, review_id: int) -> None:
        self.review_id = review_id
        super().__init__(f"No review package found with id={review_id}.")


class ReviewNotPendingError(Exception):
    """PATCH/approve/reject attempted on a review that already left
    `PENDING_REVIEW` (spec section 10/28) — 409. Terminal decisions are
    immutable; carries only the current status, never review content.
    """

    def __init__(self, review_id: int, current_status: str) -> None:
        self.review_id = review_id
        self.current_status = current_status
        super().__init__(f"Review {review_id} is no longer PENDING_REVIEW.")


class ReviewVersionConflictError(Exception):
    """`expected_review_version` no longer matches the stored
    `review_version` (spec section 11/35) — 409. Carries only the two
    version numbers.
    """

    def __init__(self, expected_review_version: int, current_review_version: int) -> None:
        self.expected_review_version = expected_review_version
        self.current_review_version = current_review_version
        super().__init__("Review version is stale.")


class ReviewManualOverrideAcknowledgmentRequiredError(Exception):
    """Approval attempted while `has_manual_overrides=true` without
    `acknowledge_manual_overrides=true` (spec section 15) — 422. The human
    must knowingly approve manually changed content.
    """

    def __init__(self, review_id: int) -> None:
        self.review_id = review_id
        super().__init__(f"Review {review_id} has manual overrides that were not acknowledged.")


class ReviewParagraphIndexError(Exception):
    """A `BewerbungParagraphPatch.index` does not reference an existing
    paragraph in the current revision — 422."""

    def __init__(self, index: int) -> None:
        self.index = index
        super().__init__(f"No body paragraph exists at index={index}.")


class ReviewRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors `CandidateCVDraftConsistencyError`'s "should
    never happen, but fail loudly if it does" stance. E.g. a review record
    whose `current_revision`/`approved_revision_id` cannot be resolved to
    an actual revision row.
    """


def verify_source_pair(cv_record, bewerbung_record) -> None:
    """Structural pairing check (spec section 5): the CV draft and
    Bewerbung draft must actually belong together. Raises
    `ReviewSourceMismatchError` naming every disagreeing field at once
    (not just the first one found), so a caller sees the full picture in
    one response.
    """
    mismatched: list[str] = []
    if bewerbung_record.cv_draft_id != cv_record.id:
        mismatched.append("cv_draft_id")
    if bewerbung_record.match_id != cv_record.match_id:
        mismatched.append("match_id")
    if bewerbung_record.candidate_profile_version != cv_record.candidate_profile_version:
        mismatched.append("candidate_profile_version")
    if bewerbung_record.job_snapshot_fingerprint != cv_record.job_snapshot_fingerprint:
        mismatched.append("job_snapshot_fingerprint")
    if bewerbung_record.match_algorithm_version != cv_record.match_algorithm_version:
        mismatched.append("match_algorithm_version")
    if bewerbung_record.cv_adapter_version != cv_record.cv_adapter_version:
        mismatched.append("cv_adapter_version")
    if mismatched:
        raise ReviewSourceMismatchError(mismatched)


def build_initial_reviewed_cv(cv_draft: TailoredCVDraft) -> ReviewedCVContent:
    """Build the review-space CV content at review-creation time — every
    field starts as `origin="MACHINE"`, copied verbatim from the pinned,
    already evidence-filtered 6C draft (spec section 2/8's "prefer using
    6C as the fact source", extended to 6E)."""
    title_value = (
        cv_draft.header.professional_title.value if cv_draft.header.professional_title else None
    )
    summary_value = cv_draft.professional_summary.value if cv_draft.professional_summary else None
    return ReviewedCVContent(
        professional_title=ReviewTextField(value=title_value, origin="MACHINE"),
        professional_summary=ReviewTextField(value=summary_value, origin="MACHINE"),
        section_order=ReviewSectionOrderField(value=list(cv_draft.section_order), origin="MACHINE"),
    )


def build_initial_reviewed_bewerbung(bewerbung_draft: BewerbungDraft) -> ReviewedBewerbungContent:
    """Build the review-space Bewerbung content at review-creation time —
    every field starts as `origin="MACHINE"`, copied verbatim from the
    pinned, evidence-bound 6D draft."""
    return ReviewedBewerbungContent(
        subject=ReviewTextField(value=bewerbung_draft.subject, origin="MACHINE"),
        salutation=ReviewTextField(value=bewerbung_draft.salutation, origin="MACHINE"),
        opening=ReviewTextField(value=bewerbung_draft.opening, origin="MACHINE"),
        body_paragraphs=[
            ReviewedBewerbungParagraph(
                text=paragraph.text,
                original_source_claim_ids=list(paragraph.source_claim_ids),
                origin="MACHINE",
            )
            for paragraph in bewerbung_draft.body_paragraphs
        ],
        closing=ReviewTextField(value=bewerbung_draft.closing, origin="MACHINE"),
        signature_name=ReviewTextField(value=bewerbung_draft.signature_name, origin="MACHINE"),
    )


def apply_cv_patch(
    current: ReviewedCVContent, patch: CVContentPatch
) -> tuple[ReviewedCVContent, list[str]]:
    """Apply a CV content patch. Fields absent from `patch` (per
    `patch.model_fields_set`) are carried forward unchanged, including
    their existing `origin` — a field already `USER_EDIT` from an earlier
    revision stays `USER_EDIT` even if this particular patch doesn't touch
    it (spec section 21's cumulative revision history).
    """
    fields_set = patch.model_fields_set
    professional_title = current.professional_title
    professional_summary = current.professional_summary
    section_order = current.section_order
    changed_paths: list[str] = []

    if "professional_title" in fields_set:
        professional_title = ReviewTextField(value=patch.professional_title, origin="USER_EDIT")
        changed_paths.append("cv.professional_title")
    if "professional_summary" in fields_set:
        professional_summary = ReviewTextField(value=patch.professional_summary, origin="USER_EDIT")
        changed_paths.append("cv.professional_summary")
    if "section_order" in fields_set:
        section_order = ReviewSectionOrderField(
            value=list(patch.section_order or []), origin="USER_EDIT"
        )
        changed_paths.append("cv.section_order")

    new_content = ReviewedCVContent(
        professional_title=professional_title,
        professional_summary=professional_summary,
        section_order=section_order,
    )
    return new_content, changed_paths


def apply_bewerbung_patch(
    current: ReviewedBewerbungContent, patch: BewerbungContentPatch
) -> tuple[ReviewedBewerbungContent, list[str]]:
    """Apply a Bewerbung content patch. Same carry-forward semantics as
    `apply_cv_patch`. `original_source_claim_ids` on an edited paragraph is
    preserved from the paragraph it replaces (which itself traces back to
    the pristine machine-generated paragraph at that position, however
    many times it has since been edited) — never recomputed, never
    dropped (spec section 20).
    """
    fields_set = patch.model_fields_set
    subject = current.subject
    salutation = current.salutation
    opening = current.opening
    closing = current.closing
    signature_name = current.signature_name
    body_paragraphs = list(current.body_paragraphs)
    changed_paths: list[str] = []

    if "subject" in fields_set:
        subject = ReviewTextField(value=patch.subject, origin="USER_EDIT")
        changed_paths.append("bewerbung.subject")
    if "salutation" in fields_set:
        salutation = ReviewTextField(value=patch.salutation, origin="USER_EDIT")
        changed_paths.append("bewerbung.salutation")
    if "opening" in fields_set:
        opening = ReviewTextField(value=patch.opening, origin="USER_EDIT")
        changed_paths.append("bewerbung.opening")
    if "closing" in fields_set:
        closing = ReviewTextField(value=patch.closing, origin="USER_EDIT")
        changed_paths.append("bewerbung.closing")
    if "signature_name" in fields_set:
        signature_name = ReviewTextField(value=patch.signature_name, origin="USER_EDIT")
        changed_paths.append("bewerbung.signature_name")
    if "body_paragraphs" in fields_set and patch.body_paragraphs is not None:
        for item in patch.body_paragraphs:
            if item.index >= len(body_paragraphs) or item.index < 0:
                raise ReviewParagraphIndexError(item.index)
            existing = body_paragraphs[item.index]
            body_paragraphs[item.index] = ReviewedBewerbungParagraph(
                text=item.text,
                original_source_claim_ids=existing.original_source_claim_ids,
                origin="USER_EDIT",
            )
            changed_paths.append(f"bewerbung.body_paragraphs[{item.index}]")

    new_content = ReviewedBewerbungContent(
        subject=subject,
        salutation=salutation,
        opening=opening,
        body_paragraphs=body_paragraphs,
        closing=closing,
        signature_name=signature_name,
    )
    return new_content, changed_paths


def collect_manual_override_paths(
    cv: ReviewedCVContent, bewerbung: ReviewedBewerbungContent
) -> list[str]:
    """The full, cumulative list of every field currently in `USER_EDIT`
    state (spec section 14) — derived by scanning `origin` tags, not by
    accumulating per-patch diffs, so it always reflects the *current*
    merged content regardless of how many revisions produced it.
    """
    paths: list[str] = []
    if cv.professional_title.origin == "USER_EDIT":
        paths.append("cv.professional_title")
    if cv.professional_summary.origin == "USER_EDIT":
        paths.append("cv.professional_summary")
    if cv.section_order.origin == "USER_EDIT":
        paths.append("cv.section_order")
    if bewerbung.subject.origin == "USER_EDIT":
        paths.append("bewerbung.subject")
    if bewerbung.salutation.origin == "USER_EDIT":
        paths.append("bewerbung.salutation")
    if bewerbung.opening.origin == "USER_EDIT":
        paths.append("bewerbung.opening")
    if bewerbung.closing.origin == "USER_EDIT":
        paths.append("bewerbung.closing")
    if bewerbung.signature_name.origin == "USER_EDIT":
        paths.append("bewerbung.signature_name")
    for index, paragraph in enumerate(bewerbung.body_paragraphs):
        if paragraph.origin == "USER_EDIT":
            paths.append(f"bewerbung.body_paragraphs[{index}]")
    return paths


def compute_has_manual_overrides(
    cv: ReviewedCVContent, bewerbung: ReviewedBewerbungContent
) -> bool:
    """True if any field currently differs in provenance from the
    pristine machine-generated content (spec section 14)."""
    return bool(collect_manual_override_paths(cv, bewerbung))


def compute_verification_state(has_manual_overrides: bool) -> str:
    """Spec section 43: a clear semantic state distinguishing
    fully-machine-evidence-bound content from human-overridden content,
    so future submission code can never mistake the two."""
    return "HUMAN_OVERRIDDEN" if has_manual_overrides else "EVIDENCE_BOUND"
