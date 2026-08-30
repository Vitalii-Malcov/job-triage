"""Candidate CV Draft persistence (Stage 6C).

Immutable snapshots, cache identity keyed by
`(match_id, cv_adapter_version)` (DB-enforced UNIQUE constraint — see
CandidateCVDraftRecord in app/db/models.py). A draft is pinned to one
specific persisted match; the match itself already pins job content and
candidate profile version (see app/db/candidate_job_match_repository.py),
so a change in either always produces a new match first, and therefore a
new draft identity here — no independent job/profile columns are needed in
this table's UNIQUE constraint (Stage 6C section 33).
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import CandidateCVDraftRecord
from app.models.cv_draft import TailoredCVDraft, TailoredCVDraftData

logger = logging.getLogger(__name__)


class CandidateCVDraftConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors CandidateJobMatchConsistencyError /
    CandidateProfileConsistencyError / CompanyResearchConsistencyError
    (same rationale): e.g. reloading the row a UNIQUE-constraint
    IntegrityError implies must exist comes back None. No code path in
    this project deletes CandidateCVDraftRecord rows, so this should be
    unreachable.
    """


def get_draft_by_id(db: Session, draft_id: int) -> CandidateCVDraftRecord | None:
    """Pure read of one immutable draft snapshot, by its own id — used by
    GET /api/v1/cv-drafts/{draft_id}. Never computes.
    """
    return db.get(CandidateCVDraftRecord, draft_id)


def get_latest_draft(db: Session, job_id: int) -> CandidateCVDraftRecord | None:
    """Pure cache read for GET /jobs/{id}/cv-draft: the most recently
    created draft for this job, regardless of whether it is still fresh
    relative to the current candidate profile version, job content, or
    the match it was generated from. Never computes.
    """
    stmt = (
        select(CandidateCVDraftRecord)
        .where(CandidateCVDraftRecord.job_id == job_id)
        .order_by(CandidateCVDraftRecord.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def get_cached_draft(
    db: Session, *, match_id: int, cv_adapter_version: str
) -> CandidateCVDraftRecord | None:
    """Exact cache-identity lookup used by POST before recomputing —
    reused only when both the pinned match and the adapter version are
    unchanged from what last produced a draft.
    """
    stmt = select(CandidateCVDraftRecord).where(
        CandidateCVDraftRecord.match_id == match_id,
        CandidateCVDraftRecord.cv_adapter_version == cv_adapter_version,
    )
    return db.scalar(stmt)


def create_draft(
    db: Session, job_id: int, job_snapshot_fingerprint: str, data: TailoredCVDraftData
) -> tuple[CandidateCVDraftRecord, bool]:
    """Persist a freshly computed CV draft.

    `job_snapshot_fingerprint` is the value the caller already verified
    against the pinned match before calling `compute_cv_draft` (section
    8) — threaded through explicitly here (not re-derived) so this
    function never needs its own JobRecord/fingerprint dependency;
    `TailoredCVDraftData` itself does not carry this field (it is
    consistency-guard metadata, not part of the rendered draft content —
    same "concurrency/consistency metadata, not a fact" precedent as
    Stage 6A's `expected_profile_version`).

    Two concurrent POSTs for the identical cache identity (match_id,
    cv_adapter_version) must not create duplicate rows — enforced by a DB
    UNIQUE constraint (uq_candidate_cv_drafts_cache_identity), not a
    SELECT-then-INSERT in application code alone. If this INSERT loses
    that race, the IntegrityError is caught and the concurrent winner's
    row is reloaded and returned instead — mirrors
    app.db.candidate_job_match_repository.create_match's established
    pattern for the exact same class of race.

    Returns (record, created); created=False means a concurrent request's
    row was reloaded instead of this call's own data being persisted.
    """
    record = CandidateCVDraftRecord(
        job_id=job_id,
        match_id=data.match_id,
        candidate_profile_version=data.candidate_profile_version,
        job_snapshot_fingerprint=job_snapshot_fingerprint,
        match_algorithm_version=data.match_algorithm_version,
        cv_adapter_version=data.cv_adapter_version,
        status=data.status,
        draft_json=data.model_dump_json(),
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_cached_draft(
            db, match_id=data.match_id, cv_adapter_version=data.cv_adapter_version
        )
        if existing is None:
            raise CandidateCVDraftConsistencyError(
                "Expected a candidate_cv_drafts row to exist after a UNIQUE constraint "
                "collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def to_tailored_cv_draft(record: CandidateCVDraftRecord) -> TailoredCVDraft:
    """Convert a persisted row into its typed API response shape. Never
    logs or otherwise exposes the record outside this explicit, structured
    conversion (privacy-safe logging — see app/api/routes.py, which logs
    only job_id/match_id/draft_id/profile_version/adapter_version/status,
    never candidate content).
    """
    payload = json.loads(record.draft_json)
    return TailoredCVDraft(**payload, id=record.id, created_at=record.created_at)
