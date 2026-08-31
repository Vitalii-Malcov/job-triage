"""Bewerbung draft persistence (Stage 6D).

Unlike app.db.candidate_cv_draft_repository / candidate_job_match_repository,
there is no cache-identity UNIQUE constraint here and therefore no
IntegrityError-catch-and-reload race handling — every call to
create_bewerbung_draft always inserts a new row (see
BewerbungDraftRecord's own docstring in app/db/models.py for why:
intentional regeneration, not a cache).
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BewerbungDraftRecord
from app.models.bewerbung import BewerbungDraft, BewerbungDraftData

logger = logging.getLogger(__name__)


def get_bewerbung_draft_by_id(db: Session, draft_id: int) -> BewerbungDraftRecord | None:
    """Pure read of one immutable draft snapshot, by its own id — used by
    GET /api/v1/bewerbung-drafts/{draft_id}. Never generates.
    """
    return db.get(BewerbungDraftRecord, draft_id)


def get_latest_bewerbung_draft(db: Session, job_id: int) -> BewerbungDraftRecord | None:
    """Pure read for GET /jobs/{id}/bewerbung-draft: the most recently
    created draft for this job, regardless of whether it is still fresh
    relative to the current candidate profile version, job content, or the
    CV draft/match it was generated from. Never generates.
    """
    stmt = (
        select(BewerbungDraftRecord)
        .where(BewerbungDraftRecord.job_id == job_id)
        .order_by(BewerbungDraftRecord.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def create_bewerbung_draft(db: Session, data: BewerbungDraftData) -> BewerbungDraftRecord:
    """Persist a freshly generated Bewerbung draft. Always inserts a new
    row — see module docstring for why there is no cache-identity/race
    handling here, unlike Stage 6B/6C's create_match/create_draft.
    """
    record = BewerbungDraftRecord(
        job_id=data.job_id,
        cv_draft_id=data.cv_draft_id,
        match_id=data.match_id,
        candidate_profile_version=data.candidate_profile_version,
        job_snapshot_fingerprint=data.job_snapshot_fingerprint,
        match_algorithm_version=data.match_algorithm_version,
        cv_adapter_version=data.cv_adapter_version,
        bewerbung_generator_version=data.bewerbung_generator_version,
        provider=data.provider,
        status=data.status,
        draft_json=data.model_dump_json(),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def to_bewerbung_draft(record: BewerbungDraftRecord) -> BewerbungDraft:
    """Convert a persisted row into its typed API response shape. Never
    logs or otherwise exposes the record outside this explicit, structured
    conversion (privacy-safe logging — see app/services/bewerbung.py, which
    logs only job_id/cv_draft_id/bewerbung_draft_id/provider/
    generator_version/status, never candidate or letter content).
    """
    payload = json.loads(record.draft_json)
    return BewerbungDraft(**payload, id=record.id, created_at=record.created_at)
