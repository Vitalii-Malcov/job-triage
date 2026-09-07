"""Persistence for Stage 8A `AutomationRunRecord` — account-scoped
create/read/finish, plus the S8A-002 ownership-aware lease primitive
(`lease_holder`/`lease_expires_at`) that makes crash recovery possible.
Mirrors app.db.gmail_repository's per-Gmail-thread lock
(`acquire_thread_lock`/`renew_thread_lock`/`release_thread_lock`) almost
exactly — see that module's docstrings for the same rationale applied
here — plus app.db.follow_up_approval_repository's INSERT +
IntegrityError-catch idiom for the initial exclusivity claim.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AutomationRunRecord
from app.models.automation import AutomationRun, AutomationRunStepResult

AUTOMATION_RUN_LIST_DEFAULT_LIMIT = 20
AUTOMATION_RUN_LIST_MAX_LIMIT = 100

# S8A-002: mirrors app.db.gmail_repository.THREAD_LOCK_TTL_SECONDS's own
# rationale — bounds how long a crashed holder (a process that died
# mid-run, or whose heartbeat otherwise stopped) can block new
# automation runs for that account. A live holder's heartbeat renews
# well before this elapses (see app.services.automation's
# _RunLeaseHeartbeat), so this is a crash-recovery bound, not a normal
# operating constraint.
AUTOMATION_RUN_LEASE_TTL_SECONDS = 30.0


class AutomationRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — e.g. reloading the row a UNIQUE constraint
    IntegrityError implies must exist comes back None. Mirrors
    app.db.follow_up_repository.FollowUpRepositoryConsistencyError.
    """


def new_run_lease_holder_token(prefix: str) -> str:
    """A per-attempt-unique holder identity — mirrors
    app.db.gmail_repository.new_thread_lock_holder_token exactly, same
    rationale: never reused across different logical attempts, so a
    stale/expired lease's own former holder string can never be mistaken
    for a currently-live one by coincidence.
    """
    return f"{prefix}:{uuid.uuid4().hex}"


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


def _ensure_utc(value: datetime) -> datetime:
    """SQLite (unlike Postgres) doesn't preserve tzinfo through a
    `DateTime(timezone=True)` round-trip — a value stored as UTC comes
    back naive, which would otherwise raise `TypeError: can't compare
    offset-naive and offset-aware datetimes` when compared against a
    tz-aware `now` in pure Python (as opposed to inside a SQL WHERE
    clause, which compares as text and is unaffected). Every
    `lease_expires_at` this module ever writes is UTC (see
    `_insert_running_run`/`renew_run_lease`), so a naive read is always
    safe to reattach as UTC — mirrors
    app.db.follow_up_repository._ensure_utc exactly.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_lease_expired(record: AutomationRunRecord, now: datetime) -> bool:
    return record.lease_expires_at is None or _ensure_utc(record.lease_expires_at) < now


def _insert_running_run(
    db: Session, *, account_key: str, holder: str, ttl_seconds: float
) -> tuple[AutomationRunRecord, bool] | None:
    """One raw INSERT attempt. Returns `(record, True)` on success, or
    `None` if it lost the race (IntegrityError) — the caller decides what
    to do with an existing row; this helper never reads or reconciles.
    """
    now = datetime.now(UTC)
    record = AutomationRunRecord(
        account_key=account_key,
        status="RUNNING",
        started_at=now,
        results_json="{}",
        lease_holder=holder,
        lease_expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(record)
    return record, True


def reconcile_stale_run_to_failed(db: Session, record: AutomationRunRecord) -> bool:
    """S8A-002: CAS `RUNNING -> FAILED` for a run whose lease has
    EXPIRED — the crash-recovery counterpart to Stage 7E's expired-lock
    reclaim, except here the reclaim itself is an explicit, auditable
    state transition (a stale RUNNING row becomes a terminal FAILED one
    with an honest `error_summary`) rather than a silent handover, since
    `AutomationRunRecord` unlike `GmailThreadRecord` is itself the
    user-visible audit record of what happened.

    The WHERE clause requires `status = 'RUNNING' AND lease_expires_at <
    now` at the moment of the UPDATE — never blindly steals a live lease,
    and if two concurrent requests both observe the same stale row, only
    ONE of their UPDATEs actually matches (the loser's `now` may differ
    by microseconds, but the row's `status` changes to `'FAILED'` after
    the winner's commit, so the loser's own WHERE clause no longer
    matches on `status = 'RUNNING'` regardless of timing). Returns True
    only if this call won that CAS.
    """
    now = datetime.now(UTC)
    result = db.execute(
        update(AutomationRunRecord)
        .where(
            AutomationRunRecord.id == record.id,
            AutomationRunRecord.status == "RUNNING",
            AutomationRunRecord.lease_expires_at < now,
        )
        .values(
            status="FAILED",
            finished_at=now,
            error_summary=(
                "Reconciled: the previous holder's lease expired without "
                "finishing or renewing (the process likely crashed)."
            ),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def create_running_run(
    db: Session,
    *,
    account_key: str,
    holder: str,
    ttl_seconds: float = AUTOMATION_RUN_LEASE_TTL_SECONDS,
) -> tuple[AutomationRunRecord, bool]:
    """Claim the RUNNING slot for `account_key` on behalf of `holder`.
    Returns `(record, created)` — `created=False` means a LIVE run for
    this account already exists (the caller,
    app.services.automation.run_automation_cycle, raises
    `AutomationRunAlreadyInProgressError` in that case).

    S8A-002 (Codex re-review, crash recovery): if the existing RUNNING
    row's lease has EXPIRED (its holder crashed or otherwise stopped
    renewing), this atomically reconciles it to `FAILED`
    (`reconcile_stale_run_to_failed`) and retries the insert — a crashed
    process can therefore never block an account's automation forever. A
    LIVE lease is never touched: `created=False` is returned immediately,
    exactly like before S8A-002. Bounded to a few attempts (each
    iteration makes real forward progress — either the insert succeeds,
    or reconciliation frees the slot for the very next attempt, or a
    concurrent request's OWN fresh claim is observed and reported as
    "already in progress") rather than looping unboundedly.
    """
    for _attempt in range(3):
        won = _insert_running_run(
            db, account_key=account_key, holder=holder, ttl_seconds=ttl_seconds
        )
        if won is not None:
            return won

        existing = get_running_run_for_account(db, account_key)
        if existing is None:
            # The row that caused our IntegrityError is already gone
            # (e.g. a concurrent request reconciled AND a third request's
            # insert raced past us too) — safe to just retry the insert.
            continue

        now = datetime.now(UTC)
        if not _is_lease_expired(existing, now):
            return existing, False

        # Stale — never blindly steal it; reconcile it as its own
        # explicit, auditable FAILED transition, then retry the insert.
        # If we LOSE the reconciliation CAS (someone else won it, or
        # already renewed it), loop back around and re-observe reality
        # fresh rather than assuming anything about who owns it now.
        reconcile_stale_run_to_failed(db, existing)

    # Exhausted retries — only reachable under sustained concurrent
    # contention on this exact account. Report the current, honest state
    # rather than looping forever.
    current = get_running_run_for_account(db, account_key)
    if current is not None:
        return current, False
    raise AutomationRepositoryConsistencyError(
        f"Could not claim or observe a RUNNING automation_runs row for "
        f"account_key={account_key!r} after repeated contention."
    )


def renew_run_lease(
    db: Session,
    run_id: int,
    *,
    holder: str,
    ttl_seconds: float = AUTOMATION_RUN_LEASE_TTL_SECONDS,
) -> bool:
    """S8A-002 (Codex re-review): the DEDICATED CAS a lease-renewal
    heartbeat (`app.services.automation._RunLeaseHeartbeat`) must use —
    mirrors app.db.gmail_repository.renew_thread_lock EXACTLY, including
    why it is NOT interchangeable with `create_running_run`'s claim path:
    that path treats an expired lease as reclaimable-by-anyone (correct
    for initial/recovery acquisition), which would let a renewal
    silently "succeed" on a lease that had already lapsed — masking a
    real ownership gap. This function's WHERE clause requires the lease
    to still be LIVE (`lease_expires_at >= now`) at the moment of
    renewal, on top of `lease_holder == holder` and `status = 'RUNNING'`
    — it fails the instant the lease has expired, even if no one else
    has claimed it yet. Returns True only if exactly one row was
    updated.
    """
    now = datetime.now(UTC)
    result = db.execute(
        update(AutomationRunRecord)
        .where(
            AutomationRunRecord.id == run_id,
            AutomationRunRecord.lease_holder == holder,
            AutomationRunRecord.status == "RUNNING",
            AutomationRunRecord.lease_expires_at >= now,
        )
        .values(lease_expires_at=now + timedelta(seconds=ttl_seconds))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def finish_run(
    db: Session,
    record: AutomationRunRecord,
    *,
    holder: str,
    status: str,
    results: dict[str, dict],
    error_summary: str | None,
) -> AutomationRunRecord | None:
    """Transition a RUNNING row to its terminal status — CONDITIONED on
    `holder` still owning it (S8A-002, Codex re-review). Returns the
    refreshed record on success, or `None` if `holder` no longer owns
    this row (the lease was lost — e.g. reconciled away as stale by
    another request while this one was still executing). Returning
    `None` rather than raising lets the caller
    (app.services.automation.run_automation_cycle) decide how to fail
    closed without this module needing to know about
    `AutomationRunLeaseLostError`.

    This is deliberately NOT unconditional (unlike a plain update would
    be): overwriting `status`/`results_json` on a row that some OTHER
    process now owns (because it reconciled this one as stale and
    started its own fresh run) would silently corrupt that other run's
    state — exactly what S8A-002 requires never happens ("do not
    overwrite another owner's state").
    """
    now = datetime.now(UTC)
    result = db.execute(
        update(AutomationRunRecord)
        .where(
            AutomationRunRecord.id == record.id,
            AutomationRunRecord.lease_holder == holder,
            AutomationRunRecord.status == "RUNNING",
        )
        .values(
            status=status,
            finished_at=now,
            results_json=json.dumps(results),
            error_summary=error_summary,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:
        return None
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
