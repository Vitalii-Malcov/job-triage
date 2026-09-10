"""Persistence for Stage 8B `AutomationScheduleRecord` -- one row per
account tracking when its next scheduled
`app.services.automation.run_automation_cycle` cycle is due, plus the
atomic CAS claim primitive (`claim_due_schedule`) that arbitrates "which
scheduler process, if any, gets to trigger the next cycle" across
multiple concurrent worker processes. Mirrors
`app.db.automation_repository`'s own INSERT + IntegrityError-catch idiom
for the initial row creation, and its plain conditional `UPDATE ...
WHERE <observed value> ...` idiom for the CAS itself -- see
`app.db.models.AutomationScheduleRecord`'s docstring for the full
concurrency/coalescing rationale.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.datetime_utils import ensure_utc
from app.db.models import AutomationScheduleRecord


class AutomationScheduleRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway -- e.g. reloading the row a UNIQUE constraint
    IntegrityError implies must exist comes back None. Mirrors
    app.db.automation_repository.AutomationRepositoryConsistencyError.
    """


def get_schedule(db: Session, account_key: str) -> AutomationScheduleRecord | None:
    """The account's schedule row, if one has ever been created. A plain
    read -- callers that need "create if missing" use
    `get_or_create_schedule` instead.
    """
    return db.scalar(
        select(AutomationScheduleRecord).where(AutomationScheduleRecord.account_key == account_key)
    )


def get_or_create_schedule(
    db: Session, account_key: str, *, now: datetime | None = None
) -> AutomationScheduleRecord:
    """Return the account's existing schedule row, or create one seeded
    with `next_run_at = now` (immediately due) if this is the first time
    this account has ever been scheduled -- the documented "first
    scheduler start creates a due schedule; the first run can happen
    immediately" semantics (see `AutomationScheduleRecord`'s docstring's
    "No first-run backlog").

    Race-safe: a concurrent first-creation attempt from another process
    raises `IntegrityError` on `uq_automation_schedules_account_key`,
    caught and translated into reloading the winner's row -- the same
    INSERT + IntegrityError-catch idiom used throughout this project
    (e.g. `app.db.automation_repository`).
    """
    existing = get_schedule(db, account_key)
    if existing is not None:
        return existing

    effective_now = now if now is not None else datetime.now(UTC)
    record = AutomationScheduleRecord(
        account_key=account_key,
        next_run_at=effective_now,
        last_claimed_at=None,
        last_run_id=None,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_schedule(db, account_key)
        if existing is None:
            raise AutomationScheduleRepositoryConsistencyError(
                f"Could not create or observe an automation_schedules row for "
                f"account_key={account_key!r} after a UNIQUE constraint conflict."
            ) from None
        return existing
    db.refresh(record)
    return record


def claim_due_schedule(
    db: Session,
    account_key: str,
    *,
    interval_seconds: int,
    now: datetime | None = None,
) -> AutomationScheduleRecord | None:
    """Atomically claim the account's schedule slot if (and only if) it
    is currently due -- the sole arbitration point for "which scheduler
    process, if any, gets to trigger the next cycle right now".

    Returns the refreshed, already-advanced row if THIS call won the
    claim, or `None` if there was nothing to claim (no schedule row yet
    -- call `get_or_create_schedule` first; or the row exists but
    `next_run_at` is still in the future) or the claim was lost to a
    concurrent winner.

    The winning UPDATE moves `next_run_at` to `now + interval_seconds`
    -- COALESCING scheduling (see `AutomationScheduleRecord`'s
    docstring): a slot that was due hours/days ago (the worker was
    offline) is claimed exactly once and the next slot is scheduled
    relative to THIS claim moment, never replayed as a backlog of
    historical intervals.

    The `WHERE account_key = ... AND next_run_at = :observed AND
    next_run_at <= :now` predicate is a single atomic statement -- two
    concurrent callers racing the same due `next_run_at` value can never
    both match it (the winner's UPDATE changes the stored value in the
    same statement that reads/compares it), so at most one ever reports
    a non-None result for the same slot. Plain portable SQL -- no
    dialect-specific `WHERE` clause, no SELECT ... FOR UPDATE row lock --
    identical behavior on SQLite and PostgreSQL.
    """
    schedule = get_schedule(db, account_key)
    if schedule is None:
        return None

    effective_now = now if now is not None else datetime.now(UTC)
    observed_next_run_at = schedule.next_run_at
    if ensure_utc(observed_next_run_at) > effective_now:
        return None  # not due yet -- no point even attempting the UPDATE

    new_next_run_at = effective_now + timedelta(seconds=interval_seconds)
    result = db.execute(
        update(AutomationScheduleRecord)
        .where(
            AutomationScheduleRecord.account_key == account_key,
            AutomationScheduleRecord.next_run_at == observed_next_run_at,
            AutomationScheduleRecord.next_run_at <= effective_now,
        )
        .values(
            next_run_at=new_next_run_at,
            last_claimed_at=effective_now,
            updated_at=effective_now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:
        return None
    db.refresh(schedule)
    return schedule


def record_last_run(
    db: Session, account_key: str, *, run_id: int, now: datetime | None = None
) -> None:
    """Best-effort bookkeeping only -- records which `AutomationRunRecord`
    the most recent claimed slot actually triggered. Never part of the
    claim arbitration itself (`claim_due_schedule` already fully owns
    that): by the time this is called, the slot has already been won and
    `run_automation_cycle` has already returned, so there is nothing left
    to race for this account's schedule row.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    db.execute(
        update(AutomationScheduleRecord)
        .where(AutomationScheduleRecord.account_key == account_key)
        .values(last_run_id=run_id, updated_at=effective_now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
