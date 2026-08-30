"""Candidate Job Match persistence (Stage 6B).

Cache identity, not a plain audit log: a row is uniquely identified by
`(job_id, candidate_profile_version, job_snapshot_fingerprint,
algorithm_version)` (DB-enforced UNIQUE constraint — see
CandidateJobMatchRecord in app/db/models.py) and is reused rather than
recomputed whenever those four inputs are unchanged (Stage 6B section 21).
Any change to the candidate profile version, the job's matching-relevant
content, or the algorithm itself produces a new row instead of overwriting
the old one — a previous analysis must keep showing which profile version
and job content it was computed against (section 17/18), never be silently
reinterpreted.
"""

import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import CandidateJobMatchRecord, JobRecord
from app.models.candidate_job_match import CandidateJobMatch, CandidateJobMatchData

logger = logging.getLogger(__name__)


class CandidateJobMatchConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors CandidateProfileConsistencyError /
    CompanyResearchConsistencyError (same rationale): e.g. reloading the
    row a UNIQUE-constraint IntegrityError implies must exist comes back
    None. No code path in this project deletes CandidateJobMatchRecord
    rows, so this should be unreachable.
    """


def compute_job_snapshot_fingerprint(job: JobRecord) -> str:
    """Content fingerprint of the job fields that feed matching (title,
    description, must/nice skill lists) — NOT JobRecord.fingerprint (that
    is the dedup *identity* key computed from source+company+title+url; a
    different concept, see app/db/repositories.py's `_fingerprint`). Used
    purely as a cache-invalidation key: if a re-collection run changes any
    of these fields, the fingerprint changes and the next match request
    computes a fresh analysis instead of reusing one computed against the
    old content.

    Uses a plain, order-preserving join rather than hashlib.sha256 over
    JSON — the four inputs already have a stable, unambiguous textual form
    (title/description are plain strings; the *_skills_json columns are
    themselves already-canonical JSON text), so no additional structure is
    needed. Casefolding title/description means a change in case alone
    does not invalidate the cache (matches this project's existing
    text-identity convention, e.g. app.db.repositories.normalize_company_name).
    """
    canonical = "|".join(
        [
            job.title.strip().casefold(),
            job.description.strip().casefold(),
            job.must_have_skills_json,
            job.nice_to_have_skills_json,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_latest_match(db: Session, job_id: int) -> CandidateJobMatchRecord | None:
    """Pure cache read for GET /jobs/{id}/match: the most recently computed
    analysis for this job, regardless of whether it is still fresh
    relative to the current candidate profile version or job content.
    Never computes — see app/api/routes.py's get_candidate_job_match.
    """
    stmt = (
        select(CandidateJobMatchRecord)
        .where(CandidateJobMatchRecord.job_id == job_id)
        .order_by(CandidateJobMatchRecord.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def get_cached_match(
    db: Session,
    *,
    job_id: int,
    candidate_profile_version: int,
    job_snapshot_fingerprint: str,
    algorithm_version: str,
) -> CandidateJobMatchRecord | None:
    """Exact cache-identity lookup used by POST before recomputing (section
    21) — reused only when job content, candidate profile version, and
    algorithm version all still match what was last computed.
    """
    stmt = select(CandidateJobMatchRecord).where(
        CandidateJobMatchRecord.job_id == job_id,
        CandidateJobMatchRecord.candidate_profile_version == candidate_profile_version,
        CandidateJobMatchRecord.job_snapshot_fingerprint == job_snapshot_fingerprint,
        CandidateJobMatchRecord.algorithm_version == algorithm_version,
    )
    return db.scalar(stmt)


def create_match(
    db: Session, data: CandidateJobMatchData, job_snapshot_fingerprint: str
) -> tuple[CandidateJobMatchRecord, bool]:
    """Persist a freshly computed match analysis.

    Section 34: two concurrent POSTs for the identical cache identity
    (job_id, candidate_profile_version, job_snapshot_fingerprint,
    algorithm_version) must not create duplicate rows. This is enforced by
    a DB UNIQUE constraint on exactly those four columns
    (uq_candidate_job_matches_cache_identity), not by a
    SELECT-then-INSERT in application code alone — a plain
    SELECT-then-INSERT has a race window between the two statements. If
    this INSERT loses that race, the IntegrityError is caught and the
    concurrent winner's row is reloaded and returned instead — mirrors
    app.db.candidate_profile_repository.get_or_create_candidate_profile's
    established pattern for the exact same class of race.

    Returns (record, created); created=False means a concurrent request's
    row was reloaded instead of this call's own data being persisted (the
    caller's own computed `data` is discarded in that case — the reloaded
    row is the canonical result, since it is what is now actually in the
    database and what a subsequent GET will return).
    """
    record = CandidateJobMatchRecord(
        job_id=data.job_id,
        candidate_profile_version=data.candidate_profile_version,
        job_snapshot_fingerprint=job_snapshot_fingerprint,
        algorithm_version=data.algorithm_version,
        company_research_id=data.company_research_id,
        overall_score=data.overall_score,
        coverage_score=data.coverage_score,
        required_skill_score=data.required_skill_score,
        preferred_skill_score=data.preferred_skill_score,
        analysis_json=data.model_dump_json(),
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_cached_match(
            db,
            job_id=data.job_id,
            candidate_profile_version=data.candidate_profile_version,
            job_snapshot_fingerprint=job_snapshot_fingerprint,
            algorithm_version=data.algorithm_version,
        )
        if existing is None:
            raise CandidateJobMatchConsistencyError(
                "Expected a candidate_job_matches row to exist after a UNIQUE constraint "
                "collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def to_candidate_job_match(record: CandidateJobMatchRecord) -> CandidateJobMatch:
    """Convert a persisted row into its typed API response shape. Never
    logs or otherwise exposes the record outside this explicit, structured
    conversion (privacy-safe logging — see app/api/routes.py, which logs
    only job_id/profile_version/match_id/algorithm_version, never
    candidate content).
    """
    payload = json.loads(record.analysis_json)
    return CandidateJobMatch(**payload, id=record.id, created_at=record.created_at)
