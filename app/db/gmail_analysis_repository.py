"""Persistence for Stage 7B `GmailMessageAnalysisRecord` results —
bounded candidate/context queries, idempotent immutable-revision writes,
and read/list access. Mirrors app.db.gmail_repository's conventions
(plain functions, `db: Session` first arg, INSERT + IntegrityError-catch
+ reload for idempotency, account_key scoping on every read).

**Bounded, always.** `get_job_candidates` and `get_thread_prior_matches`
are the only two queries app/services/gmail_message_analysis.py issues
per analysis run — both hard-limited (see
app.services.email_matching.MATCH_CANDIDATE_SCAN_LIMIT /
THREAD_ASSOCIATION_SCAN_LIMIT), so one analyze call can never turn into
an unbounded table scan or N+1 loop regardless of how many `JobRecord`s
or thread messages exist.

**Privacy.** Nothing in this module logs message content — same
convention as app.db.gmail_repository (see its module docstring).
"""

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.email_classifier import ClassificationEvidenceItem
from app.db.models import GmailMessageAnalysisRecord, GmailMessageRecord, JobRecord
from app.models.gmail_analysis import CandidateMatch as CandidateMatchModel
from app.models.gmail_analysis import EvidenceItem, GmailMessageAnalysis
from app.services.email_matching import (
    MATCH_CANDIDATE_SCAN_LIMIT,
    THREAD_ASSOCIATION_SCAN_LIMIT,
    CandidateMatch,
    EmailMatchResult,
    JobCandidate,
    MatchEvidenceItem,
    ThreadPriorMatch,
)


class GmailAnalysisRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors app.db.gmail_repository's
    GmailRepositoryConsistencyError. No code path in this project deletes
    or updates a GmailMessageAnalysisRecord row, so this should be
    unreachable; raised instead of silently returning None.
    """


def get_job_candidates(
    db: Session, *, limit: int = MATCH_CANDIDATE_SCAN_LIMIT
) -> list[JobCandidate]:
    """The bounded candidate `JobRecord` pool matching scores against —
    ordered most-recently-seen first, capped at `limit` (spec: "Bound:
    candidate jobs/applications considered", "Avoid: full-table
    application scan"). One query, never one query per candidate.
    """
    rows = db.execute(
        select(
            JobRecord.id,
            JobRecord.title,
            JobRecord.company,
            JobRecord.location,
            JobRecord.url,
            JobRecord.status,
        )
        .order_by(JobRecord.last_seen_at.desc(), JobRecord.id.desc())
        .limit(limit)
    ).all()
    return [
        JobCandidate(
            job_id=row.id,
            title=row.title,
            company=row.company,
            location=row.location,
            url=row.url,
            status=row.status,
        )
        for row in rows
    ]


def get_thread_prior_matches(
    db: Session,
    *,
    account_key: str,
    thread_id: int,
    exclude_gmail_message_id: int,
    limit: int = THREAD_ASSOCIATION_SCAN_LIMIT,
) -> list[ThreadPriorMatch]:
    """The latest analysis result for every OTHER message already
    persisted in this (Stage-7A-vetted, see app.services.email_matching's
    module docstring) thread, restricted to a decisive match_type
    (APPLICATION/JOB_ONLY — AMBIGUOUS/UNMATCHED prior results carry no
    trustworthy association and are excluded). Bounded to `limit` most
    recent analyses; if a message has multiple revisions, only its
    latest (by created_at) is considered.
    """
    rows = db.execute(
        select(
            GmailMessageAnalysisRecord.gmail_message_id,
            GmailMessageAnalysisRecord.match_type,
            GmailMessageAnalysisRecord.matched_job_id,
            GmailMessageAnalysisRecord.created_at,
        )
        .join(
            GmailMessageRecord, GmailMessageRecord.id == GmailMessageAnalysisRecord.gmail_message_id
        )
        .where(
            GmailMessageRecord.thread_id == thread_id,
            GmailMessageRecord.account_key == account_key,
            GmailMessageAnalysisRecord.account_key == account_key,
            GmailMessageAnalysisRecord.gmail_message_id != exclude_gmail_message_id,
            GmailMessageAnalysisRecord.match_type.in_(("APPLICATION", "JOB_ONLY")),
            GmailMessageAnalysisRecord.matched_job_id.is_not(None),
        )
        .order_by(
            GmailMessageAnalysisRecord.created_at.desc(), GmailMessageAnalysisRecord.id.desc()
        )
        .limit(limit)
    ).all()

    latest_by_message: dict[int, ThreadPriorMatch] = {}
    for row in rows:
        if row.gmail_message_id in latest_by_message:
            continue  # already saw this message's latest revision (rows are newest-first)
        latest_by_message[row.gmail_message_id] = ThreadPriorMatch(
            job_id=row.matched_job_id, match_type=row.match_type
        )
    return list(latest_by_message.values())


def _evidence_to_json(items: Sequence[MatchEvidenceItem | ClassificationEvidenceItem]) -> str:
    return json.dumps(
        [{"kind": item.kind, "value": item.value, "weight": item.weight} for item in items]
    )


def _candidates_to_json(candidates: Sequence[CandidateMatch]) -> str:
    return json.dumps(
        [
            {
                "job_id": c.job_id,
                "score": c.score,
                "evidence": [
                    {"kind": e.kind, "value": e.value, "weight": e.weight} for e in c.evidence
                ],
            }
            for c in candidates
        ]
    )


def get_analysis_identity(
    db: Session, *, gmail_message_id: int, analysis_version: int, input_fingerprint: str
) -> GmailMessageAnalysisRecord | None:
    return db.scalar(
        select(GmailMessageAnalysisRecord).where(
            GmailMessageAnalysisRecord.gmail_message_id == gmail_message_id,
            GmailMessageAnalysisRecord.analysis_version == analysis_version,
            GmailMessageAnalysisRecord.input_fingerprint == input_fingerprint,
        )
    )


def get_or_create_analysis(
    db: Session,
    *,
    account_key: str,
    gmail_message_id: int,
    analysis_version: int,
    input_fingerprint: str,
    match_result: EmailMatchResult,
    classification_category: str,
    classification_confidence: str,
    classification_evidence: Sequence[ClassificationEvidenceItem],
    is_automated: bool,
    requires_human_review: bool,
) -> tuple[GmailMessageAnalysisRecord, bool]:
    """Idempotent write of one immutable analysis revision. Returns
    (record, created) — created=False for an already-persisted
    (gmail_message_id, analysis_version, input_fingerprint) identity, in
    which case the pre-existing row is returned UNCHANGED (this table is
    never UPDATEd — see GmailMessageAnalysisRecord's docstring).

    Concurrency: if two callers race to analyze the same message under
    the same version/fingerprint, the loser's INSERT fails on the UNIQUE
    constraint; caught below, rolled back, and resolved by re-reading the
    winner's row — never a double-insert, never an unhandled exception
    (mirrors app.db.gmail_repository.upsert_message).
    """
    existing = get_analysis_identity(
        db,
        gmail_message_id=gmail_message_id,
        analysis_version=analysis_version,
        input_fingerprint=input_fingerprint,
    )
    if existing is not None:
        return existing, False

    record = GmailMessageAnalysisRecord(
        account_key=account_key,
        gmail_message_id=gmail_message_id,
        analysis_version=analysis_version,
        input_fingerprint=input_fingerprint,
        match_type=match_result.match_type,
        matched_job_id=match_result.matched_job_id,
        match_confidence=match_result.confidence,
        match_score=match_result.score,
        match_evidence_json=_evidence_to_json(match_result.evidence),
        candidate_matches_json=_candidates_to_json(match_result.candidates),
        classification=classification_category,
        classification_confidence=classification_confidence,
        classification_evidence_json=_evidence_to_json(classification_evidence),
        is_automated=is_automated,
        requires_human_review=requires_human_review,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_analysis_identity(
            db,
            gmail_message_id=gmail_message_id,
            analysis_version=analysis_version,
            input_fingerprint=input_fingerprint,
        )
        if existing is None:
            raise GmailAnalysisRepositoryConsistencyError(
                f"Expected a gmail_message_analyses row for gmail_message_id="
                f"{gmail_message_id!r} analysis_version={analysis_version!r} "
                f"input_fingerprint={input_fingerprint!r} after a UNIQUE constraint "
                "collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def get_latest_analysis_for_message(
    db: Session, account_key: str, gmail_message_id: int
) -> GmailMessageAnalysisRecord | None:
    """The most recent analysis revision for one message — GET
    /gmail/messages/{id}/analysis. Account-scoped (GMAIL-002-style
    isolation, consistent with every other Gmail read in this project).
    """
    return db.scalar(
        select(GmailMessageAnalysisRecord)
        .where(
            GmailMessageAnalysisRecord.gmail_message_id == gmail_message_id,
            GmailMessageAnalysisRecord.account_key == account_key,
        )
        .order_by(
            GmailMessageAnalysisRecord.analysis_version.desc(),
            GmailMessageAnalysisRecord.created_at.desc(),
            GmailMessageAnalysisRecord.id.desc(),
        )
        .limit(1)
    )


def list_analyses(
    db: Session, account_key: str, limit: int, offset: int
) -> list[GmailMessageAnalysisRecord]:
    """Bounded, account-scoped, most-recent-first list of analysis
    revisions (GET /gmail/analyses). Each row is one persisted revision —
    not deduplicated to "latest per message" — see the endpoint's own
    docstring in app/api/routes.py for why.
    """
    stmt = (
        select(GmailMessageAnalysisRecord)
        .where(GmailMessageAnalysisRecord.account_key == account_key)
        .order_by(
            GmailMessageAnalysisRecord.created_at.desc(), GmailMessageAnalysisRecord.id.desc()
        )
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def to_gmail_message_analysis(record: GmailMessageAnalysisRecord) -> GmailMessageAnalysis:
    match_evidence = [EvidenceItem(**item) for item in json.loads(record.match_evidence_json)]
    classification_evidence = [
        EvidenceItem(**item) for item in json.loads(record.classification_evidence_json)
    ]
    candidate_matches = [
        CandidateMatchModel(
            job_id=item["job_id"],
            score=item["score"],
            evidence=[EvidenceItem(**e) for e in item["evidence"]],
        )
        for item in json.loads(record.candidate_matches_json)
    ]
    return GmailMessageAnalysis(
        id=record.id,
        gmail_message_id=record.gmail_message_id,
        analysis_version=record.analysis_version,
        match_type=record.match_type,
        matched_job_id=record.matched_job_id,
        match_confidence=record.match_confidence,
        match_score=record.match_score,
        match_evidence=match_evidence,
        candidate_matches=candidate_matches,
        classification=record.classification,
        classification_confidence=record.classification_confidence,
        classification_evidence=classification_evidence,
        is_automated=record.is_automated,
        requires_human_review=record.requires_human_review,
        created_at=record.created_at,
    )
