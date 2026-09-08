"""Persistence for Stage 8E `TelegramDigestDeliveryRecord` -- the
idempotency primitive for the optional DAILY Telegram digest. Mirrors
`app.db.response_draft_approval_repository`'s claim/CAS shape exactly
(INSERT + IntegrityError-catch for the first claim; CAS UPDATEs
conditioned on `id` + expected `status` for every state transition) --
see that module's docstring for the full concurrency rationale, which
applies here unchanged with "send one Telegram message" in place of
"send one email".
"""

from datetime import UTC, date, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import TelegramDigestDeliveryRecord


class TelegramDigestRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway -- mirrors
    app.db.response_draft_approval_repository.ResponseDraftApprovalRepositoryConsistencyError.
    """


def get_delivery(
    db: Session, account_key: str, digest_date: date
) -> TelegramDigestDeliveryRecord | None:
    return db.scalar(
        select(TelegramDigestDeliveryRecord).where(
            TelegramDigestDeliveryRecord.account_key == account_key,
            TelegramDigestDeliveryRecord.digest_date == digest_date,
        )
    )


def claim_delivery(
    db: Session, account_key: str, digest_date: date
) -> tuple[TelegramDigestDeliveryRecord, bool]:
    """Insert-only FIRST-attempt claim for `(account_key, digest_date)`.
    Returns `(record, claimed)` -- `claimed=True` means THIS call won
    the race and may proceed to attempt the Telegram send with the row
    in `PENDING` state. `claimed=False` means a row already existed
    (another process already claimed, sent, or failed this date's
    digest) -- the caller must inspect its `status` rather than assume
    anything, exactly like
    app.db.response_draft_approval_repository.claim_send_attempt.
    """
    existing = get_delivery(db, account_key, digest_date)
    if existing is not None:
        return existing, False

    record = TelegramDigestDeliveryRecord(
        account_key=account_key,
        digest_date=digest_date,
        status="PENDING",
        attempt_count=1,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_delivery(db, account_key, digest_date)
        if existing is None:
            raise TelegramDigestRepositoryConsistencyError(
                f"Expected a telegram_digest_deliveries row for "
                f"account_key={account_key!r} digest_date={digest_date!r} after a "
                "UNIQUE constraint collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def retry_delivery(db: Session, record: TelegramDigestDeliveryRecord) -> bool:
    """CAS `FAILED -> PENDING` retry claim, guarded on `id` + the
    caller's own in-hand `status == 'FAILED'` snapshot -- exactly one
    concurrent retry attempt can ever win this transition. Returns
    whether THIS call won. Never matches an `UNCERTAIN` row (see
    `TelegramDigestDeliveryRecord`'s docstring: an uncertain outcome is
    never automatically retried).
    """
    result = db.execute(
        update(TelegramDigestDeliveryRecord)
        .where(
            TelegramDigestDeliveryRecord.id == record.id,
            TelegramDigestDeliveryRecord.status == "FAILED",
        )
        .values(
            status="PENDING",
            attempt_count=TelegramDigestDeliveryRecord.attempt_count + 1,
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won


def mark_sent(db: Session, record: TelegramDigestDeliveryRecord) -> bool:
    """CAS `PENDING -> SENT`, executed ONLY after Telegram's API has
    positively confirmed delivery (2xx response) -- never called
    speculatively."""
    now = datetime.now(UTC)
    result = db.execute(
        update(TelegramDigestDeliveryRecord)
        .where(
            TelegramDigestDeliveryRecord.id == record.id,
            TelegramDigestDeliveryRecord.status == "PENDING",
        )
        .values(status="SENT", sent_at=now, updated_at=now)
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won


def mark_failed(db: Session, record: TelegramDigestDeliveryRecord, *, last_error: str) -> bool:
    """CAS `PENDING -> FAILED` -- for a send that DEFINITELY did not
    reach Telegram (connection-level error, or a non-2xx response
    Telegram itself returned). `retry_delivery` can still move this row
    back to `PENDING` on a later poll tick, same calendar date."""
    result = db.execute(
        update(TelegramDigestDeliveryRecord)
        .where(
            TelegramDigestDeliveryRecord.id == record.id,
            TelegramDigestDeliveryRecord.status == "PENDING",
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


def mark_uncertain(db: Session, record: TelegramDigestDeliveryRecord, *, last_error: str) -> bool:
    """CAS `PENDING -> UNCERTAIN` -- the fail-closed terminal transition
    for a send whose outcome could not be proven either way. Never
    automatically retried (see `TelegramDigestDeliveryRecord`'s
    docstring) -- the digest simply resumes on the next calendar date."""
    result = db.execute(
        update(TelegramDigestDeliveryRecord)
        .where(
            TelegramDigestDeliveryRecord.id == record.id,
            TelegramDigestDeliveryRecord.status == "PENDING",
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
