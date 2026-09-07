"""Persistence for Stage 8A `AutomationRunRecord` — account-scoped
create/read/finish. Mirrors app.db.follow_up_approval_repository's
INSERT + IntegrityError-catch idiom for the exclusivity claim (see
`create_running_run`), and app.db.follow_up_repository's plain
account-scoped reads/list for everything else.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AutomationRunRecord
from app.models.automation import AutomationRun, AutomationRunStepResult

AUTOMATION_RUN_LIST_DEFAULT_LIMIT = 20
AUTOMATION_RUN_LIST_MAX_LIMIT = 100


class AutomationRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — e.g. reloading the row a UNIQUE constraint
    IntegrityError implies must exist comes back None. Mirrors
    app.db.follow_up_repository.FollowUpRepositoryConsistencyError.
    """


def get_running_run_for_account(db: Session, account_key: str) -> AutomationRunRecord | None:
    """The account's current RUNNING row, if any — a plain read; the
    PARTIAL unique index (`uq_automation_runs_one_running_per_account`)
    is the actual arbiter of "at most one", not this query.
    """
    return db.scalar(
        select(AutomationRunRecord).where(
            AutomationRunRecord.account_key == account_key,
            AutomationRunRecord.status == "RUNNING",
        )
    )


def create_running_run(db: Session, *, account_key: str) -> tuple[AutomationRunRecord, bool]:
    """Insert-only claim of the RUNNING slot for `account_key`. Returns
    `(record, created)` — `created=False` means a RUNNING run for this
    account ALREADY existed (the caller,
    app.services.automation.run_automation_cycle, raises
    `AutomationRunAlreadyInProgressError` in that case; this function
    itself never raises for the expected "already running" case).

    Concurrency: two concurrent requests for the SAME account racing to
    start a run can never both win — the loser's INSERT fails on
    `uq_automation_runs_one_running_per_account` (a real DB constraint,
    not a Python check-then-act read), caught below and resolved by
    re-reading the winner's row rather than raising or double-inserting
    — same idiom as
    app.db.follow_up_approval_repository.claim_send_attempt.
    """
    record = AutomationRunRecord(
        account_key=account_key,
        status="RUNNING",
        started_at=datetime.now(UTC),
        results_json="{}",
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_running_run_for_account(db, account_key)
        if existing is None:
            raise AutomationRepositoryConsistencyError(
                f"Expected an automation_runs row RUNNING for account_key={account_key!r} "
                "after a UNIQUE constraint collision, but none was found."
            ) from None
        return existing, False

    db.refresh(record)
    return record, True


def finish_run(
    db: Session,
    record: AutomationRunRecord,
    *,
    status: str,
    results: dict[str, dict],
    error_summary: str | None,
) -> AutomationRunRecord:
    """Transition a RUNNING row to its terminal status. A plain update —
    unlike Stage 7E's send-record CAS transitions, no other writer ever
    contends for this exact row after `create_running_run` returns it to
    its one caller, so no additional compare-and-swap is needed here.
    """
    record.status = status
    record.finished_at = datetime.now(UTC)
    record.results_json = json.dumps(results)
    record.error_summary = error_summary
    db.commit()
    db.refresh(record)
    return record


def get_run_by_id(db: Session, account_key: str, run_id: int) -> AutomationRunRecord | None:
    return db.scalar(
        select(AutomationRunRecord).where(
            AutomationRunRecord.id == run_id,
            AutomationRunRecord.account_key == account_key,
        )
    )


def list_runs(
    db: Session,
    account_key: str,
    limit: int = AUTOMATION_RUN_LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> list[AutomationRunRecord]:
    stmt = (
        select(AutomationRunRecord)
        .where(AutomationRunRecord.account_key == account_key)
        .order_by(AutomationRunRecord.started_at.desc(), AutomationRunRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def to_automation_run(record: AutomationRunRecord) -> AutomationRun:
    raw_results = json.loads(record.results_json)
    return AutomationRun(
        id=record.id,
        account_key=record.account_key,
        status=record.status,
        started_at=record.started_at,
        finished_at=record.finished_at,
        results={
            step_name: AutomationRunStepResult(**step_result)
            for step_name, step_result in raw_results.items()
        },
        error_summary=record.error_summary,
        created_at=record.created_at,
    )
