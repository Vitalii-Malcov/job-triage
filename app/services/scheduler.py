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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db.automation_schedule_repository import (
    claim_due_schedule,
    get_or_create_schedule,
    record_last_run,
)
from app.db.telegram_digest_repository import (
    claim_delivery,
    mark_failed,
    mark_sent,
    mark_uncertain,
    retry_delivery,
)
from app.services.automation import (
    AutomationRunAlreadyInProgressError,
    AutomationRunLeaseLostError,
    run_automation_cycle,
)
from app.services.telegram import TelegramSendOutcome, send_telegram_text
from app.services.telegram_digest import build_digest_text

logger = logging.getLogger(__name__)

__all__ = [
    "SchedulerConfigurationError",
    "run_due_cycle_if_claimed",
    "run_due_digest_if_claimed",
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
    poll loop. Both the automation cycle and the Stage 8E daily digest
    are validated INDEPENDENTLY -- neither being enabled is itself
    invalid (the worker simply does not start at all in that case; see
    `app.scheduler.main`), and either one can be enabled without the
    other. `app.core.config.Settings` itself already enforces the same
    "enabled requires a non-blank account_key" rules at construction
    time (`Settings._validate_scheduler_requires_account_key_when_enabled`/
    `_validate_daily_digest_requires_account_key_when_enabled`), so in
    practice the account_key checks below only ever re-confirm what
    `get_settings()` already guaranteed; they exist as their own
    explicit step so the worker's fail-closed behavior does not depend
    on those Settings-level validators never changing.
    """
    scheduler_account_key = settings.automation_scheduler_account_key.strip()
    if settings.automation_scheduler_enabled and not scheduler_account_key:
        raise SchedulerConfigurationError(
            "automation_scheduler_enabled=True requires a non-blank "
            "automation_scheduler_account_key (AUTOMATION_SCHEDULER_ACCOUNT_KEY)."
        )

    if settings.telegram_daily_digest_enabled:
        if not scheduler_account_key:
            raise SchedulerConfigurationError(
                "telegram_daily_digest_enabled=True requires a non-blank "
                "automation_scheduler_account_key (AUTOMATION_SCHEDULER_ACCOUNT_KEY)."
            )
        if not settings.telegram_bot_token.strip() or not settings.telegram_chat_id.strip():
            raise SchedulerConfigurationError(
                "telegram_daily_digest_enabled=True requires TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID to be configured."
            )
        try:
            ZoneInfo(settings.telegram_daily_digest_timezone)
        except ZoneInfoNotFoundError:
            raise SchedulerConfigurationError(
                "telegram_daily_digest_enabled=True requires a valid IANA "
                "TELEGRAM_DAILY_DIGEST_TIMEZONE."
            ) from None


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


def _local_now(timezone_name: str, now: datetime | None = None) -> datetime:
    """The current time in `timezone_name` -- `now` is a UTC-aware
    override for tests (never used in production, where the default
    `datetime.now(UTC)` is always the real clock); a separate helper so
    tests can inject a fixed instant without monkeypatching `datetime`
    itself.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    return effective_now.astimezone(ZoneInfo(timezone_name))


async def run_due_digest_if_claimed(
    db: Session, *, account_key: str, settings, now: datetime | None = None
) -> bool:
    """One polling tick's worth of work for the Stage 8E optional DAILY
    Telegram digest -- entirely independent of
    `run_due_cycle_if_claimed`/the automation cycle (see this module's
    docstring: either may be enabled without the other; the standalone
    worker's poll loop, `app.scheduler._poll_loop`, calls each
    independently, gated on its OWN settings flag).

    Computes the current LOCAL date in
    `settings.telegram_daily_digest_timezone`, gates on
    `settings.telegram_daily_digest_hour` having already passed for that
    date, and -- only if so -- attempts the once-per-`(account_key,
    digest_date)` claim (`app.db.telegram_digest_repository.claim_delivery`)
    that is the actual duplicate-send guard (see
    `TelegramDigestDeliveryRecord`'s docstring for the full CAS
    rationale: a `FAILED` delivery may be retried on a LATER tick of the
    SAME still-current date; a `SENT` or `UNCERTAIN` one never is).

    Returns `True` if a send was attempted this tick (any outcome --
    SENT/FAILED/UNCERTAIN all count as "attempted"), `False` if nothing
    was due yet, or the claim/retry was lost to a concurrent claimer, or
    today's digest already resolved SENT/UNCERTAIN. Never raises -- a
    truly unexpected failure is caught, sanitized, recorded as
    `UNCERTAIN` (never automatically retried -- the safest assumption
    when the failure mode itself is unknown, see the model docstring),
    and absorbed, exactly mirroring `run_due_cycle_if_claimed`'s own
    fail-closed, no-retry-storm contract.
    """
    try:
        local_now = _local_now(settings.telegram_daily_digest_timezone, now)
    except Exception as exc:
        # An invalid timezone string reaching this far (bypassing both
        # Settings construction and validate_scheduler_settings's own
        # startup check) must still fail closed per-tick, not crash the
        # worker.
        logger.warning("telegram_daily_digest_invalid_timezone error_type=%s", type(exc).__name__)
        return False

    if local_now.hour < settings.telegram_daily_digest_hour:
        return False  # not due yet today

    digest_date = local_now.date()
    record, claimed = claim_delivery(db, account_key, digest_date)
    if not claimed:
        if record.status != "FAILED":
            # SENT (already delivered today) / UNCERTAIN (never
            # automatically retried) / PENDING (held by a concurrent
            # claimer or attempt) -- nothing for THIS tick to do.
            return False
        if not retry_delivery(db, record):
            return False  # lost the retry race to a concurrent claimer

    try:
        text = build_digest_text(db, account_key)
        outcome = await send_telegram_text(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            text,
            timeout_seconds=settings.telegram_timeout_seconds,
        )
    except Exception as exc:
        # Truly unexpected (e.g. a bug in build_digest_text, not a
        # Telegram/network failure -- send_telegram_text already catches
        # and classifies every httpx exception itself). Whether the
        # message actually reached Telegram is genuinely unknown here,
        # so this is recorded UNCERTAIN, never FAILED -- never
        # automatically retried, to avoid risking a duplicate send.
        db.rollback()
        logger.warning(
            "telegram_daily_digest_unexpected_error account_key=%s error_type=%s",
            account_key,
            type(exc).__name__,
        )
        mark_uncertain(db, record, last_error=type(exc).__name__)
        return True

    if outcome is TelegramSendOutcome.SENT:
        mark_sent(db, record)
        logger.info(
            "telegram_daily_digest_sent account_key=%s digest_date=%s", account_key, digest_date
        )
    elif outcome is TelegramSendOutcome.FAILED:
        mark_failed(db, record, last_error=outcome.value)
        logger.warning(
            "telegram_daily_digest_failed account_key=%s digest_date=%s", account_key, digest_date
        )
    else:
        mark_uncertain(db, record, last_error=outcome.value)
        logger.warning(
            "telegram_daily_digest_uncertain account_key=%s digest_date=%s",
            account_key,
            digest_date,
        )

    return True
