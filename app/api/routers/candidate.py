"""HTTP routes: candidate (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.agents.bewerbung_generator import (
    BewerbungCVDraftJobMismatchError,
    BewerbungCVDraftNotFoundError,
    BewerbungJobChangedError,
    BewerbungMatchInconsistentError,
    BewerbungMatchNotFoundError,
    BewerbungProfileChangedError,
)
from app.agents.bewerbung_renderer import BewerbungPlanRejectedError
from app.agents.cv_adapter import (
    CVDraftJobChangedError,
    CVDraftMatchJobMismatchError,
    CVDraftMatchNotFoundError,
    CVDraftProfileChangedError,
)
from app.api.http_errors import http_errors
from app.api.routers.common import _already_applied_detail
from app.db.bewerbung_repository import (
    get_bewerbung_draft_by_id,
    get_latest_bewerbung_draft,
    to_bewerbung_draft,
)
from app.db.candidate_cv_draft_repository import (
    get_draft_by_id,
    get_latest_draft,
    to_tailored_cv_draft,
)
from app.db.candidate_job_match_repository import get_latest_match, to_candidate_job_match
from app.db.candidate_profile_repository import (
    CandidateProfileTrustDowngradeError,
    CandidateProfileVersionConflictError,
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
    to_candidate_profile_response,
)
from app.db.repositories import (
    get_job_by_id,
)
from app.db.session import get_db
from app.models.bewerbung import BewerbungDraft, BewerbungDraftRequest
from app.models.candidate_job_match import CandidateJobMatch, MatchRequest
from app.models.candidate_profile import CandidateProfile, CandidateProfilePatchRequest
from app.models.cv_draft import CVDraftRequest, TailoredCVDraft
from app.providers.bewerbung.base import BewerbungProviderError, BewerbungProviderNotConfiguredError
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_bewerbung_rate_limit,
    enforce_cv_draft_rate_limit,
    enforce_match_rate_limit,
    enforce_rate_limit,
)
from app.services.application_guard import AlreadyAppliedError
from app.services.candidate_preparation import (
    prepare_bewerbung_draft as _run_bewerbung_draft,
)
from app.services.candidate_preparation import (
    prepare_candidate_cv_draft as _run_candidate_cv_draft,
)
from app.services.candidate_preparation import (
    prepare_candidate_job_match as _run_candidate_job_match,
)
from app.services.offload import run_off_loop

logger = logging.getLogger("app.api.routes")
router = APIRouter()


@router.get(
    "/candidate-profile",
    response_model=CandidateProfile,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_profile(db: Session = Depends(get_db)) -> CandidateProfile:
    """Stage 6A: the single canonical Candidate Profile — the factual
    authority future CV/Bewerbung generation must read candidate-side
    claims from (see app/db/candidate_profile_repository.py's module
    docstring). Always 200: the singleton is created empty on first access
    rather than 404ing before any PATCH has ever been sent.
    """
    record = get_or_create_candidate_profile(db)
    return to_candidate_profile_response(record)


@router.patch(
    "/candidate-profile",
    response_model=CandidateProfile,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def patch_candidate_profile(
    body: CandidateProfilePatchRequest, db: Session = Depends(get_db)
) -> CandidateProfile:
    """Partial update — see CandidateProfilePatchRequest's docstring for
    the exact semantics (omitted keys untouched; a present list field
    replaces that list wholesale). No PUT endpoint is exposed; see the
    same docstring for why.

    `body.expected_profile_version` is required (structurally enforced —
    422 if omitted) and must match the profile's current version
    — a stale value raises CandidateProfileVersionConflictError, mapped to
    409 here. The caller must GET the profile again and retry with the
    fresh version; the response detail never includes profile content.
    """
    with http_errors(
        (
            (
                CandidateProfileVersionConflictError,
                http_status.HTTP_409_CONFLICT,
                lambda exc: {"message": str(exc), "current_profile_version": exc.current_version},
            ),
            (
                CandidateProfileTrustDowngradeError,
                http_status.HTTP_409_CONFLICT,
                lambda exc: {"message": str(exc), "fields": exc.fields},
            ),
        )
    ):
        record = apply_candidate_profile_patch(db, body)
    return to_candidate_profile_response(record)


@router.post(
    "/jobs/{job_id}/match",
    response_model=CandidateJobMatch,
    dependencies=[Depends(require_api_key), Depends(enforce_match_rate_limit)],
)
def run_candidate_job_match(
    job_id: int,
    body: MatchRequest = MatchRequest(),
    db: Session = Depends(get_db),
) -> CandidateJobMatch:
    """Compute or reuse a deterministic Candidate Profile <-> Job match
    analysis. Always 200 for an existing job, even with a sparse/empty
    Candidate Profile (section 33) — a profile with no confirmed facts
    yields low/neutral sub-scores plus an explicit warning, never a
    failure merely because the profile is sparse.
    """
    result = _run_candidate_job_match(db, job_id, force_recompute=body.force_recompute)
    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/match",
    response_model=CandidateJobMatch,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_job_match(job_id: int, db: Session = Depends(get_db)) -> CandidateJobMatch:
    """Pure cache read — never computes (section 23). Returns the most
    recently computed analysis for this job, whatever candidate profile
    version and job content it was computed against; the response's own
    `candidate_profile_version` tells the caller whether it may be stale
    relative to the current profile.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_match(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No match analysis found for this job. POST /jobs/{id}/match to compute it.",
        )
    return to_candidate_job_match(record)


@router.post(
    "/jobs/{job_id}/cv-draft",
    response_model=TailoredCVDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_cv_draft_rate_limit)],
)
def run_candidate_cv_draft(
    job_id: int,
    body: CVDraftRequest,
    db: Session = Depends(get_db),
) -> TailoredCVDraft:
    """Compute or reuse a deterministic Tailored CV Draft pinned to
    `body.match_id`. `match_id` is required (no "latest match" fallback —
    section 5). Always 200 for a valid, still-fresh match, even with a
    sparse/empty Candidate Profile (section 37) — a profile with few
    trusted facts yields a sparse draft plus explicit warnings, never a
    failure merely because the profile is sparse.
    """
    with http_errors(
        (
            (CVDraftMatchNotFoundError, http_status.HTTP_404_NOT_FOUND, str),
            (CVDraftMatchJobMismatchError, http_status.HTTP_422_UNPROCESSABLE_CONTENT, str),
            (
                CVDraftProfileChangedError,
                http_status.HTTP_409_CONFLICT,
                lambda exc: {
                    "message": str(exc),
                    "match_profile_version": exc.match_profile_version,
                    "current_profile_version": exc.current_profile_version,
                },
            ),
            (CVDraftJobChangedError, http_status.HTTP_409_CONFLICT, str),
        )
    ):
        result = _run_candidate_cv_draft(
            db, job_id, body.match_id, force_recompute=body.force_recompute
        )

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/cv-draft",
    response_model=TailoredCVDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_cv_draft_for_job(job_id: int, db: Session = Depends(get_db)) -> TailoredCVDraft:
    """Pure cache read — never computes (section 35). Returns the most
    recently created draft for this job, whatever match/profile
    version/job content it was generated against.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_draft(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No CV draft found for this job. POST /jobs/{id}/cv-draft to create one.",
        )
    return to_tailored_cv_draft(record)


@router.get(
    "/cv-drafts/{draft_id}",
    response_model=TailoredCVDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_candidate_cv_draft_by_id(draft_id: int, db: Session = Depends(get_db)) -> TailoredCVDraft:
    """Returns the exact immutable draft snapshot for `draft_id` (section
    35) — never recomputed, never mutated.
    """
    record = get_draft_by_id(db, draft_id)
    if record is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="CV draft not found")
    return to_tailored_cv_draft(record)


@router.post(
    "/jobs/{job_id}/bewerbung-draft",
    response_model=BewerbungDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_bewerbung_rate_limit)],
)
async def run_bewerbung_draft(
    job_id: int,
    body: BewerbungDraftRequest,
    db: Session = Depends(get_db),
) -> BewerbungDraft:
    """Generate a new Bewerbung draft pinned to `body.cv_draft_id`.
    `cv_draft_id` is required (no "latest CV draft" fallback — section 3).
    Every successful call creates a NEW immutable draft row (section 35) —
    never reused/cached, unlike POST .../match or .../cv-draft.
    """
    with http_errors(
        (
            (
                AlreadyAppliedError,
                http_status.HTTP_409_CONFLICT,
                lambda exc: _already_applied_detail(exc),
            ),
            (BewerbungCVDraftNotFoundError, http_status.HTTP_404_NOT_FOUND, str),
            (BewerbungCVDraftJobMismatchError, http_status.HTTP_422_UNPROCESSABLE_CONTENT, str),
            (
                BewerbungProfileChangedError,
                http_status.HTTP_409_CONFLICT,
                lambda exc: {
                    "message": str(exc),
                    "cv_draft_profile_version": exc.cv_draft_profile_version,
                    "current_profile_version": exc.current_profile_version,
                },
            ),
            (BewerbungJobChangedError, http_status.HTTP_409_CONFLICT, str),
            (BewerbungMatchNotFoundError, http_status.HTTP_500_INTERNAL_SERVER_ERROR, str),
            (BewerbungMatchInconsistentError, http_status.HTTP_500_INTERNAL_SERVER_ERROR, str),
            (BewerbungProviderNotConfiguredError, http_status.HTTP_503_SERVICE_UNAVAILABLE, str),
            (BewerbungProviderError, http_status.HTTP_502_BAD_GATEWAY, str),
            (
                BewerbungPlanRejectedError,
                http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                lambda exc: {"message": str(exc), "codes": exc.codes},
            ),
        )
    ):
        result = await run_off_loop(lambda: _run_bewerbung_draft(db, job_id, body.cv_draft_id))

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")
    return result


@router.get(
    "/jobs/{job_id}/bewerbung-draft",
    response_model=BewerbungDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_bewerbung_draft_for_job(job_id: int, db: Session = Depends(get_db)) -> BewerbungDraft:
    """Pure cache read — never generates. Returns the most recently
    created Bewerbung draft for this job, whatever CV draft/match/profile
    version/job content it was generated against.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    record = get_latest_bewerbung_draft(db, job_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=(
                "No Bewerbung draft found for this job. "
                "POST /jobs/{id}/bewerbung-draft to create one."
            ),
        )
    return to_bewerbung_draft(record)


@router.get(
    "/bewerbung-drafts/{draft_id}",
    response_model=BewerbungDraft,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_bewerbung_draft_snapshot(draft_id: int, db: Session = Depends(get_db)) -> BewerbungDraft:
    """Returns the exact immutable draft snapshot for `draft_id` — never
    regenerated, never mutated.
    """
    record = get_bewerbung_draft_by_id(db, draft_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Bewerbung draft not found"
        )
    return to_bewerbung_draft(record)
