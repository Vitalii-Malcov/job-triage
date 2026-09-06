"""Persistence for Stage 7E `FollowUpProposalRecord` — bounded matched-
thread lookup, idempotent proposal writes, and read/list access. Mirrors
app.db.gmail_analysis_repository / app.db.response_draft_repository's
conventions (plain functions, `db: Session` first arg, INSERT +
IntegrityError-catch + reload for idempotency, account_key scoping on
every read).
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.db.gmail_repository import list_messages_for_thread
from app.db.models import (
    FollowUpProposalRecord,
    GmailMessageAnalysisRecord,
    GmailMessageRecord,
)
from app.models.follow_up import FollowUpProposal
from app.services.follow_up_eligibility import ThreadMessageInfo

# Bounded, always (mirrors app.services.email_matching's own scan bounds):
# how many distinct Gmail threads app.services.follow_up considers "the"
# matched thread pool for one job before giving up rather than risking an
# unbounded scan, and how many messages within one thread are read to
# compute outbound/inbound timestamps.
FOLLOW_UP_MATCHED_THREAD_SCAN_LIMIT = 50
FOLLOW_UP_THREAD_MESSAGE_SCAN_LIMIT = 200

FOLLOW_UP_LIST_DEFAULT_LIMIT = 50
FOLLOW_UP_LIST_MAX_LIMIT = 200

# Only a message whose LATEST Stage 7B analysis decisively matched a job
# (not AMBIGUOUS/UNMATCHED) is trusted to identify WHICH thread belongs to
# that job — mirrors app.services.email_matching's own
# `_APPLICATION_STATUSES`-style decisive-match-type convention.
_DECISIVE_MATCH_TYPES = ("APPLICATION", "JOB_ONLY")


class FollowUpRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors
    app.db.response_draft_repository.ResponseDraftRepositoryConsistencyError.
    No code path in this project deletes or updates a
    FollowUpProposalRecord row, so this should be unreachable.
    """


def _latest_analysis_id_subquery():
    """Correlated scalar subquery for the truly latest analysis revision
    per message — identical technique to
    app.db.gmail_analysis_repository._latest_id_subquery (duplicated
    rather than imported: that helper is private to its own module, and
    this project's convention — see e.g. the Alembic downgrade preflight
    checks duplicated across migrations — is to duplicate small,
    self-contained helpers across module boundaries rather than reach
    into another module's private symbols).
    """
    latest = aliased(GmailMessageAnalysisRecord)
    return (
        select(latest.id)
        .where(latest.gmail_message_id == GmailMessageAnalysisRecord.gmail_message_id)
        .order_by(latest.analysis_version.desc(), latest.id.desc())
        .limit(1)
        .correlate(GmailMessageAnalysisRecord)
        .scalar_subquery()
    )


def get_matched_thread_ids_for_job(
    db: Session, account_key: str, job_id: int, limit: int = FOLLOW_UP_MATCHED_THREAD_SCAN_LIMIT
) -> frozenset[int]:
    """Every distinct `gmail_threads.id` containing at least one message
    whose LATEST Stage 7B analysis decisively matched `job_id`
    (`matched_job_id == job_id`, `match_type` in APPLICATION/JOB_ONLY).
    Bounded to `limit` distinct threads — a job legitimately correlated
    with more than a handful of threads would be unusual; the eligibility
    rule (app.services.follow_up_eligibility) treats anything other than
    EXACTLY ONE matched thread as ambiguous/missing and refuses to guess,
    so this bound only protects against a pathological scan, never
    silently truncates a normal single-thread result.
    """
    rows = db.execute(
        select(GmailMessageRecord.thread_id)
        .join(
            GmailMessageAnalysisRecord,
            GmailMessageAnalysisRecord.gmail_message_id == GmailMessageRecord.id,
        )
        .where(
            GmailMessageRecord.account_key == account_key,
            GmailMessageAnalysisRecord.account_key == account_key,
            GmailMessageAnalysisRecord.matched_job_id == job_id,
            GmailMessageAnalysisRecord.match_type.in_(_DECISIVE_MATCH_TYPES),
            GmailMessageAnalysisRecord.id == _latest_analysis_id_subquery(),
        )
        .distinct()
        .limit(limit)
    ).all()
    return frozenset(row[0] for row in rows)


def get_thread_message_infos(
    db: Session,
    account_key: str,
    thread_id: int,
    limit: int = FOLLOW_UP_THREAD_MESSAGE_SCAN_LIMIT,
) -> list[ThreadMessageInfo]:
    """Every message in `thread_id` (already the one job-matched thread —
    see `get_matched_thread_ids_for_job`), reduced to the direction/
    timestamp pair app.services.follow_up_eligibility needs. Reuses
    app.db.gmail_repository.list_messages_for_thread — the same bounded,
    account-scoped, already-tested query the thread-detail API uses —
    rather than re-deriving thread membership here. `timestamp` prefers
    the message's own `sent_at` (its real Date header) and falls back to
    `received_at` (when this sync run persisted it) only when `sent_at`
    is unknown — never `JobRecord.first_seen_at`/`last_seen_at`.
    """
    records = list_messages_for_thread(db, account_key, thread_id, limit)
    return [
        ThreadMessageInfo(
            gmail_message_id=record.id,
            direction=record.direction,
            timestamp=_ensure_utc(record.sent_at or record.received_at),
        )
        for record in records
    ]


def _ensure_utc(value: datetime) -> datetime:
    """SQLite (unlike Postgres) doesn't preserve tzinfo through a
    `DateTime(timezone=True)` round-trip — a value stored as UTC comes
    back naive, which would otherwise raise
    `TypeError: can't compare offset-naive and offset-aware datetimes`
    the moment app.services.follow_up_eligibility compares it against a
    tz-aware `now`. Every `sent_at`/`received_at` value this project ever
    writes is UTC (see app/providers/email/imap.py and
    GmailMessageRecord's `default=lambda: datetime.now(UTC)`), so a naive
    read is always safe to reattach as UTC — mirrors the identical fix in
    app.services.company_research.CompanyResearchService._is_fresh.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def get_follow_up_proposal_by_anchor(
    db: Session, account_key: str, anchor_gmail_message_id: int
) -> FollowUpProposalRecord | None:
    return db.scalar(
        select(FollowUpProposalRecord).where(
            FollowUpProposalRecord.account_key == account_key,
            FollowUpProposalRecord.anchor_gmail_message_id == anchor_gmail_message_id,
        )
    )


def get_or_create_follow_up_proposal(
    db: Session,
    *,
    account_key: str,
    job_id: int,
    gmail_thread_id: int,
    anchor_gmail_message_id: int,
    eligibility_reason: str,
    due_at,
    subject: str,
    body: str,
    language: str,
    missing_fields: Sequence[str],
    provider: str,
    generator_version: str,
) -> tuple[FollowUpProposalRecord, bool]:
    """Idempotent write of one follow-up proposal, keyed on
    `(account_key, anchor_gmail_message_id)` — see
    `FollowUpProposalRecord`'s docstring for why re-evaluating the same
    still-eligible anchor is always safe (spec: "duplicate scan
    idempotent"). Returns `(record, created)` — `created=False` for an
    already-persisted anchor, in which case the pre-existing row is
    returned UNCHANGED (this table is never UPDATEd).

    Concurrency: if two callers race to propose a follow-up for the same
    anchor, the loser's INSERT fails on the UNIQUE constraint; caught
    below, rolled back, and resolved by re-reading the winner's row —
    never a double-insert (mirrors
    app.db.response_draft_repository.get_or_create_response_draft).
    """
    existing = get_follow_up_proposal_by_anchor(db, account_key, anchor_gmail_message_id)
    if existing is not None:
        return existing, False

    record = FollowUpProposalRecord(
        account_key=account_key,
        job_id=job_id,
        gmail_thread_id=gmail_thread_id,
        anchor_gmail_message_id=anchor_gmail_message_id,
        eligibility_reason=eligibility_reason,
        due_at=due_at,
        subject=subject,
        body=body,
        language=language,
        missing_fields_json=json.dumps(list(missing_fields)),
        provider=provider,
        generator_version=generator_version,
        status="PROPOSED",
        requires_human_review=True,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_follow_up_proposal_by_anchor(db, account_key, anchor_gmail_message_id)
        if existing is None:
            raise FollowUpRepositoryConsistencyError(
                f"Expected a follow_up_proposals row for account_key={account_key!r} "
                f"anchor_gmail_message_id={anchor_gmail_message_id!r} after a UNIQUE "
                "constraint collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def get_follow_up_proposal_by_id(
    db: Session, account_key: str, follow_up_proposal_id: int
) -> FollowUpProposalRecord | None:
    return db.scalar(
        select(FollowUpProposalRecord).where(
            FollowUpProposalRecord.id == follow_up_proposal_id,
            FollowUpProposalRecord.account_key == account_key,
        )
    )


def list_follow_up_proposals(
    db: Session,
    account_key: str,
    limit: int = FOLLOW_UP_LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> list[FollowUpProposalRecord]:
    stmt = (
        select(FollowUpProposalRecord)
        .where(FollowUpProposalRecord.account_key == account_key)
        .order_by(FollowUpProposalRecord.created_at.desc(), FollowUpProposalRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def to_follow_up_proposal(record: FollowUpProposalRecord) -> FollowUpProposal:
    return FollowUpProposal(
        id=record.id,
        job_id=record.job_id,
        gmail_thread_id=record.gmail_thread_id,
        anchor_gmail_message_id=record.anchor_gmail_message_id,
        eligibility_reason=record.eligibility_reason,
        due_at=record.due_at,
        subject=record.subject,
        body=record.body,
        language=record.language,
        missing_fields=json.loads(record.missing_fields_json),
        provider=record.provider,
        generator_version=record.generator_version,
        status=record.status,
        requires_human_review=record.requires_human_review,
        created_at=record.created_at,
    )
