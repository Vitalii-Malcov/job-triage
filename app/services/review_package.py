"""Application Package Review orchestration (Stage 6E): source-pair
verification, freshness checks (at creation AND at approval), patch
application, and atomic status transitions.

**Deterministic, DB-backed, no LLM, no provider call, no network (spec
section 45/46).** Unlike `app.services.bewerbung.BewerbungService`, this
service is fully synchronous — every operation here is either a pure
computation (`app.agents.review_package_builder`) or a plain DB
read/write. Human edits come from the API request body only; nothing here
ever calls a `BewerbungProvider` or regenerates content.

**No external action of any kind (spec section 3/51).** Approval means
only "the human approved this exact package" — it never sends email,
opens XING/LinkedIn, contacts a recruiter, or mutates `ApplicationStatus`.
A future submission stage must consume `GET /jobs/{id}/approved-package`
explicitly; nothing in this module is that consumer.
"""

import logging

from sqlalchemy.orm import Session

from app.agents.review_package_builder import (
    ReviewBewerbungDraftJobMismatchError,
    ReviewBewerbungDraftNotFoundError,
    ReviewCurrentJobMissingError,
    ReviewCurrentProfileMissingError,
    ReviewCVDraftJobMismatchError,
    ReviewCVDraftNotFoundError,
    ReviewJobChangedError,
    ReviewManualOverrideAcknowledgmentRequiredError,
    ReviewNotFoundError,
    ReviewNotPendingError,
    ReviewProfileChangedError,
    ReviewRepositoryConsistencyError,
    ReviewVersionConflictError,
    apply_bewerbung_patch,
    apply_cv_patch,
    build_initial_reviewed_bewerbung,
    build_initial_reviewed_cv,
    collect_manual_override_paths,
    compute_has_manual_overrides,
    verify_source_pair,
)
from app.db.bewerbung_repository import get_bewerbung_draft_by_id, to_bewerbung_draft
from app.db.candidate_cv_draft_repository import get_draft_by_id, to_tailored_cv_draft
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint
from app.db.candidate_profile_repository import get_candidate_profile
from app.db.models import JobRecord
from app.db.review_package_repository import (
    ReviewCASConflict,
    create_review,
    create_revision,
    decide_review,
    get_current_revision,
    get_latest_approved_review_for_job,
    get_review_by_id,
    get_revision_by_id,
    to_review_package,
)
from app.models.review_package import (
    BewerbungContentPatch,
    CVContentPatch,
    ReviewedBewerbungContent,
    ReviewedCVContent,
    ReviewPackage,
)

logger = logging.getLogger(__name__)


def _resolve_cas_conflict(db: Session, review_id: int, expected_review_version: int) -> None:
    """Turn a `ReviewCASConflict` into the specific typed error a caller
    should see, by inspecting the row's current state after the failed
    CAS attempt — never guesses; always re-reads.
    """
    current = get_review_by_id(db, review_id)
    if current is None:
        raise ReviewNotFoundError(review_id)
    if current.status != "PENDING_REVIEW":
        raise ReviewNotPendingError(review_id, current.status)
    raise ReviewVersionConflictError(expected_review_version, current.review_version)


class ReviewPackageService:
    def create(
        self, db: Session, job: JobRecord, cv_draft_id: int, bewerbung_draft_id: int
    ) -> ReviewPackage:
        cv_record = get_draft_by_id(db, cv_draft_id)
        if cv_record is None:
            raise ReviewCVDraftNotFoundError(cv_draft_id)
        if cv_record.job_id != job.id:
            raise ReviewCVDraftJobMismatchError(cv_draft_id, job.id)

        bewerbung_record = get_bewerbung_draft_by_id(db, bewerbung_draft_id)
        if bewerbung_record is None:
            raise ReviewBewerbungDraftNotFoundError(bewerbung_draft_id)
        if bewerbung_record.job_id != job.id:
            raise ReviewBewerbungDraftJobMismatchError(bewerbung_draft_id, job.id)

        verify_source_pair(cv_record, bewerbung_record)

        # Fail-closed authority checks (blocker fix): a missing profile
        # must never be silently (re)created merely to pass this version
        # comparison — see get_candidate_profile's docstring. `job` here
        # was already loaded by the caller via a real lookup (None ->
        # 404 before this method is ever called), so no separate
        # missing-job check is needed for creation.
        profile_record = get_candidate_profile(db)
        if profile_record is None:
            raise ReviewCurrentProfileMissingError()
        if profile_record.profile_version != cv_record.candidate_profile_version:
            raise ReviewProfileChangedError(
                cv_record.candidate_profile_version, profile_record.profile_version
            )
        current_fingerprint = compute_job_snapshot_fingerprint(job)
        if current_fingerprint != cv_record.job_snapshot_fingerprint:
            raise ReviewJobChangedError()

        cv_draft = to_tailored_cv_draft(cv_record)
        bewerbung_draft = to_bewerbung_draft(bewerbung_record)
        reviewed_cv = build_initial_reviewed_cv(cv_draft)
        reviewed_bewerbung = build_initial_reviewed_bewerbung(bewerbung_draft)

        record, revision = create_review(
            db,
            job_id=job.id,
            cv_draft_id=cv_record.id,
            bewerbung_draft_id=bewerbung_record.id,
            match_id=cv_record.match_id,
            candidate_profile_version=cv_record.candidate_profile_version,
            job_snapshot_fingerprint=cv_record.job_snapshot_fingerprint,
            match_algorithm_version=cv_record.match_algorithm_version,
            cv_adapter_version=cv_record.cv_adapter_version,
            bewerbung_generator_version=bewerbung_record.bewerbung_generator_version,
            reviewed_cv=reviewed_cv,
            reviewed_bewerbung=reviewed_bewerbung,
        )
        logger.info(
            "review_package_created review_id=%s job_id=%s cv_draft_id=%s "
            "bewerbung_draft_id=%s review_version=%s status=%s",
            record.id,
            job.id,
            cv_draft_id,
            bewerbung_draft_id,
            record.review_version,
            record.status,
        )
        return to_review_package(record, revision)

    def patch(
        self,
        db: Session,
        review_id: int,
        expected_review_version: int,
        cv_changes: CVContentPatch | None,
        bewerbung_changes: BewerbungContentPatch | None,
        edit_note: str | None,
    ) -> ReviewPackage:
        record = get_review_by_id(db, review_id)
        if record is None:
            raise ReviewNotFoundError(review_id)
        if record.status != "PENDING_REVIEW":
            raise ReviewNotPendingError(review_id, record.status)

        latest_revision = get_current_revision(db, record)
        if latest_revision is None:
            raise ReviewRepositoryConsistencyError(
                f"Review {review_id} has no revisions — should be unreachable."
            )

        current_cv = ReviewedCVContent.model_validate_json(latest_revision.reviewed_cv_json)
        current_bewerbung = ReviewedBewerbungContent.model_validate_json(
            latest_revision.reviewed_bewerbung_json
        )

        new_cv = current_cv
        if cv_changes is not None:
            new_cv, _ = apply_cv_patch(current_cv, cv_changes)

        new_bewerbung = current_bewerbung
        if bewerbung_changes is not None:
            new_bewerbung, _ = apply_bewerbung_patch(current_bewerbung, bewerbung_changes)

        has_overrides = compute_has_manual_overrides(new_cv, new_bewerbung)
        override_paths = collect_manual_override_paths(new_cv, new_bewerbung)

        try:
            updated_record, revision = create_revision(
                db,
                review_id=review_id,
                expected_review_version=expected_review_version,
                reviewed_cv=new_cv,
                reviewed_bewerbung=new_bewerbung,
                has_manual_overrides=has_overrides,
                manual_override_paths=override_paths,
                edit_note=edit_note,
            )
        except ReviewCASConflict:
            _resolve_cas_conflict(db, review_id, expected_review_version)
            raise  # unreachable — _resolve_cas_conflict always raises

        logger.info(
            "review_package_revised review_id=%s review_version=%s has_manual_overrides=%s",
            review_id,
            updated_record.review_version,
            updated_record.has_manual_overrides,
        )
        return to_review_package(updated_record, revision)

    def approve(
        self,
        db: Session,
        review_id: int,
        expected_review_version: int,
        acknowledge_manual_overrides: bool,
        decision_note: str | None,
    ) -> ReviewPackage:
        record = get_review_by_id(db, review_id)
        if record is None:
            raise ReviewNotFoundError(review_id)
        if record.status != "PENDING_REVIEW":
            raise ReviewNotPendingError(review_id, record.status)
        # Fail fast on an already-known-stale version before doing any
        # freshness lookups — the atomic CAS in decide_review() remains
        # the authoritative concurrency guard below regardless (a version
        # bump between this read and the CAS is still caught there).
        if record.review_version != expected_review_version:
            raise ReviewVersionConflictError(expected_review_version, record.review_version)

        # Freshness must be rechecked here, not just at creation (spec
        # section 7) — a review may sit open while the profile/job move
        # on. Fail-closed authority checks (blocker fix): a missing
        # profile/job must NEVER be treated as "skip the check" or "safe
        # to recreate and compare" — see get_candidate_profile's
        # docstring and ReviewCurrentProfileMissingError/
        # ReviewCurrentJobMissingError. No terminal decision (the CAS
        # below) may occur before both authorities are confirmed present
        # and fresh.
        profile_record = get_candidate_profile(db)
        if profile_record is None:
            raise ReviewCurrentProfileMissingError()
        if profile_record.profile_version != record.candidate_profile_version:
            raise ReviewProfileChangedError(
                record.candidate_profile_version, profile_record.profile_version
            )

        job = db.get(JobRecord, record.job_id)
        if job is None:
            raise ReviewCurrentJobMissingError()
        current_fingerprint = compute_job_snapshot_fingerprint(job)
        if current_fingerprint != record.job_snapshot_fingerprint:
            raise ReviewJobChangedError()

        if record.has_manual_overrides and not acknowledge_manual_overrides:
            raise ReviewManualOverrideAcknowledgmentRequiredError(review_id)

        latest_revision = get_current_revision(db, record)
        if latest_revision is None:
            raise ReviewRepositoryConsistencyError(
                f"Review {review_id} has no revisions — should be unreachable."
            )

        try:
            updated_record = decide_review(
                db,
                review_id=review_id,
                expected_review_version=expected_review_version,
                new_status="APPROVED",
                decision_note=decision_note,
                approved_revision_id=latest_revision.id,
            )
        except ReviewCASConflict:
            _resolve_cas_conflict(db, review_id, expected_review_version)
            raise  # unreachable

        logger.info(
            "review_package_approved review_id=%s job_id=%s review_version=%s "
            "has_manual_overrides=%s",
            review_id,
            record.job_id,
            updated_record.review_version,
            updated_record.has_manual_overrides,
        )
        return to_review_package(updated_record, latest_revision)

    def reject(
        self,
        db: Session,
        review_id: int,
        expected_review_version: int,
        decision_note: str | None,
    ) -> ReviewPackage:
        record = get_review_by_id(db, review_id)
        if record is None:
            raise ReviewNotFoundError(review_id)
        if record.status != "PENDING_REVIEW":
            raise ReviewNotPendingError(review_id, record.status)

        latest_revision = get_current_revision(db, record)
        if latest_revision is None:
            raise ReviewRepositoryConsistencyError(
                f"Review {review_id} has no revisions — should be unreachable."
            )

        try:
            updated_record = decide_review(
                db,
                review_id=review_id,
                expected_review_version=expected_review_version,
                new_status="REJECTED",
                decision_note=decision_note,
            )
        except ReviewCASConflict:
            _resolve_cas_conflict(db, review_id, expected_review_version)
            raise  # unreachable

        logger.info(
            "review_package_rejected review_id=%s job_id=%s review_version=%s",
            review_id,
            record.job_id,
            updated_record.review_version,
        )
        return to_review_package(updated_record, latest_revision)


def get_approved_package(db: Session, job: JobRecord) -> ReviewPackage | None:
    """Pure read for GET /jobs/{id}/approved-package — never generates,
    never approves. Returns None if no review for this job has ever been
    approved (caller maps to 404).
    """
    record = get_latest_approved_review_for_job(db, job.id)
    if record is None:
        return None
    revision = get_revision_by_id(db, record.approved_revision_id)
    if revision is None:
        raise ReviewRepositoryConsistencyError(
            f"Approved review {record.id} has no resolvable approved_revision_id — "
            "should be unreachable."
        )
    return to_review_package(record, revision)
