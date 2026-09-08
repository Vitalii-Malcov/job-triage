"""Persistence for Stage 8E `TelegramDigestDeliveryRecord` -- the
idempotency primitive for the optional DAILY Telegram digest. Mirrors
`app.db.response_draft_approval_repository`'s claim/CAS shape exactly
(INSERT + IntegrityError-catch for the first claim; CAS UPDATEs
conditioned on `id` + expected `status` for every state transition) --
see that module's docstring for the full concurrency rationale, which
applies here unchanged with "send one Telegram message" in place of
"send one email".
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import TelegramDigestDeliveryRecord

# Codex Stage 8E MEDIUM finding (STALE PENDING): bounds how long a
# claimed-but-unresolved PENDING row may sit before it is treated as
# abandoned (the process that won `claim_delivery` crashed, or
# otherwise stopped, before it could call
# mark_sent/mark_failed/mark_uncertain). Generous relative to a single
# send attempt (bounded by `settings.telegram_timeout_seconds`, default
# 5s) so a genuinely in-flight attempt is never mistaken for stale, but
# short enough that a crashed claim self-heals the SAME day rather than
# blocking every remaining poll tick until the date rolls over.
STALE_PENDING_TTL_SECONDS = 300.0


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
    db: Session, account_key: str, digest_date: date, *, now: datetime | None = None
) -> tuple[TelegramDigestDeliveryRecord, bool]:
    """Insert-only FIRST-attempt claim for `(account_key, digest_date)`.
    Returns `(record, claimed)` -- `claimed=True` means THIS call won
    the race and may proceed to attempt the Telegram send with the row
    in `PENDING` state. `claimed=False` means a row already existed
    (another process already claimed, sent, or failed this date's
    digest) -- the caller must inspect its `status` rather than assume
    anything, exactly like
    app.db.response_draft_approval_repository.claim_send_attempt.

    `now` is a test-injectable override for `created_at`/`updated_at`
    (mirrors `app.db.automation_schedule_repository.get_or_create_schedule`'s
    own `now` parameter) -- production callers never pass it, so the
    real wall clock is used exactly as before. Tests exercising
    `reconcile_stale_pending_to_uncertain`'s bounded-TTL CAS need this:
    without it, a claim's `updated_at` would always be the REAL current
    time, which can never be made consistent with a simulated/injected
    `now` used elsewhere in the same test.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    existing = get_delivery(db, account_key, digest_date)
    if existing is not None:
        return existing, False

    record = TelegramDigestDeliveryRecord(
        account_key=account_key,
        digest_date=digest_date,
        status="PENDING",
        attempt_count=1,
        created_at=effective_now,
        updated_at=effective_now,
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


def reconcile_stale_pending_to_uncertain(
    db: Session,
    record: TelegramDigestDeliveryRecord,
    *,
    ttl_seconds: float = STALE_PENDING_TTL_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Codex Stage 8E MEDIUM finding (STALE PENDING): CAS
    `PENDING -> UNCERTAIN` for a claim whose `updated_at` is older than
    `ttl_seconds` -- the crash-recovery counterpart to
    `app.db.automation_repository.reconcile_stale_run_to_failed`,
    applied to a digest delivery claim instead of an `AutomationRun`
    lease.

    A PENDING row this old means whichever process won `claim_delivery`
    (or `retry_delivery`) crashed, or otherwise stopped, BEFORE it could
    call `mark_sent`/`mark_failed`/`mark_uncertain` -- the real Telegram
    send outcome for that attempt is now permanently unknowable (it may
    have been sent, or never even attempted). Per
    `TelegramDigestDeliveryRecord`'s docstring, an outcome that cannot
    be proven either way is always `UNCERTAIN`, never retried -- so
    this reconciliation is deliberately terminal for THIS
    `(account_key, digest_date)`, exactly like a fresh `UNCERTAIN`
    outcome: the digest simply resumes on the next calendar date, never
    sent for this one after being reconciled.

    The `WHERE status='PENDING' AND updated_at < cutoff` predicate is
    evaluated atomically by the UPDATE itself -- a LIVE claim (still
    being actively processed, `updated_at` too recent) is never
    touched, and if two concurrent callers both observe the same stale
    row, only ONE UPDATE actually matches (mirrors
    `reconcile_stale_run_to_failed`'s own CAS rationale exactly).
    Returns True only if THIS call won that CAS -- callers must NEVER
    proceed to attempt a send in the same tick this returns True for
    (see `app.services.scheduler.run_due_digest_if_claimed`).
    """
    effective_now = now if now is not None else datetime.now(UTC)
    cutoff = effective_now - timedelta(seconds=ttl_seconds)
    result = db.execute(
        update(TelegramDigestDeliveryRecord)
        .where(
            TelegramDigestDeliveryRecord.id == record.id,
            TelegramDigestDeliveryRecord.status == "PENDING",
            TelegramDigestDeliveryRecord.updated_at < cutoff,
        )
        .values(
            status="UNCERTAIN",
            last_error="stale_pending_reconciled",
            updated_at=effective_now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    won = result.rowcount == 1
    if won:
        db.refresh(record)
    return won
