"""Stage 8B: coalescing periodic trigger for the EXISTING Stage 8A
`app.services.automation.run_automation_cycle` orchestrator -- runs it
on a configurable interval via a separate, standalone worker process
(`python -m app.scheduler`), never embedded in FastAPI's own
process/lifespan (see `app/scheduler.py`'s module docstring for why:
multiple Uvicorn workers/processes must not each run their own
independent timer loop).

**Reuse, not duplication.** This module adds ZERO new
collection/scoring/dedup/run-bookkeeping logic -- `run_due_cycle_if_claimed`
does exactly two things: (1) atomically claim a due schedule slot via
`app.db.automation_schedule_repository.claim_due_schedule`, and (2), only
if that claim was won, call the SAME, UNCHANGED
`app.services.automation.run_automation_cycle` that
`POST /automation/runs` already calls. Stage 8A's own concurrency
protection (per-account RUNNING lease), collector orchestration, and
"no application/email auto-send" guarantee are reused exactly, not
reimplemented.

**No new side effects.** This module never sends email, never submits an
application, never approves anything, and never talks to Telegram
directly -- any Telegram notification that happens is entirely
`run_automation_cycle`'s own EXISTING, unchanged behavior. There is no
retry storm on any outcome: `AutomationRunAlreadyInProgressError` and
`AutomationRunLeaseLostError` are both logged (sanitized) and left for
the next normal polling interval, exactly like an unexpected exception
(see each branch below) -- see this module's own docstring section
"Fail-closed slot claim" in the Stage 8B spec for the accepted
crash-between-claim-and-run tradeoff.
"""

import logging

from sqlalchemy.orm import Session

from app.db.automation_schedule_repository import (
    claim_due_schedule,
    get_or_create_schedule,
    record_last_run,
)
from app.services.automation import (
    AutomationRunAlreadyInProgressError,
    AutomationRunLeaseLostError,
    run_automation_cycle,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SchedulerConfigurationError",
    "run_due_cycle_if_claimed",
    "validate_scheduler_settings",
]


class SchedulerConfigurationError(Exception):
    """Raised when the scheduler is enabled but misconfigured -- fail
    closed rather than silently no-op, guess an account_key, or start
    polling with nothing meaningful to schedule. Mirrors this project's
    other "unset required config -> fail closed" conventions (e.g.
    collectors' `CollectorNotConfiguredError`), except this one is
    raised at worker STARTUP, before any polling loop begins, since an
    unconfigured scheduler can never become valid mid-run.
    """


def validate_scheduler_settings(settings) -> None:
    """Fail-closed configuration check for the standalone worker
    (`app/scheduler.py`) to run once at startup, before entering its
    poll loop. A disabled scheduler is always valid (nothing to
    validate) -- `app.core.config.Settings` itself already enforces the
    same "enabled requires a non-blank account_key" rule at construction
    time (`Settings._validate_scheduler_requires_account_key_when_enabled`),
    so in practice this only ever re-confirms what `get_settings()`
    already guaranteed; it exists as its own explicit step so the
    worker's fail-closed behavior does not depend on that Settings-level
    validator never changing.
    """
    if not settings.automation_scheduler_enabled:
        return
    if not settings.automation_scheduler_account_key.strip():
        raise SchedulerConfigurationError(
            "automation_scheduler_enabled=True requires a non-blank "
            "automation_scheduler_account_key (AUTOMATION_SCHEDULER_ACCOUNT_KEY)."
        )


async def run_due_cycle_if_claimed(db: Session, *, account_key: str, settings) -> bool:
    """One polling tick's worth of work. Ensures the account's schedule
    row exists, attempts to atomically claim a due slot, and -- only if
    that claim was won -- calls the EXISTING `run_automation_cycle(...)`
    exactly once with THIS `db` Session.

    Returns `True` if a cycle was triggered (regardless of its outcome --
    success, "already in progress", lease lost, or an unexpected
    exception all still count as "a cycle was attempted this tick") and
    `False` if nothing was due or the claim was lost to a concurrent
    claimer. The caller (`app.scheduler`'s poll loop) does not need to
    distinguish further -- either way, it simply waits for the next
    normal polling iteration; see the module docstring for why none of
    the failure branches below trigger an immediate retry.
    """
    get_or_create_schedule(db, account_key)
    claimed = claim_due_schedule(
        db, account_key, interval_seconds=settings.automation_scheduler_interval_seconds
    )
    if claimed is None:
        return False

    try:
        run = await run_automation_cycle(db, account_key=account_key, settings=settings)
    except AutomationRunAlreadyInProgressError:
        # A RUNNING run for this account already exists (e.g. a manual
        # POST /automation/runs, or the previous scheduled cycle is
        # still executing past this interval). Do not retry immediately
        # or queue -- the schedule slot has already been advanced by
        # claim_due_schedule above; the account is simply re-evaluated
        # on the next normal interval.
        logger.info("automation_scheduler_run_already_in_progress account_key=%s", account_key)
        return True
    except AutomationRunLeaseLostError:
        # Fail closed -- run_automation_cycle itself could not safely
        # finalize this run's outcome (another process may now own its
        # lease). No immediate retry storm; wait for the next normal
        # interval.
        logger.warning("automation_scheduler_run_lease_lost account_key=%s", account_key)
        return True
    except Exception as exc:
        # Any truly unexpected failure -- never let it kill the poll
        # loop, and never log/persist the raw exception text or a
        # traceback (only its type), mirroring
        # app.services.automation._run_step's identical sanitized-logging
        # convention (S8A-004).
        db.rollback()
        logger.warning(
            "automation_scheduler_unexpected_error account_key=%s error_type=%s",
            account_key,
            type(exc).__name__,
        )
        return True

    # Best-effort bookkeeping only (mirrors this project's "external
    # notification failures must not fail core persistence" convention,
    # CLAUDE.md's Implementation rules) -- run_automation_cycle has
    # already finished and returned a real, finalized AutomationRunRecord
    # at this point; a failure recording the schedule's last_run_id
    # pointer must never be treated the same as the run itself failing,
    # and must never kill this polling iteration either.
    try:
        record_last_run(db, account_key, run_id=run.id)
    except Exception as exc:
        db.rollback()
        logger.warning(
            "automation_scheduler_record_last_run_failed account_key=%s run_id=%s error_type=%s",
            account_key,
            run.id,
            type(exc).__name__,
        )

    logger.info(
        "automation_scheduler_run_triggered account_key=%s run_id=%s status=%s",
        account_key,
        run.id,
        run.status,
    )
    return True
