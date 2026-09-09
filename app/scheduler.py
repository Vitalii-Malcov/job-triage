"""Stage 8B standalone scheduler worker entrypoint.

    python -m app.scheduler

Runs `app.services.automation.run_automation_cycle` automatically on a
configurable interval for exactly one configured account
(`AUTOMATION_SCHEDULER_ACCOUNT_KEY`), via its own dedicated polling loop
and its own SQLAlchemy `Session` per tick.

**Deliberately NOT started from `app.main`'s FastAPI lifespan.** A
production deployment can run multiple Uvicorn worker processes (or
multiple app instances behind a load balancer); each would otherwise
start its own independent timer loop, and every one of them would try to
trigger the same account's automation on the same schedule --
multiplying cycles instead of running exactly one. Running this as a
single, separate, explicitly-started process is what makes "one
scheduler for this account" an operational choice the deployer makes
(run exactly one instance of `python -m app.scheduler`), not an
accident of however many API worker processes happen to be configured.
The actual multi-process safety net -- `app.db.automation_schedule_repository.claim_due_schedule`'s
atomic CAS -- still holds even if more than one scheduler process is
ever accidentally started; it just means both instances are safe to
coexist, not that running more than one is the intended deployment
shape.

This module is intentionally thin: all real logic (configuration
validation, the claim, calling `run_automation_cycle`, outcome handling)
lives in `app.services.scheduler`/`app.db.automation_schedule_repository`.
This file only wires Settings + logging + a Session per tick + the
sleep/poll loop + clean shutdown on Ctrl+C together.
"""

import asyncio
import logging
import sys

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.scheduler import (
    SchedulerConfigurationError,
    run_due_cycle_if_claimed,
    run_due_digest_if_claimed,
    validate_scheduler_settings,
)
from app.services.telegram_digest import resolve_digest_account_key

logger = logging.getLogger(__name__)

# S8B-PRE-001: a fixed, generic message only -- `Settings` holds several
# credentials (Gmail app password, Telegram bot token, API keys, ...), and
# pydantic's ValidationError.__str__ can include the offending INPUT VALUE
# for other, unrelated fields that also failed validation in the same
# construction. `str(exc)`/`repr(exc)`/a traceback must never reach
# stdout/stderr or a log line -- only `type(exc).__name__` is safe (see
# each `except` branch in `main()` below).
_CONFIGURATION_ERROR_MESSAGE = "Automation scheduler configuration error. Check scheduler settings."


async def _poll_loop(settings) -> None:
    """Poll persisted schedule state every `automation_scheduler_poll_seconds`,
    opening and closing a fresh `Session` for each tick (never one
    long-lived Session held across the whole process lifetime -- a
    single tick's DB work should never be able to leak a connection or
    hold stale state into the next one). Runs until cancelled (Ctrl+C /
    KeyboardInterrupt via `asyncio.run`, see `main()`).
    """
    # Imported lazily (not at module load time): app.db.session builds a
    # real SQLAlchemy engine + Settings() at ITS OWN import time. Deferring
    # this import until AFTER main() has already validated the scheduler's
    # configuration keeps a misconfigured Settings() (e.g. this stage's own
    # "enabled but blank account_key" rule) surfacing as main()'s clean,
    # sanitized error message instead of an import-time traceback.
    from app.db.session import SessionLocal

    account_key = settings.automation_scheduler_account_key
    poll_seconds = settings.automation_scheduler_poll_seconds
    # AUD-010: account_key is an operator's real account identity
    # (derived from GMAIL_USERNAME/AUTOMATION_SCHEDULER_ACCOUNT_KEY, an
    # email address in practice -- see app.core.config's own account_key
    # docstrings) -- it must never be written to startup logs, mirroring
    # every other account_key-privacy fix already applied to this
    # module's per-tick digest logging (see run_due_digest_if_claimed's
    # own docstring). Only non-identifying operational config is logged.
    logger.info(
        "automation_scheduler_started interval_seconds=%s poll_seconds=%s "
        "automation_enabled=%s digest_enabled=%s",
        settings.automation_scheduler_interval_seconds,
        poll_seconds,
        settings.automation_scheduler_enabled,
        settings.telegram_daily_digest_enabled,
    )
    while True:
        db = SessionLocal()
        try:
            # S8E: the automation cycle and the daily Telegram digest are
            # independent features, each gated on its OWN settings flag
            # -- one being enabled must never imply or require the
            # other (see app.services.scheduler's module docstring). Two
            # separate try/except blocks so a failure in one can never
            # prevent the other from being attempted this same tick.
            if settings.automation_scheduler_enabled:
                try:
                    await run_due_cycle_if_claimed(db, account_key=account_key, settings=settings)
                except Exception as exc:
                    # A tick-level failure (e.g. a DB connectivity blip)
                    # must never kill the whole worker process -- log
                    # sanitized only (never the raw exception
                    # text/traceback, mirroring app.services.scheduler's
                    # own convention) and keep polling.
                    db.rollback()
                    logger.warning(
                        "automation_scheduler_poll_iteration_error error_type=%s",
                        type(exc).__name__,
                    )
            if settings.telegram_daily_digest_enabled:
                try:
                    # S8E ACCOUNT SCOPE (Codex finding): resolved through
                    # the SAME shared helper the manual /digest command
                    # uses (app.services.telegram_digest.cmd_digest),
                    # never re-derived here -- see
                    # resolve_digest_account_key's own docstring. Does
                    # NOT affect `account_key` above, which remains
                    # Stage 8B's own unchanged automation-cycle identity.
                    digest_account_key = resolve_digest_account_key(settings)
                    await run_due_digest_if_claimed(
                        db, account_key=digest_account_key, settings=settings
                    )
                except Exception as exc:
                    db.rollback()
                    logger.warning(
                        "telegram_daily_digest_poll_iteration_error error_type=%s",
                        type(exc).__name__,
                    )
        finally:
            db.close()
        await asyncio.sleep(poll_seconds)


def main() -> int:
    configure_logging()
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Covers this stage's own "enabled but blank account_key" rule
        # (app.core.config.Settings's model_validator) as well as any
        # other Settings misconfiguration -- fail closed with a clear,
        # SANITIZED message here rather than an unhandled traceback.
        # S8B-PRE-001: never print/log str(exc)/repr(exc) -- a
        # ValidationError's own message can embed the offending input
        # value for ANY field that failed validation in this same
        # Settings() construction, which may be a credential (Gmail app
        # password, Telegram bot token, an API key, ...), not just this
        # stage's own account_key.
        logger.error("automation_scheduler_configuration_error error_type=%s", type(exc).__name__)
        print(_CONFIGURATION_ERROR_MESSAGE, file=sys.stderr)
        return 1

    # S8E: the worker starts its poll loop if EITHER the automation
    # cycle OR the daily Telegram digest is enabled -- neither implies
    # or requires the other (see app.services.scheduler's module
    # docstring). _poll_loop itself gates each independently per tick,
    # so a worker started for "digest only" never triggers an
    # automation cycle, and vice versa.
    if not settings.automation_scheduler_enabled and not settings.telegram_daily_digest_enabled:
        logger.info("automation_scheduler_disabled")
        print(
            "Automation scheduler and Telegram daily digest are both disabled "
            "(AUTOMATION_SCHEDULER_ENABLED=false, TELEGRAM_DAILY_DIGEST_ENABLED=false) "
            "-- exiting without starting a poll loop."
        )
        return 0

    try:
        validate_scheduler_settings(settings)
    except SchedulerConfigurationError as exc:
        # S8B-PRE-001 (defense-in-depth): this exception's own message is
        # currently a fixed string with no user input embedded, but never
        # print str(exc) here either -- keeps this branch safe even if a
        # future SchedulerConfigurationError ever echoes caller-provided
        # detail, matching the ValidationError branch above exactly.
        logger.error("automation_scheduler_configuration_error error_type=%s", type(exc).__name__)
        print(_CONFIGURATION_ERROR_MESSAGE, file=sys.stderr)
        return 1

    try:
        asyncio.run(_poll_loop(settings))
    except KeyboardInterrupt:
        logger.info("automation_scheduler_stopped_keyboard_interrupt")
        print("Automation scheduler stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
