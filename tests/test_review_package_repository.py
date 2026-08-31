import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.review_package_builder import (
    build_initial_reviewed_bewerbung,
    build_initial_reviewed_cv,
)
from app.db.base import Base
from app.db.models import ApplicationPackageReviewRecord, ApplicationPackageReviewRevisionRecord
from app.db.review_package_repository import (
    ReviewCASConflict,
    create_review,
    create_revision,
    decide_review,
    get_current_revision,
    get_latest_approved_review_for_job,
    get_latest_review_for_job,
    get_latest_revision,
    get_review_by_id,
    to_review_package,
)
from tests.test_review_package_builder import _bewerbung_draft, _cv_draft


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _file_session_factory(tmp_path, name: str):
    db_path = tmp_path / name
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _create(db) -> tuple[ApplicationPackageReviewRecord, ApplicationPackageReviewRevisionRecord]:
    cv_draft = _cv_draft()
    bewerbung_draft = _bewerbung_draft()
    reviewed_cv = build_initial_reviewed_cv(cv_draft)
    reviewed_bewerbung = build_initial_reviewed_bewerbung(bewerbung_draft)
    return create_review(
        db,
        job_id=1,
        cv_draft_id=1,
        bewerbung_draft_id=1,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        bewerbung_generator_version="v1",
        reviewed_cv=reviewed_cv,
        reviewed_bewerbung=reviewed_bewerbung,
    )


# --- create / round trip ----------------------------------------------------


def test_create_review_persists_review_and_revision_one():
    db = _db()
    record, revision = _create(db)

    assert record.status == "PENDING_REVIEW"
    assert record.review_version == 1
    assert record.has_manual_overrides is False
    assert revision.review_id == record.id
    assert revision.revision_number == 1

    response = to_review_package(record, revision)
    assert response.status == "PENDING_REVIEW"
    assert response.review_version == 1
    assert response.verification_state == "EVIDENCE_BOUND"
    assert response.current_revision_id == revision.id


def test_get_review_by_id_round_trip():
    db = _db()
    record, _ = _create(db)
    found = get_review_by_id(db, record.id)
    assert found is not None
    assert found.id == record.id


def test_get_review_by_id_returns_none_for_unknown_id():
    db = _db()
    assert get_review_by_id(db, 999) is None


def test_get_latest_review_for_job_returns_most_recent():
    db = _db()
    first, _ = _create(db)
    second, _ = _create(db)
    latest = get_latest_review_for_job(db, 1)
    assert latest.id == second.id
    assert latest.id != first.id


def test_get_latest_review_for_job_returns_none_when_absent():
    db = _db()
    assert get_latest_review_for_job(db, 999) is None


def test_get_latest_revision_returns_most_recent_revision():
    db = _db()
    record, revision1 = _create(db)
    _, revision2 = create_revision(
        db,
        review_id=record.id,
        expected_review_version=1,
        reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
        reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
        has_manual_overrides=False,
        manual_override_paths=[],
        edit_note=None,
    )
    latest = get_latest_revision(db, record.id)
    assert latest.id == revision2.id
    assert latest.id != revision1.id


# --- create_revision: optimistic concurrency (CAS) --------------------------


def test_create_revision_increments_version_and_creates_new_row():
    db = _db()
    record, _ = _create(db)
    updated_record, revision2 = create_revision(
        db,
        review_id=record.id,
        expected_review_version=1,
        reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
        reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
        has_manual_overrides=True,
        manual_override_paths=["cv.professional_summary"],
        edit_note="tweaked summary",
    )
    assert updated_record.review_version == 2
    assert updated_record.has_manual_overrides is True
    assert revision2.revision_number == 2

    total = db.scalar(select(func.count()).select_from(ApplicationPackageReviewRevisionRecord))
    assert total == 2


def test_create_revision_stale_version_raises_cas_conflict():
    db = _db()
    record, _ = _create(db)
    create_revision(
        db,
        review_id=record.id,
        expected_review_version=1,
        reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
        reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
        has_manual_overrides=False,
        manual_override_paths=[],
        edit_note=None,
    )
    # Second caller still thinks version is 1 — must be rejected.
    with pytest.raises(ReviewCASConflict):
        create_revision(
            db,
            review_id=record.id,
            expected_review_version=1,
            reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
            reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
            has_manual_overrides=False,
            manual_override_paths=[],
            edit_note=None,
        )

    total = db.scalar(select(func.count()).select_from(ApplicationPackageReviewRevisionRecord))
    assert total == 2  # no lost/extra revision from the failed attempt


def test_create_revision_on_decided_review_raises_cas_conflict():
    db = _db()
    record, _ = _create(db)
    decide_review(
        db,
        review_id=record.id,
        expected_review_version=1,
        new_status="APPROVED",
        decision_note=None,
        approved_revision_id=get_latest_revision(db, record.id).id,
    )
    with pytest.raises(ReviewCASConflict):
        create_revision(
            db,
            review_id=record.id,
            expected_review_version=1,
            reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
            reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
            has_manual_overrides=False,
            manual_override_paths=[],
            edit_note=None,
        )


# --- decide_review: approve / reject CAS ------------------------------------


def test_decide_review_approve_pins_revision_and_sets_decided_at():
    db = _db()
    record, revision = _create(db)
    updated = decide_review(
        db,
        review_id=record.id,
        expected_review_version=1,
        new_status="APPROVED",
        decision_note="looks good",
        approved_revision_id=revision.id,
    )
    assert updated.status == "APPROVED"
    assert updated.approved_revision_id == revision.id
    assert updated.decided_at is not None
    assert updated.decision_note == "looks good"


def test_decide_review_reject_does_not_set_approved_revision_id():
    db = _db()
    record, _ = _create(db)
    updated = decide_review(
        db,
        review_id=record.id,
        expected_review_version=1,
        new_status="REJECTED",
        decision_note=None,
    )
    assert updated.status == "REJECTED"
    assert updated.approved_revision_id is None
    assert updated.decided_at is not None


def test_decide_review_stale_version_raises_cas_conflict():
    db = _db()
    record, revision = _create(db)
    with pytest.raises(ReviewCASConflict):
        decide_review(
            db,
            review_id=record.id,
            expected_review_version=99,
            new_status="APPROVED",
            decision_note=None,
            approved_revision_id=revision.id,
        )
    # Status must remain PENDING_REVIEW after a failed CAS attempt.
    reloaded = get_review_by_id(db, record.id)
    assert reloaded.status == "PENDING_REVIEW"


def test_decide_review_twice_raises_cas_conflict_second_time():
    db = _db()
    record, revision = _create(db)
    decide_review(
        db,
        review_id=record.id,
        expected_review_version=1,
        new_status="APPROVED",
        decision_note=None,
        approved_revision_id=revision.id,
    )
    with pytest.raises(ReviewCASConflict):
        decide_review(
            db,
            review_id=record.id,
            expected_review_version=1,
            new_status="REJECTED",
            decision_note=None,
        )
    reloaded = get_review_by_id(db, record.id)
    assert reloaded.status == "APPROVED"


# --- get_current_revision ----------------------------------------------------


def test_get_current_revision_returns_latest_when_pending():
    db = _db()
    record, revision1 = _create(db)
    _, revision2 = create_revision(
        db,
        review_id=record.id,
        expected_review_version=1,
        reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
        reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
        has_manual_overrides=False,
        manual_override_paths=[],
        edit_note=None,
    )
    reloaded = get_review_by_id(db, record.id)
    current = get_current_revision(db, reloaded)
    assert current.id == revision2.id
    assert current.id != revision1.id


def test_get_current_revision_returns_pinned_approved_revision_not_latest():
    """Even though PATCH is blocked once decided (so there's never a
    'later' revision in practice), get_current_revision must explicitly
    honor approved_revision_id rather than recomputing 'latest' — proven
    here by constructing a review with two revisions where the *first* one
    is pinned as approved."""
    db = _db()
    record, revision1 = _create(db)
    create_revision(
        db,
        review_id=record.id,
        expected_review_version=1,
        reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
        reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
        has_manual_overrides=False,
        manual_override_paths=[],
        edit_note=None,
    )
    # Manually pin revision1 as "approved" to prove the lookup uses the
    # pin, not "whatever is latest".
    decide_review(
        db,
        review_id=record.id,
        expected_review_version=2,
        new_status="APPROVED",
        decision_note=None,
        approved_revision_id=revision1.id,
    )
    reloaded = get_review_by_id(db, record.id)
    current = get_current_revision(db, reloaded)
    assert current.id == revision1.id


# --- get_latest_approved_review_for_job -------------------------------------


def test_get_latest_approved_review_for_job_ignores_pending_and_rejected():
    db = _db()
    pending, _ = _create(db)
    rejected, _ = _create(db)
    decide_review(
        db,
        review_id=rejected.id,
        expected_review_version=1,
        new_status="REJECTED",
        decision_note=None,
    )
    assert get_latest_approved_review_for_job(db, 1) is None


def test_get_latest_approved_review_for_job_returns_approved_one():
    db = _db()
    record, revision = _create(db)
    decide_review(
        db,
        review_id=record.id,
        expected_review_version=1,
        new_status="APPROVED",
        decision_note=None,
        approved_revision_id=revision.id,
    )
    found = get_latest_approved_review_for_job(db, 1)
    assert found is not None
    assert found.id == record.id


# --- privacy-safe persisted JSON --------------------------------------------


def test_reviewed_content_survives_json_round_trip():
    db = _db()
    record, revision = _create(db)
    response = to_review_package(record, revision)
    assert response.reviewed_cv.professional_title.value == "Junior Python Developer"
    assert response.reviewed_bewerbung.subject.value == "Bewerbung als Python Developer"
    assert response.manual_override_paths == []


# --- concurrency (real independent sessions, spec section 34/70) -----------


def test_concurrent_approve_and_reject_only_one_wins(tmp_path):
    """Two independent sessions both attempt a terminal decision on the
    same review at the same expected_review_version — exactly one must
    succeed, the other must get a CAS conflict, and the row must never end
    up in an ambiguous or corrupted state.
    """
    factory = _file_session_factory(tmp_path, "concurrent_review.db")
    db_setup = factory()
    record, revision = create_review(
        db_setup,
        job_id=1,
        cv_draft_id=1,
        bewerbung_draft_id=1,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        bewerbung_generator_version="v1",
        reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
        reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
    )
    review_id = record.id
    revision_id = revision.id
    db_setup.close()

    db_a = factory()
    db_b = factory()

    a_error = None
    b_error = None
    try:
        decide_review(
            db_a,
            review_id=review_id,
            expected_review_version=1,
            new_status="APPROVED",
            decision_note=None,
            approved_revision_id=revision_id,
        )
    except ReviewCASConflict as exc:
        a_error = exc

    try:
        decide_review(
            db_b,
            review_id=review_id,
            expected_review_version=1,
            new_status="REJECTED",
            decision_note=None,
        )
    except ReviewCASConflict as exc:
        b_error = exc

    db_a.close()
    db_b.close()

    # Exactly one of the two calls succeeded (no error), the other failed.
    assert (a_error is None) != (b_error is None)

    final = factory()
    reloaded = get_review_by_id(final, review_id)
    assert reloaded.status in ("APPROVED", "REJECTED")
    final.close()


def _setup_concurrent_review(tmp_path, name: str):
    factory = _file_session_factory(tmp_path, name)
    db_setup = factory()
    record, revision = create_review(
        db_setup,
        job_id=1,
        cv_draft_id=1,
        bewerbung_draft_id=1,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        bewerbung_generator_version="v1",
        reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
        reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
    )
    review_id, revision_id = record.id, revision.id
    db_setup.close()
    return factory, review_id, revision_id


def test_concurrent_approve_and_patch_only_one_wins_no_orphan_revision(tmp_path):
    """spec section 20/22: APPROVE and PATCH racing on the same
    expected_review_version — exactly one wins, and if APPROVE wins, the
    losing PATCH's revision must never have been persisted (no orphan
    row), because create_revision's CAS UPDATE is attempted before the
    revision INSERT, and a failed CAS rolls back before that INSERT ever
    runs.
    """
    factory, review_id, revision_id = _setup_concurrent_review(
        tmp_path, "concurrent_approve_patch.db"
    )
    db_a = factory()
    db_b = factory()

    approve_error = None
    patch_error = None
    try:
        decide_review(
            db_a,
            review_id=review_id,
            expected_review_version=1,
            new_status="APPROVED",
            decision_note=None,
            approved_revision_id=revision_id,
        )
    except ReviewCASConflict as exc:
        approve_error = exc

    try:
        create_revision(
            db_b,
            review_id=review_id,
            expected_review_version=1,
            reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
            reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
            has_manual_overrides=False,
            manual_override_paths=[],
            edit_note=None,
        )
    except ReviewCASConflict as exc:
        patch_error = exc

    db_a.close()
    db_b.close()

    assert (approve_error is None) != (patch_error is None)

    final = factory()
    total_revisions = final.scalar(
        select(func.count()).select_from(ApplicationPackageReviewRevisionRecord)
    )
    reloaded = get_review_by_id(final, review_id)
    if approve_error is None:
        assert reloaded.status == "APPROVED"
        assert reloaded.approved_revision_id == revision_id
        # The losing PATCH's revision must not exist — no orphan row.
        assert total_revisions == 1
    else:
        assert reloaded.status == "PENDING_REVIEW"
        assert total_revisions == 2
    final.close()


def test_concurrent_reject_and_patch_only_one_wins_no_orphan_revision(tmp_path):
    """spec section 21/22: same invariant as APPROVE/PATCH, for REJECT."""
    factory, review_id, _revision_id = _setup_concurrent_review(
        tmp_path, "concurrent_reject_patch.db"
    )
    db_a = factory()
    db_b = factory()

    reject_error = None
    patch_error = None
    try:
        decide_review(
            db_a,
            review_id=review_id,
            expected_review_version=1,
            new_status="REJECTED",
            decision_note=None,
        )
    except ReviewCASConflict as exc:
        reject_error = exc

    try:
        create_revision(
            db_b,
            review_id=review_id,
            expected_review_version=1,
            reviewed_cv=build_initial_reviewed_cv(_cv_draft()),
            reviewed_bewerbung=build_initial_reviewed_bewerbung(_bewerbung_draft()),
            has_manual_overrides=False,
            manual_override_paths=[],
            edit_note=None,
        )
    except ReviewCASConflict as exc:
        patch_error = exc

    db_a.close()
    db_b.close()

    assert (reject_error is None) != (patch_error is None)

    final = factory()
    total_revisions = final.scalar(
        select(func.count()).select_from(ApplicationPackageReviewRevisionRecord)
    )
    reloaded = get_review_by_id(final, review_id)
    if reject_error is None:
        assert reloaded.status == "REJECTED"
        assert total_revisions == 1
    else:
        assert reloaded.status == "PENDING_REVIEW"
        assert total_revisions == 2
    final.close()
