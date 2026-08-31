"""Stage 6E — pure unit tests for
app.agents.review_package_builder: initial content construction, patch
application, manual-override detection/carry-forward, and source-pair
consistency checks. No DB/HTTP involved.
"""

from datetime import UTC, datetime

import pytest

from app.agents.review_package_builder import (
    ReviewParagraphIndexError,
    ReviewSourceMismatchError,
    apply_bewerbung_patch,
    apply_cv_patch,
    build_initial_reviewed_bewerbung,
    build_initial_reviewed_cv,
    collect_manual_override_paths,
    compute_has_manual_overrides,
    compute_verification_state,
    verify_source_pair,
)
from app.models.bewerbung import BewerbungDraft, BewerbungParagraph
from app.models.cv_draft import CVHeader, CVTopLevelFact, TailoredCVDraft
from app.models.review_package import BewerbungContentPatch, CVContentPatch


def _cv_draft(**overrides) -> TailoredCVDraft:
    defaults = dict(
        id=1,
        created_at=datetime.now(UTC),
        job_id=1,
        match_id=1,
        candidate_profile_version=1,
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        status="DRAFT",
        header=CVHeader(
            professional_title=CVTopLevelFact(
                value="Junior Python Developer",
                source_id=1,
                source_field="professional_title",
                profile_version=1,
            )
        ),
        professional_summary=CVTopLevelFact(
            value="Backend-focused developer.",
            source_id=1,
            source_field="professional_summary",
            profile_version=1,
        ),
        section_order=["HEADER", "SUMMARY", "SKILLS"],
        projects_emphasis="STANDARD",
        skills=[],
        experience=[],
        projects=[],
        education=[],
        certifications=[],
        languages=[],
        warnings=[],
    )
    defaults.update(overrides)
    return TailoredCVDraft(**defaults)


def _bewerbung_draft(**overrides) -> BewerbungDraft:
    from app.models.bewerbung import BewerbungProviderPlan

    defaults = dict(
        id=1,
        created_at=datetime.now(UTC),
        job_id=1,
        cv_draft_id=1,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        bewerbung_generator_version="v1",
        provider="deterministic",
        subject="Bewerbung als Python Developer",
        salutation="Sehr geehrte Damen und Herren,",
        opening="mit Interesse habe ich Ihre Anzeige gelesen.",
        body_paragraphs=[
            BewerbungParagraph(
                text="Ich bringe Kenntnisse in Python mit.",
                source_claim_ids=["candidate_skill:1"],
            ),
            BewerbungParagraph(
                text="Zudem verfüge ich über Deutschkenntnisse.",
                source_claim_ids=["candidate_language:1"],
            ),
        ],
        closing="Ich freue mich auf ein Gespräch.",
        signature_name="Anna Example",
        plan=BewerbungProviderPlan(
            opening_style="ROLE_INTEREST",
            paragraphs=[{"kind": "EVIDENCE", "claim_ids": ["candidate_skill:1"]}],
            closing_style="INTERVIEW_INTEREST",
        ),
    )
    defaults.update(overrides)
    return BewerbungDraft(**defaults)


class _FakeCVDraftRecord:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# --- verify_source_pair ------------------------------------------------


def test_verify_source_pair_accepts_consistent_pair():
    cv = _FakeCVDraftRecord(
        id=7,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
    )
    bewerbung = _FakeCVDraftRecord(
        cv_draft_id=7,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
    )
    verify_source_pair(cv, bewerbung)  # must not raise


def test_verify_source_pair_rejects_wrong_cv_draft_id():
    cv = _FakeCVDraftRecord(
        id=7,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
    )
    bewerbung = _FakeCVDraftRecord(
        cv_draft_id=999,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
    )
    with pytest.raises(ReviewSourceMismatchError) as exc_info:
        verify_source_pair(cv, bewerbung)
    assert "cv_draft_id" in exc_info.value.mismatched_fields


def test_verify_source_pair_reports_every_mismatch_at_once():
    cv = _FakeCVDraftRecord(
        id=7,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
    )
    bewerbung = _FakeCVDraftRecord(
        cv_draft_id=999,
        match_id=2,
        candidate_profile_version=2,
        job_snapshot_fingerprint="fp-2",
        match_algorithm_version="v2",
        cv_adapter_version="v2",
    )
    with pytest.raises(ReviewSourceMismatchError) as exc_info:
        verify_source_pair(cv, bewerbung)
    assert set(exc_info.value.mismatched_fields) == {
        "cv_draft_id",
        "match_id",
        "candidate_profile_version",
        "job_snapshot_fingerprint",
        "match_algorithm_version",
        "cv_adapter_version",
    }


# --- build_initial_reviewed_cv / build_initial_reviewed_bewerbung ---------


def test_build_initial_reviewed_cv_copies_machine_content():
    cv_draft = _cv_draft()
    reviewed = build_initial_reviewed_cv(cv_draft)
    assert reviewed.professional_title.value == "Junior Python Developer"
    assert reviewed.professional_title.origin == "MACHINE"
    assert reviewed.professional_summary.value == "Backend-focused developer."
    assert reviewed.section_order.value == ["HEADER", "SUMMARY", "SKILLS"]
    assert reviewed.section_order.origin == "MACHINE"


def test_build_initial_reviewed_cv_handles_untrusted_title():
    cv_draft = _cv_draft(header=CVHeader(professional_title=None), professional_summary=None)
    reviewed = build_initial_reviewed_cv(cv_draft)
    assert reviewed.professional_title.value is None
    assert reviewed.professional_title.origin == "MACHINE"


def test_build_initial_reviewed_bewerbung_copies_machine_content_and_claim_ids():
    bewerbung_draft = _bewerbung_draft()
    reviewed = build_initial_reviewed_bewerbung(bewerbung_draft)
    assert reviewed.subject.value == "Bewerbung als Python Developer"
    assert reviewed.subject.origin == "MACHINE"
    assert len(reviewed.body_paragraphs) == 2
    assert reviewed.body_paragraphs[0].origin == "MACHINE"
    assert reviewed.body_paragraphs[0].original_source_claim_ids == ["candidate_skill:1"]


# --- apply_cv_patch ------------------------------------------------------


def test_apply_cv_patch_only_touches_provided_fields():
    cv_draft = _cv_draft()
    original = build_initial_reviewed_cv(cv_draft)
    patch = CVContentPatch(professional_summary="Edited summary.")
    updated, changed = apply_cv_patch(original, patch)

    assert updated.professional_summary.value == "Edited summary."
    assert updated.professional_summary.origin == "USER_EDIT"
    # Untouched fields carry forward unchanged, including origin.
    assert updated.professional_title.value == original.professional_title.value
    assert updated.professional_title.origin == "MACHINE"
    assert changed == ["cv.professional_summary"]


def test_apply_cv_patch_preserves_prior_user_edit_when_untouched():
    cv_draft = _cv_draft()
    original = build_initial_reviewed_cv(cv_draft)
    first, _ = apply_cv_patch(original, CVContentPatch(professional_title="New Title"))
    # A second patch that doesn't touch professional_title must not reset
    # its origin back to MACHINE.
    second, changed = apply_cv_patch(first, CVContentPatch(professional_summary="Another edit."))
    assert second.professional_title.value == "New Title"
    assert second.professional_title.origin == "USER_EDIT"
    assert changed == ["cv.professional_summary"]


def test_apply_cv_patch_explicit_null_is_applied():
    cv_draft = _cv_draft()
    original = build_initial_reviewed_cv(cv_draft)
    patch = CVContentPatch.model_validate({"professional_summary": None})
    updated, changed = apply_cv_patch(original, patch)
    assert updated.professional_summary.value is None
    assert updated.professional_summary.origin == "USER_EDIT"
    assert changed == ["cv.professional_summary"]


# --- apply_bewerbung_patch -------------------------------------------------


def test_apply_bewerbung_patch_edits_one_paragraph_by_index():
    bewerbung_draft = _bewerbung_draft()
    original = build_initial_reviewed_bewerbung(bewerbung_draft)
    patch = BewerbungContentPatch(body_paragraphs=[{"index": 0, "text": "Ich habe AWS-Erfahrung."}])
    updated, changed = apply_bewerbung_patch(original, patch)

    assert updated.body_paragraphs[0].text == "Ich habe AWS-Erfahrung."
    assert updated.body_paragraphs[0].origin == "USER_EDIT"
    # original_source_claim_ids preserved even though text changed — spec
    # section 20: an edited paragraph's original claim ids never prove the
    # new text.
    assert updated.body_paragraphs[0].original_source_claim_ids == ["candidate_skill:1"]
    # The untouched second paragraph is unchanged.
    assert updated.body_paragraphs[1].text == original.body_paragraphs[1].text
    assert updated.body_paragraphs[1].origin == "MACHINE"
    assert changed == ["bewerbung.body_paragraphs[0]"]


def test_apply_bewerbung_patch_out_of_range_index_is_rejected():
    bewerbung_draft = _bewerbung_draft()
    original = build_initial_reviewed_bewerbung(bewerbung_draft)
    patch = BewerbungContentPatch(body_paragraphs=[{"index": 99, "text": "..."}])
    with pytest.raises(ReviewParagraphIndexError):
        apply_bewerbung_patch(original, patch)


def test_apply_bewerbung_patch_preserves_claim_ids_across_repeated_edits():
    bewerbung_draft = _bewerbung_draft()
    original = build_initial_reviewed_bewerbung(bewerbung_draft)
    first, _ = apply_bewerbung_patch(
        original, BewerbungContentPatch(body_paragraphs=[{"index": 0, "text": "First edit."}])
    )
    second, _ = apply_bewerbung_patch(
        first, BewerbungContentPatch(body_paragraphs=[{"index": 0, "text": "Second edit."}])
    )
    assert second.body_paragraphs[0].text == "Second edit."
    assert second.body_paragraphs[0].original_source_claim_ids == ["candidate_skill:1"]


def test_apply_bewerbung_patch_signature_and_subject():
    bewerbung_draft = _bewerbung_draft()
    original = build_initial_reviewed_bewerbung(bewerbung_draft)
    patch = BewerbungContentPatch(subject="New subject", signature_name="Different Name")
    updated, changed = apply_bewerbung_patch(original, patch)
    assert updated.subject.value == "New subject"
    assert updated.subject.origin == "USER_EDIT"
    assert updated.signature_name.value == "Different Name"
    assert set(changed) == {"bewerbung.subject", "bewerbung.signature_name"}


# --- manual override detection -------------------------------------------


def test_no_edits_means_no_manual_overrides():
    cv = build_initial_reviewed_cv(_cv_draft())
    bewerbung = build_initial_reviewed_bewerbung(_bewerbung_draft())
    assert compute_has_manual_overrides(cv, bewerbung) is False
    assert collect_manual_override_paths(cv, bewerbung) == []
    assert compute_verification_state(False) == "EVIDENCE_BOUND"


def test_any_edit_means_manual_overrides():
    cv = build_initial_reviewed_cv(_cv_draft())
    bewerbung = build_initial_reviewed_bewerbung(_bewerbung_draft())
    edited_bewerbung, _ = apply_bewerbung_patch(
        bewerbung, BewerbungContentPatch(opening="New opening.")
    )
    assert compute_has_manual_overrides(cv, edited_bewerbung) is True
    assert "bewerbung.opening" in collect_manual_override_paths(cv, edited_bewerbung)
    assert compute_verification_state(True) == "HUMAN_OVERRIDDEN"
