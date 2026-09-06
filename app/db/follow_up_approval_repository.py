"""Persistence for Stage 7E human approval + send state
(`FollowUpApprovalRecord` / `FollowUpSendRecord`) — see
app/services/follow_up_send.py for orchestration. This module is a
line-for-line mirror of app.db.response_draft_approval_repository, with
`FollowUp*` records in place of `ResponseDraft*` ones — see that module's
docstring for the full concurrency/idempotency rationale (INSERT +
IntegrityError catch for `create_approval`/`claim_send_attempt`; CAS
UPDATEs conditioned on `id` + expected `status` for every state
transition).
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import FollowUpApprovalRecord, FollowUpProposalRecord, FollowUpSendRecord
from app.models.follow_up import FollowUpApproval, FollowUpSendStatus


class FollowUpApprovalRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors
    app.db.response_draft_approval_repository.ResponseDraftApprovalRepositoryConsistencyError.
    """


def get_follow_up_proposal_by_id(
    db: Session, account_key: str, follow_up_proposal_id: int
) -> FollowUpProposalRecord | None:
    return db.scalar(
        select(FollowUpProposalRecord).where(
            FollowUpProposalRecord.id == follow_up_proposal_id,
            FollowUpProposalRecord.account_key == account_key,
        )
    )


def get_approval_for_proposal(
    db: Session, account_key: str, follow_up_proposal_id: int
) -> FollowUpApprovalRecord | None:
    return db.scalar(
        select(FollowUpApprovalRecord).where(
            FollowUpApprovalRecord.follow_up_proposal_id == follow_up_proposal_id,
            FollowUpApprovalRecord.account_key == account_key,
        )
    )


def create_approval(
    db: Session,
    *,
    account_key: str,
    follow_up_proposal_id: int,
    gmail_message_id: int,
    decision: str,
    decision_note: str | None,
    pinned_subject: str,
    pinned_body: str,
) -> tuple[FollowUpApprovalRecord, bool]:
    """Insert-only decision write. Returns `(record, created)` —
    `created=False` means a decision ALREADY existed for this
    `follow_up_proposal_id` — a decision is permanent, never overwritten.
    """
    existing = get_approval_for_proposal(db, account_key, follow_up_proposal_id)
    if existing is not None:
        return existing, False

    record = FollowUpApprovalRecord(
        account_key=account_key,
        follow_up_proposal_id=follow_up_proposal_id,
        gmail_message_id=gmail_message_id,
        decision=decision,
        decision_note=decision_note,
        pinned_subject=pinned_subject,
        pinned_body=pinned_body,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_approval_for_proposal(db, account_key, follow_up_proposal_id)
        if existing is None:
            raise FollowUpApprovalRepositoryConsistencyError(
                f"Expected a follow_up_approvals row for "
                f"follow_up_proposal_id={follow_up_proposal_id!r} after a UNIQUE "
                "constraint collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def get_send_for_proposal(
    db: Session, account_key: str, follow_up_proposal_id: int
) -> FollowUpSendRecord | None:
    return db.scalar(
        select(FollowUpSendRecord).where(
            FollowUpSendRecord.follow_up_proposal_id == follow_up_proposal_id,
            FollowUpSendRecord.account_key == account_key,
        )
    )


def claim_send_attempt(
    db: Session,
    *,
    account_key: str,
    follow_up_proposal_id: int,
    gmail_message_id: int,
    approval_id: int,
) -> tuple[FollowUpSendRecord, bool]:
    """Insert-only FIRST-attempt claim — see
    app.db.response_draft_approval_repository.claim_send_attempt's
    docstring for the full rationale, which applies here unchanged."""
    existing = get_send_for_proposal(db, account_key, follow_up_proposal_id)
    if existing is not None:
        return existing, False

    record = FollowUpSendRecord(
        account_key=account_key,
        follow_up_proposal_id=follow_up_proposal_id,
        approval_id=approval_id,
        gmail_message_id=gmail_message_id,
        status="PENDING",
        attempt_count=1,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_send_for_proposal(db, account_key, follow_up_proposal_id)
        if existing is None:
            raise FollowUpApprovalRepositoryConsistencyError(
                f"Expected a follow_up_sends row for "
                f"follow_up_proposal_id={follow_up_proposal_id!r} after a UNIQUE "
                "constraint collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def retry_send_attempt(db: Session, record: FollowUpSendRecord) -> bool:
    """CAS `FAILED -> PENDING` retry claim — see
    app.db.response_draft_approval_repository.retry_send_attempt's
    docstring."""
    result = db.execute(
        update(FollowUpSendRecord)
        .where(
            FollowUpSendRecord.id == record.id,
            FollowUpSendRecord.status == "FAILED",
        )
        .values(
            status="PENDING",
            attempt_count=FollowUpSendRecord.attempt_count + 1,
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won


def mark_send_sent(
    db: Session, record: FollowUpSendRecord, *, provider_message_id: str | None
) -> bool:
    """CAS `PENDING -> SENT`, executed ONLY after the outbound provider
    has already confirmed success."""
    result = db.execute(
        update(FollowUpSendRecord)
        .where(
            FollowUpSendRecord.id == record.id,
            FollowUpSendRecord.status == "PENDING",
        )
        .values(
            status="SENT",
            provider_message_id=provider_message_id,
            sent_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won


def mark_send_failed(db: Session, record: FollowUpSendRecord, *, last_error: str) -> bool:
    """CAS `PENDING -> FAILED` — the approval is NOT consumed by this
    transition; `retry_send_attempt` can still move this row back to
    `PENDING` later."""
    result = db.execute(
        update(FollowUpSendRecord)
        .where(
            FollowUpSendRecord.id == record.id,
            FollowUpSendRecord.status == "PENDING",
        )
        .values(
            status="FAILED",
            last_error=last_error[:500],
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won


def mark_send_uncertain(db: Session, record: FollowUpSendRecord, *, last_error: str) -> bool:
    """CAS `PENDING -> UNCERTAIN` — the fail-closed terminal transition
    for a send whose outcome the outbound provider could not prove either
    way. Deliberately does NOT increment `attempt_count` — never
    automatically retried."""
    result = db.execute(
        update(FollowUpSendRecord)
        .where(
            FollowUpSendRecord.id == record.id,
            FollowUpSendRecord.status == "PENDING",
        )
        .values(
            status="UNCERTAIN",
            last_error=last_error[:500],
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won


def to_follow_up_approval(record: FollowUpApprovalRecord) -> FollowUpApproval:
    return FollowUpApproval(
        id=record.id,
        follow_up_proposal_id=record.follow_up_proposal_id,
        gmail_message_id=record.gmail_message_id,
        decision=record.decision,
        decision_note=record.decision_note,
        pinned_subject=record.pinned_subject,
        pinned_body=record.pinned_body,
        decided_at=record.decided_at,
    )


def to_follow_up_send_status(record: FollowUpSendRecord) -> FollowUpSendStatus:
    return FollowUpSendStatus(
        id=record.id,
        follow_up_proposal_id=record.follow_up_proposal_id,
        gmail_message_id=record.gmail_message_id,
        status=record.status,
        attempt_count=record.attempt_count,
        provider_message_id=record.provider_message_id,
        last_error=record.last_error,
        sent_at=record.sent_at,
    )
