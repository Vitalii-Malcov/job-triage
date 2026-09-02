"""Persistence for Stage 7D human approval + send state
(`ResponseDraftApprovalRecord` / `ResponseDraftSendRecord`) — see
app/services/response_draft_send.py for orchestration and
app/db/models.py for the full concurrency/idempotency rationale each
table's docstring documents.

**Concurrency primitives used here, both already established elsewhere
in this project:**

- `create_approval` — INSERT + `IntegrityError` catch against
  `UNIQUE(response_draft_id)`, exactly like
  `app.db.gmail_repository`'s `GmailMessageIdClaimRecord` claim pattern.
- `claim_send_attempt` / `retry_send_attempt` / `mark_send_sent` /
  `mark_send_failed` / `mark_send_uncertain` — CAS (compare-and-swap)
  UPDATEs conditioned on `id` + expected `status`, checking
  `rowcount == 1`, exactly like `app.db.review_package_repository`'s
  `ApplicationPackageReviewRecord` status transitions.

`mark_send_uncertain`'s `PENDING -> UNCERTAIN` transition is terminal:
no function in this module ever transitions a row OUT of `UNCERTAIN` —
`retry_send_attempt`'s own CAS is scoped to `WHERE status='FAILED'`
specifically so it can never match (and therefore never resurrect) an
`UNCERTAIN` row. See `app.db.models.ResponseDraftSendRecord`'s docstring
for the full rationale.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ResponseDraftApprovalRecord, ResponseDraftRecord, ResponseDraftSendRecord
from app.models.response_draft_approval import ResponseDraftApproval, ResponseDraftSendStatus


class ResponseDraftApprovalRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors
    app.db.gmail_analysis_repository.GmailAnalysisRepositoryConsistencyError.
    """


def get_response_draft_by_id(
    db: Session, account_key: str, response_draft_id: int
) -> ResponseDraftRecord | None:
    """Pure, account-scoped read of one exact `ResponseDraftRecord` by
    its own primary key — the lookup every Stage 7D endpoint starts from
    (decision/send/state all act on one specific, already-generated
    revision, never "the latest for a message").
    """
    return db.scalar(
        select(ResponseDraftRecord).where(
            ResponseDraftRecord.id == response_draft_id,
            ResponseDraftRecord.account_key == account_key,
        )
    )


def get_approval_for_draft(
    db: Session, account_key: str, response_draft_id: int
) -> ResponseDraftApprovalRecord | None:
    return db.scalar(
        select(ResponseDraftApprovalRecord).where(
            ResponseDraftApprovalRecord.response_draft_id == response_draft_id,
            ResponseDraftApprovalRecord.account_key == account_key,
        )
    )


def create_approval(
    db: Session,
    *,
    account_key: str,
    response_draft_id: int,
    gmail_message_id: int,
    decision: str,
    decision_note: str | None,
    pinned_subject: str,
    pinned_body: str,
) -> tuple[ResponseDraftApprovalRecord, bool]:
    """Insert-only decision write. Returns `(record, created)` —
    `created=False` means a decision ALREADY existed for this
    `response_draft_id` (the winning row, whatever it decided) and
    NOTHING was written by this call — see
    `ResponseDraftApprovalRecord`'s docstring: a decision is permanent,
    never overwritten or "reconsidered" in place. The caller
    (app.services.response_draft_send) is responsible for turning
    `created=False` into the appropriate 409 — this function itself
    never raises merely because a decision already exists.
    """
    existing = get_approval_for_draft(db, account_key, response_draft_id)
    if existing is not None:
        return existing, False

    record = ResponseDraftApprovalRecord(
        account_key=account_key,
        response_draft_id=response_draft_id,
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
        existing = get_approval_for_draft(db, account_key, response_draft_id)
        if existing is None:
            raise ResponseDraftApprovalRepositoryConsistencyError(
                f"Expected a response_draft_approvals row for "
                f"response_draft_id={response_draft_id!r} after a UNIQUE constraint "
                "collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def get_send_for_draft(
    db: Session, account_key: str, response_draft_id: int
) -> ResponseDraftSendRecord | None:
    return db.scalar(
        select(ResponseDraftSendRecord).where(
            ResponseDraftSendRecord.response_draft_id == response_draft_id,
            ResponseDraftSendRecord.account_key == account_key,
        )
    )


def claim_send_attempt(
    db: Session,
    *,
    account_key: str,
    response_draft_id: int,
    gmail_message_id: int,
    approval_id: int,
) -> tuple[ResponseDraftSendRecord, bool]:
    """Insert-only FIRST-attempt claim. Returns `(record, claimed)` —
    `claimed=True` means THIS call won the race and may proceed to call
    the outbound provider with the row in `PENDING` state. `claimed=False`
    means a row already existed (another request already claimed,
    completed, or failed this send) — the caller must inspect its
    `status` rather than assume anything (see
    app.services.response_draft_send's send-attempt orchestration).
    """
    existing = get_send_for_draft(db, account_key, response_draft_id)
    if existing is not None:
        return existing, False

    record = ResponseDraftSendRecord(
        account_key=account_key,
        response_draft_id=response_draft_id,
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
        existing = get_send_for_draft(db, account_key, response_draft_id)
        if existing is None:
            raise ResponseDraftApprovalRepositoryConsistencyError(
                f"Expected a response_draft_sends row for "
                f"response_draft_id={response_draft_id!r} after a UNIQUE constraint "
                "collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def retry_send_attempt(db: Session, record: ResponseDraftSendRecord) -> bool:
    """CAS `FAILED -> PENDING` retry claim, guarded on `id` + the
    CALLER'S OWN in-hand `status == 'FAILED'` snapshot via the UPDATE's
    WHERE clause (not a separate re-read first) — exactly one concurrent
    retry attempt can ever win this transition; every other concurrent
    retry's UPDATE affects 0 rows. Returns whether THIS call won.
    """
    result = db.execute(
        update(ResponseDraftSendRecord)
        .where(
            ResponseDraftSendRecord.id == record.id,
            ResponseDraftSendRecord.status == "FAILED",
        )
        .values(
            status="PENDING",
            attempt_count=ResponseDraftSendRecord.attempt_count + 1,
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won


def mark_send_sent(
    db: Session, record: ResponseDraftSendRecord, *, provider_message_id: str | None
) -> bool:
    """CAS `PENDING -> SENT`, executed ONLY after the outbound provider
    has already confirmed success (spec: "Do not mark SENT before
    provider success") — never called speculatively. Returns whether
    this call actually performed the transition (should always be True
    in practice: the caller holds the PENDING claim it just won and
    nothing else can concurrently move a PENDING row anywhere but the
    caller itself); guarded with the same CAS discipline as every other
    transition here regardless, rather than trusting that invariant
    blindly.
    """
    result = db.execute(
        update(ResponseDraftSendRecord)
        .where(
            ResponseDraftSendRecord.id == record.id,
            ResponseDraftSendRecord.status == "PENDING",
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


def mark_send_failed(db: Session, record: ResponseDraftSendRecord, *, last_error: str) -> bool:
    """CAS `PENDING -> FAILED` — the approval is NOT consumed by this
    transition (spec: "Handle provider failure without falsely consuming
    approval"): `retry_send_attempt` can still move this same row back to
    `PENDING` later.
    """
    result = db.execute(
        update(ResponseDraftSendRecord)
        .where(
            ResponseDraftSendRecord.id == record.id,
            ResponseDraftSendRecord.status == "PENDING",
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


def mark_send_uncertain(db: Session, record: ResponseDraftSendRecord, *, last_error: str) -> bool:
    """CAS `PENDING -> UNCERTAIN` — the fail-closed terminal transition
    for a send whose outcome the outbound provider could not prove
    either way (see `app.providers.email.outbound_base.
    EmailSendOutcomeUnknownError` and `ResponseDraftSendRecord`'s own
    docstring). Deliberately does NOT increment `attempt_count` — this is
    not "a failed attempt to retry", it is a terminal, ambiguous outcome
    that is never automatically retried; a later send request for this
    same draft is refused before this table's claim logic even runs
    again (see `app.services.response_draft_send`'s
    `ResponseDraftSendOutcomeUncertainError`).
    """
    result = db.execute(
        update(ResponseDraftSendRecord)
        .where(
            ResponseDraftSendRecord.id == record.id,
            ResponseDraftSendRecord.status == "PENDING",
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


def to_response_draft_approval(record: ResponseDraftApprovalRecord) -> ResponseDraftApproval:
    return ResponseDraftApproval(
        id=record.id,
        response_draft_id=record.response_draft_id,
        gmail_message_id=record.gmail_message_id,
        decision=record.decision,
        decision_note=record.decision_note,
        pinned_subject=record.pinned_subject,
        pinned_body=record.pinned_body,
        decided_at=record.decided_at,
    )


def to_response_draft_send_status(record: ResponseDraftSendRecord) -> ResponseDraftSendStatus:
    return ResponseDraftSendStatus(
        id=record.id,
        response_draft_id=record.response_draft_id,
        gmail_message_id=record.gmail_message_id,
        status=record.status,
        attempt_count=record.attempt_count,
        provider_message_id=record.provider_message_id,
        last_error=record.last_error,
        sent_at=record.sent_at,
    )
