"""Persistence for Stage 7B `GmailMessageAnalysisRecord` results —
bounded candidate/context queries, idempotent immutable-revision writes,
and read/list access. Mirrors app.db.gmail_repository's conventions
(plain functions, `db: Session` first arg, INSERT + IntegrityError-catch
+ reload for idempotency, account_key scoping on every read).

**Codex remediation round 1.** Two concrete defects were reproduced and
fixed here:

- **7B-005**: `get_thread_prior_matches` used to filter to
  `match_type IN ('APPLICATION','JOB_ONLY')` in the SQL WHERE clause
  BEFORE picking "the latest row per message" — which meant an older
  DECISIVE revision could outrank a newer, correct UNMATCHED/AMBIGUOUS
  revision merely because the newer row got filtered out before the
  "latest" comparison ever happened. Fixed by first finding each prior
  message's truly latest revision (via `MAX(id)` — this table is
  INSERT-only, so `id` order is creation order), THEN checking whether
  THAT (and only that) row is decisive.
- **7B-007**: a fixed `LIMIT 500` recency-ordered scan could hide an
  exact reference match older than the 500 most-recently-seen jobs.
  `get_job_candidates` now also runs a second, small, targeted query for
  candidates whose own `url`/`title` contains a caller-supplied reference
  token, merged with the recency pool (deduplicated). Still bounded — at
  most 2 SQL statements total, never one query per candidate, regardless
  of table size.

**Bounded, always.** `get_job_candidates` and `get_thread_prior_matches`
are the only two queries app/services/gmail_message_analysis.py issues
per analysis run — both hard-limited (see
app.services.email_matching.MATCH_CANDIDATE_SCAN_LIMIT /
THREAD_ASSOCIATION_SCAN_LIMIT / REFERENCE_TARGETED_SCAN_LIMIT), so one
analyze call can never turn into an unbounded table scan or N+1 loop
regardless of how many `JobRecord`s or thread messages exist.

**Privacy.** Nothing in this module logs message content — same
convention as app.db.gmail_repository (see its module docstring).
"""

import hashlib
import json
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.email_classifier import ClassificationEvidenceItem
from app.db.models import GmailMessageAnalysisRecord, GmailMessageRecord, JobRecord
from app.models.gmail_analysis import CandidateMatch as CandidateMatchModel
from app.models.gmail_analysis import EvidenceItem, GmailMessageAnalysis
from app.services.email_matching import (
    MATCH_CANDIDATE_SCAN_LIMIT,
    REFERENCE_TARGETED_SCAN_LIMIT,
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


def _job_candidate_from_row(row) -> JobCandidate:
    return JobCandidate(
        job_id=row.id,
        title=row.title,
        company=row.company,
        location=row.location,
        url=row.url,
        status=row.status,
    )


def get_job_candidates(
    db: Session,
    *,
    limit: int = MATCH_CANDIDATE_SCAN_LIMIT,
    reference_tokens: frozenset[str] = frozenset(),
) -> list[JobCandidate]:
    """The bounded candidate `JobRecord` pool matching scores against.

    Two bounded queries, merged and deduplicated by job id (7B-007):

    1. A recency-ordered pool (most-recently-seen first), capped at
       `limit` (spec: "Bound: candidate jobs/applications considered",
       "Avoid: full-table application scan").
    2. If `reference_tokens` is non-empty (the email carried an explicit
       reference — see app.services.email_matching.extract_reference_tokens),
       a SECOND small query for any `JobRecord` whose own `url`/`title`
       contains one of those tokens, capped at
       `REFERENCE_TARGETED_SCAN_LIMIT` — so an exact reference match
       older than the `limit` most-recently-seen jobs is still
       discoverable, without ever scanning the whole table or issuing
       one query per candidate.
    """
    columns = (
        JobRecord.id,
        JobRecord.title,
        JobRecord.company,
        JobRecord.location,
        JobRecord.url,
        JobRecord.status,
    )
    recency_rows = db.execute(
        select(*columns).order_by(JobRecord.last_seen_at.desc(), JobRecord.id.desc()).limit(limit)
    ).all()
    candidates: dict[int, JobCandidate] = {
        row.id: _job_candidate_from_row(row) for row in recency_rows
    }

    if reference_tokens:
        conditions = [
            condition
            for token in reference_tokens
            for condition in (
                func.upper(JobRecord.url).contains(token),
                func.upper(JobRecord.title).contains(token),
            )
        ]
        targeted_rows = db.execute(
            select(*columns).where(or_(*conditions)).limit(REFERENCE_TARGETED_SCAN_LIMIT)
        ).all()
        for row in targeted_rows:
            candidates.setdefault(row.id, _job_candidate_from_row(row))

    return list(candidates.values())


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
    trustworthy association and are excluded).

    **7B-005 fix.** "Latest" is determined FIRST (via `MAX(id)` per
    `gmail_message_id` — this table is INSERT-only, so `id` order is
    creation order — a correlated-subquery / one round-trip, not N
    queries), and only THEN is that (and only that) row checked for
    decisiveness. The previous version filtered to decisive rows in the
    same WHERE clause used to pick "the latest" — which let an OLDER
    decisive revision win over a NEWER, correct UNMATCHED/AMBIGUOUS
    revision, because the newer row was filtered out of contention before
    "latest" was ever evaluated. A message whose latest revision is
    UNMATCHED/AMBIGUOUS now correctly contributes NOTHING to thread
    association, even if an earlier revision of that same message was
    once decisive.
    """
    latest_id_per_message = (
        select(func.max(GmailMessageAnalysisRecord.id).label("latest_id"))
        .join(
            GmailMessageRecord, GmailMessageRecord.id == GmailMessageAnalysisRecord.gmail_message_id
        )
        .where(
            GmailMessageRecord.thread_id == thread_id,
            GmailMessageRecord.account_key == account_key,
            GmailMessageAnalysisRecord.account_key == account_key,
            GmailMessageAnalysisRecord.gmail_message_id != exclude_gmail_message_id,
        )
        .group_by(GmailMessageAnalysisRecord.gmail_message_id)
        .subquery()
    )
    rows = db.execute(
        select(GmailMessageAnalysisRecord.match_type, GmailMessageAnalysisRecord.matched_job_id)
        .where(GmailMessageAnalysisRecord.id.in_(select(latest_id_per_message.c.latest_id)))
        .order_by(GmailMessageAnalysisRecord.id.desc())
        .limit(limit)
    ).all()

    return [
        ThreadPriorMatch(job_id=row.matched_job_id, match_type=row.match_type)
        for row in rows
        if row.match_type in ("APPLICATION", "JOB_ONLY") and row.matched_job_id is not None
    ]


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


def compute_context_fingerprint(
    job_candidates: list[JobCandidate], thread_prior_matches: list[ThreadPriorMatch]
) -> str:
    """7B-003/004: SHA-256 digest over the EFFECTIVE candidate/thread
    context an analysis run actually considered — the second half of the
    analysis identity (see `GmailMessageAnalysisRecord.context_fingerprint`
    docstring in app/db/models.py). Canonical (sorted by job_id, stable
    field order) so the same effective context always hashes identically
    regardless of query row ordering. No secrets, no PII beyond what
    JobCandidate/ThreadPriorMatch already carry (title/company/location/
    url/status — all already user-entered job-tracking data, never
    correspondence content).
    """
    canonical_candidates = [
        {
            "job_id": c.job_id,
            "title": c.title,
            "company": c.company,
            "location": c.location,
            "url": c.url,
            "status": c.status,
        }
        for c in sorted(job_candidates, key=lambda c: c.job_id)
    ]
    canonical_thread = [
        {"job_id": m.job_id, "match_type": m.match_type}
        for m in sorted(thread_prior_matches, key=lambda m: m.job_id)
    ]
    payload = json.dumps(
        {"candidates": canonical_candidates, "thread_prior_matches": canonical_thread},
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_analysis_identity(
    db: Session,
    *,
    gmail_message_id: int,
    analysis_version: int,
    input_fingerprint: str,
    context_fingerprint: str,
) -> GmailMessageAnalysisRecord | None:
    return db.scalar(
        select(GmailMessageAnalysisRecord).where(
            GmailMessageAnalysisRecord.gmail_message_id == gmail_message_id,
            GmailMessageAnalysisRecord.analysis_version == analysis_version,
            GmailMessageAnalysisRecord.input_fingerprint == input_fingerprint,
            GmailMessageAnalysisRecord.context_fingerprint == context_fingerprint,
        )
    )


def get_or_create_analysis(
    db: Session,
    *,
    account_key: str,
    gmail_message_id: int,
    analysis_version: int,
    input_fingerprint: str,
    context_fingerprint: str,
    match_result: EmailMatchResult,
    classification_category: str,
    classification_confidence: str,
    classification_evidence: Sequence[ClassificationEvidenceItem],
    is_automated: bool,
    requires_human_review: bool,
) -> tuple[GmailMessageAnalysisRecord, bool]:
    """Idempotent write of one immutable analysis revision. Returns
    (record, created) — created=False for an already-persisted
    (gmail_message_id, analysis_version, input_fingerprint,
    context_fingerprint) identity, in which case the pre-existing row is
    returned UNCHANGED (this table is never UPDATEd — see
    GmailMessageAnalysisRecord's docstring).

    **7B-003/004.** `context_fingerprint` (see `compute_context_fingerprint`)
    makes the identity sensitive to the EFFECTIVE candidate pool and
    thread context an analysis run actually considered — not just the
    message's own content. Re-analyzing the same unchanged message after
    a new matching `JobRecord` is added, or after thread context changes,
    now correctly produces a NEW revision (different context_fingerprint)
    rather than silently returning a stale cached result.

    Concurrency: if two callers race to analyze the same message under
    the same version/fingerprints, the loser's INSERT fails on the UNIQUE
    constraint; caught below, rolled back, and resolved by re-reading the
    winner's row — never a double-insert, never an unhandled exception
    (mirrors app.db.gmail_repository.upsert_message).
    """
    existing = get_analysis_identity(
        db,
        gmail_message_id=gmail_message_id,
        analysis_version=analysis_version,
        input_fingerprint=input_fingerprint,
        context_fingerprint=context_fingerprint,
    )
    if existing is not None:
        return existing, False

    record = GmailMessageAnalysisRecord(
        account_key=account_key,
        gmail_message_id=gmail_message_id,
        analysis_version=analysis_version,
        input_fingerprint=input_fingerprint,
        context_fingerprint=context_fingerprint,
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
            context_fingerprint=context_fingerprint,
        )
        if existing is None:
            raise GmailAnalysisRepositoryConsistencyError(
                f"Expected a gmail_message_analyses row for gmail_message_id="
                f"{gmail_message_id!r} analysis_version={analysis_version!r} "
                f"input_fingerprint={input_fingerprint!r} "
                f"context_fingerprint={context_fingerprint!r} after a UNIQUE "
                "constraint collision, but none was found."
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
    Ordered by `id` (insertion order — this table is INSERT-only), the
    same "latest" rule `get_thread_prior_matches` uses (7B-005).
    """
    return db.scalar(
        select(GmailMessageAnalysisRecord)
        .where(
            GmailMessageAnalysisRecord.gmail_message_id == gmail_message_id,
            GmailMessageAnalysisRecord.account_key == account_key,
        )
        .order_by(GmailMessageAnalysisRecord.id.desc())
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
