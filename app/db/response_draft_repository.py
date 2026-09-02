"""Persistence for Stage 7C `ResponseDraftRecord` results — idempotent
immutable-revision writes plus read/history access. Mirrors
app.db.gmail_analysis_repository's conventions (plain functions,
`db: Session` first arg, INSERT + IntegrityError-catch + reload for
idempotency, account_key scoping on every read).
"""

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ResponseDraftRecord
from app.models.response_draft import ResponseDraft

# Bounded, most-recent-first history page size defaults — mirrors
# GMAIL_ANALYSES_DEFAULT_LIST_LIMIT / GMAIL_ANALYSES_MAX_LIST_LIMIT in
# app/api/routes.py.
RESPONSE_DRAFT_HISTORY_DEFAULT_LIMIT = 20
RESPONSE_DRAFT_HISTORY_MAX_LIMIT = 100


class ResponseDraftRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors
    app.db.gmail_analysis_repository.GmailAnalysisRepositoryConsistencyError.
    No code path in this project deletes or updates a ResponseDraftRecord
    row, so this should be unreachable; raised instead of silently
    returning None.
    """


def get_response_draft_identity(
    db: Session,
    *,
    gmail_message_id: int,
    analysis_id: int,
    candidate_profile_version: int,
    generator_version: str,
) -> ResponseDraftRecord | None:
    return db.scalar(
        select(ResponseDraftRecord).where(
            ResponseDraftRecord.gmail_message_id == gmail_message_id,
            ResponseDraftRecord.analysis_id == analysis_id,
            ResponseDraftRecord.candidate_profile_version == candidate_profile_version,
            ResponseDraftRecord.generator_version == generator_version,
        )
    )


def get_or_create_response_draft(
    db: Session,
    *,
    account_key: str,
    gmail_message_id: int,
    analysis_id: int,
    analysis_version: int,
    candidate_profile_version: int,
    matched_job_id: int | None,
    classification: str,
    status: str,
    reason: str | None,
    subject: str | None,
    body: str | None,
    language: str | None,
    missing_fields: Sequence[str],
    provider: str,
    generator_version: str,
) -> tuple[ResponseDraftRecord, bool]:
    """Idempotent write of one immutable response-draft revision. Returns
    `(record, created)` — `created=False` for an already-persisted
    `(gmail_message_id, analysis_id, candidate_profile_version,
    generator_version)` identity, in which case the pre-existing row is
    returned UNCHANGED (this table is never UPDATEd — see
    ResponseDraftRecord's docstring).

    Concurrency: if two callers race to generate a draft for the same
    identity, the loser's INSERT fails on the UNIQUE constraint; caught
    below, rolled back, and resolved by re-reading the winner's row —
    never a double-insert, never an unhandled exception (mirrors
    app.db.gmail_analysis_repository.get_or_create_analysis).
    """
    existing = get_response_draft_identity(
        db,
        gmail_message_id=gmail_message_id,
        analysis_id=analysis_id,
        candidate_profile_version=candidate_profile_version,
        generator_version=generator_version,
    )
    if existing is not None:
        return existing, False

    record = ResponseDraftRecord(
        account_key=account_key,
        gmail_message_id=gmail_message_id,
        analysis_id=analysis_id,
        analysis_version=analysis_version,
        candidate_profile_version=candidate_profile_version,
        matched_job_id=matched_job_id,
        classification=classification,
        status=status,
        reason=reason,
        subject=subject,
        body=body,
        language=language,
        missing_fields_json=json.dumps(list(missing_fields)),
        provider=provider,
        generator_version=generator_version,
        requires_human_review=True,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_response_draft_identity(
            db,
            gmail_message_id=gmail_message_id,
            analysis_id=analysis_id,
            candidate_profile_version=candidate_profile_version,
            generator_version=generator_version,
        )
        if existing is None:
            raise ResponseDraftRepositoryConsistencyError(
                f"Expected a response_drafts row for gmail_message_id={gmail_message_id!r} "
                f"analysis_id={analysis_id!r} "
                f"candidate_profile_version={candidate_profile_version!r} "
                f"generator_version={generator_version!r} after a UNIQUE constraint "
                "collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def get_latest_response_draft_for_message(
    db: Session, account_key: str, gmail_message_id: int
) -> ResponseDraftRecord | None:
    """The most recent response-draft revision for one message — GET
    /gmail/messages/{id}/response-draft. Account-scoped (GMAIL-002-style
    isolation, consistent with every other Gmail-adjacent read in this
    project).
    """
    return db.scalar(
        select(ResponseDraftRecord)
        .where(
            ResponseDraftRecord.gmail_message_id == gmail_message_id,
            ResponseDraftRecord.account_key == account_key,
        )
        .order_by(ResponseDraftRecord.created_at.desc(), ResponseDraftRecord.id.desc())
        .limit(1)
    )


def list_response_drafts_for_message(
    db: Session,
    account_key: str,
    gmail_message_id: int,
    limit: int = RESPONSE_DRAFT_HISTORY_DEFAULT_LIMIT,
    offset: int = 0,
) -> list[ResponseDraftRecord]:
    """Bounded, account-scoped, most-recent-first FULL history of every
    response-draft revision for one message — never deduplicated to
    "latest" (see GET /gmail/messages/{id}/response-draft for that).
    """
    stmt = (
        select(ResponseDraftRecord)
        .where(
            ResponseDraftRecord.gmail_message_id == gmail_message_id,
            ResponseDraftRecord.account_key == account_key,
        )
        .order_by(ResponseDraftRecord.created_at.desc(), ResponseDraftRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def to_response_draft(record: ResponseDraftRecord) -> ResponseDraft:
    return ResponseDraft(
        id=record.id,
        gmail_message_id=record.gmail_message_id,
        analysis_id=record.analysis_id,
        analysis_version=record.analysis_version,
        matched_job_id=record.matched_job_id,
        classification=record.classification,
        status=record.status,
        reason=record.reason,
        subject=record.subject,
        body=record.body,
        language=record.language,
        missing_fields=json.loads(record.missing_fields_json),
        provider=record.provider,
        generator_version=record.generator_version,
        requires_human_review=record.requires_human_review,
        created_at=record.created_at,
    )
