"""Persistence for Stage 8D `AutomationMailProgressRecord` -- one row per
account tracking Gmail response-draft cycle and follow-up proposal cycle
progress, plus the atomic CAS primitives (`advance_gmail_cursor`/
`advance_follow_up_cursor`) that arbitrate "may THIS caller advance
progress, or has a newer owner already moved past it". Mirrors
`app.db.automation_schedule_repository`'s own INSERT + IntegrityError
-catch idiom for the initial row creation, and its plain conditional
`UPDATE ... WHERE <observed value> ...` idiom for the CAS itself -- see
`app.db.models.AutomationMailProgressRecord`'s docstring for the full
concurrency/crash-safety rationale.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AutomationMailProgressRecord


class AutomationMailProgressConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway -- e.g. reloading the row a UNIQUE constraint
    IntegrityError implies must exist comes back None. Mirrors
    app.db.automation_schedule_repository.AutomationScheduleRepositoryConsistencyError.
    """


def get_mail_progress(db: Session, account_key: str) -> AutomationMailProgressRecord | None:
    """The account's progress row, if one has ever been created. A plain
    read -- callers that need "create if missing" use
    `get_or_create_mail_progress` instead.
    """
    return db.scalar(
        select(AutomationMailProgressRecord).where(
            AutomationMailProgressRecord.account_key == account_key
        )
    )


def get_or_create_mail_progress(db: Session, account_key: str) -> AutomationMailProgressRecord:
    """Return the account's existing progress row, or create one with
    both cursors NULL (a brand-new installation starts the Gmail scan
    from the oldest stored message and the follow-up scan from the
    oldest currently-APPLIED job -- see
    `AutomationMailProgressRecord`'s own column comments).

    Race-safe: a concurrent first-creation attempt from another process
    raises `IntegrityError` on `uq_automation_mail_progress_account_key`,
    caught and translated into reloading the winner's row -- the same
    INSERT + IntegrityError-catch idiom used throughout this project.
    """
    existing = get_mail_progress(db, account_key)
    if existing is not None:
        return existing

    record = AutomationMailProgressRecord(
        account_key=account_key,
        gmail_after_message_id=None,
        follow_up_after_job_id=None,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_mail_progress(db, account_key)
        if existing is None:
            raise AutomationMailProgressConsistencyError(
                f"Could not create or observe an automation_mail_progress row for "
                f"account_key={account_key!r} after a UNIQUE constraint conflict."
            ) from None
        return existing
    db.refresh(record)
    return record


def _cas_advance(
    db: Session,
    account_key: str,
    *,
    column,
    expected_cursor: int | None,
    new_cursor: int | None,
) -> bool:
    """Shared CAS primitive for both cursor columns: a single atomic
    `UPDATE ... WHERE account_key = :account_key AND <column> IS/=
    :expected_cursor` -- exactly like `claim_due_schedule`'s CAS. `NULL`
    is handled explicitly (plain SQL `= NULL` never matches, unlike
    Python's `is None`), so an `expected_cursor=None` genuinely requires
    the STORED value to still be NULL, not merely absent from the
    WHERE clause. Returns True iff this call's UPDATE matched exactly one
    row (i.e. genuinely owned/advanced the cursor); False means a
    concurrent/newer owner already changed it first -- the caller must
    fail closed, never retry-and-overwrite.
    """
    condition = column.is_(None) if expected_cursor is None else column == expected_cursor
    result = db.execute(
        update(AutomationMailProgressRecord)
        .where(AutomationMailProgressRecord.account_key == account_key, condition)
        .values(**{column.key: new_cursor}, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def advance_gmail_cursor(
    db: Session, account_key: str, *, expected_cursor: int | None, new_cursor: int
) -> bool:
    """CAS-advance `gmail_after_message_id`. Only ever moves forward
    (never reset to NULL -- see `AutomationMailProgressRecord`'s own
    column comment); the caller (`app.services.automation_gmail`)
    supplies `expected_cursor` as whatever it last observed/advanced to
    within THIS run, so a mismatch means another process has since
    claimed ownership of this account's progress -- fail closed rather
    than silently overwrite a newer owner's state.
    """
    return _cas_advance(
        db,
        account_key,
        column=AutomationMailProgressRecord.gmail_after_message_id,
        expected_cursor=expected_cursor,
        new_cursor=new_cursor,
    )


def advance_follow_up_cursor(
    db: Session, account_key: str, *, expected_cursor: int | None, new_cursor: int | None
) -> bool:
    """CAS-advance (or, with `new_cursor=None`, wrap-reset)
    `follow_up_after_job_id`. Unlike `advance_gmail_cursor`,
    `new_cursor` MAY be `None` -- the round-robin wrap-around
    (`app.services.automation_follow_up`) resetting the cursor back to
    the start of the currently-APPLIED job list once a full pass reaches
    the end.
    """
    return _cas_advance(
        db,
        account_key,
        column=AutomationMailProgressRecord.follow_up_after_job_id,
        expected_cursor=expected_cursor,
        new_cursor=new_cursor,
    )
