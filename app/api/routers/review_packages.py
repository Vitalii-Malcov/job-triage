"""HTTP routes: review packages (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.agents.review_package_builder import (
    ReviewApplicationApprovedError,
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
    ReviewParagraphIndexError,
    ReviewProfileChangedError,
    ReviewSourceMismatchError,
    ReviewVersionConflictError,
)
from app.api.routers.common import _already_applied_detail
from app.db.repositories import (
    get_job_by_id,
)
from app.db.review_package_repository import (
    get_current_revision,
    get_latest_review_for_job,
    get_review_by_id,
    to_review_package,
)
from app.db.session import get_db
from app.models.review_package import (
    ReviewPackage,
    ReviewPackageApproveRequest,
    ReviewPackageCreateRequest,
    ReviewPackagePatchRequest,
    ReviewPackageRejectRequest,
)
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_rate_limit,
    enforce_review_write_rate_limit,
)
from app.services.application_guard import AlreadyAppliedError
from app.services.review_package import ReviewPackageService, get_approved_package

logger = logging.getLogger("app.api.routes")
router = APIRouter()


def _run_create_review_package(
    db: Session, job_id: int, cv_draft_id: int, bewerbung_draft_id: int
) -> ReviewPackage | None:
    """Create a Stage 6E review package pinned to one specific persisted
    CV draft and one specific persisted Bewerbung draft. Returns None if
    the job doesn't exist — callers translate that into their own 404.
    Deterministic, synchronous, zero I/O beyond the database (no provider
    call, no LLM — spec section 45).
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None
    return ReviewPackageService().create(db, job, cv_draft_id, bewerbung_draft_id)


@router.post(
    "/jobs/{job_id}/review-package",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def create_review_package(
    job_id: int,
    body: ReviewPackageCreateRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """Create a new PENDING_REVIEW package pinned to `body.cv_draft_id` +
    `body.bewerbung_draft_id` — both required, no "latest" fallback
    (section 4). Never auto-approves.
    """
    try:
        result = _run_create_review_package(db, job_id, body.cv_draft_id, body.bewerbung_draft_id)
    except AlreadyAppliedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=_already_applied_detail(exc)
        ) from exc
    except ReviewCVDraftNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewBewerbungDraftNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewCVDraftJobMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewBewerbungDraftJobMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewSourceMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(exc), "mismatched_fields": exc.mismatched_fields},
        ) from exc
    except ReviewCurrentProfileMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewProfileChangedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "pinned_profile_version": exc.pinned_profile_version,
                "current_profile_version": exc.current_profile_version,
            },
        ) from exc
    except ReviewCurrentJobMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewJobChangedError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/review-package",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_review_package_for_job(job_id: int, db: Session = Depends(get_db)) -> ReviewPackage:
    """Pure read — never creates. Returns the most recently created
    review package for this job, whatever its status.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_review_for_job(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=(
                "No review package found for this job. "
                "POST /jobs/{id}/review-package to create one."
            ),
        )
    revision = get_current_revision(db, record)
    return to_review_package(record, revision)


@router.get(
    "/review-packages/{review_id}",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_review_package_by_id(review_id: int, db: Session = Depends(get_db)) -> ReviewPackage:
    """Pure read by exact id — never creates, never mutates."""
    record = get_review_by_id(db, review_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Review package not found"
        )
    revision = get_current_revision(db, record)
    return to_review_package(record, revision)


@router.patch(
    "/review-packages/{review_id}",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def patch_review_package(
    review_id: int,
    body: ReviewPackagePatchRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """Apply a human edit to the CV/Bewerbung review surface, creating a
    new immutable revision (section 21/27) and bumping `review_version`
    (section 11) — only while the review is still PENDING_REVIEW (section
    28). Never edits the pinned source `candidate_cv_drafts`/
    `bewerbung_drafts` rows.
    """
    try:
        result = ReviewPackageService().patch(
            db,
            review_id,
            body.expected_review_version,
            body.cv_changes,
            body.bewerbung_changes,
            body.edit_note,
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewNotPendingError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "current_status": exc.current_status},
        ) from exc
    except ReviewParagraphIndexError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewVersionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "expected_review_version": exc.expected_review_version,
                "current_review_version": exc.current_review_version,
            },
        ) from exc
    return result


@router.post(
    "/review-packages/{review_id}/approve",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def approve_review_package(
    review_id: int,
    body: ReviewPackageApproveRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """The only endpoint in this project that may transition a review
    package to APPROVED (section 2). Never sends anything, never mutates
    ApplicationStatus (section 3) — approval means only "the human
    approved this exact package".
    """
    try:
        result = ReviewPackageService().approve(
            db,
            review_id,
            body.expected_review_version,
            body.acknowledge_manual_overrides,
            body.decision_note,
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewNotPendingError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "current_status": exc.current_status},
        ) from exc
    except ReviewCurrentProfileMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewProfileChangedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "pinned_profile_version": exc.pinned_profile_version,
                "current_profile_version": exc.current_profile_version,
            },
        ) from exc
    except ReviewCurrentJobMissingError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewJobChangedError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReviewManualOverrideAcknowledgmentRequiredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReviewVersionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "expected_review_version": exc.expected_review_version,
                "current_review_version": exc.current_review_version,
            },
        ) from exc
    return result


@router.post(
    "/review-packages/{review_id}/reject",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_review_write_rate_limit)],
)
def reject_review_package(
    review_id: int,
    body: ReviewPackageRejectRequest,
    db: Session = Depends(get_db),
) -> ReviewPackage:
    """PENDING_REVIEW -> REJECTED. Never mutates the source drafts, never
    touches ApplicationStatus. Refused (409) while a linked application is
    APPROVED (Stage 16)."""
    try:
        result = ReviewPackageService().reject(
            db, review_id, body.expected_review_version, body.decision_note
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewApplicationApprovedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "application_id": exc.application_id},
        ) from exc
    except ReviewNotPendingError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "current_status": exc.current_status},
        ) from exc
    except ReviewVersionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "expected_review_version": exc.expected_review_version,
                "current_review_version": exc.current_review_version,
            },
        ) from exc
    return result


@router.get(
    "/jobs/{job_id}/approved-package",
    response_model=ReviewPackage,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_approved_package_for_job(job_id: int, db: Session = Depends(get_db)) -> ReviewPackage:
    """Pure read — the future submission-stage handoff boundary (section
    31/44). Returns only an actually APPROVED review package/revision;
    never auto-approves, never falls back to PENDING_REVIEW or a raw
    latest CV/Bewerbung draft.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = get_approved_package(db, job)
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No approved review package found for this job.",
        )
    return result
