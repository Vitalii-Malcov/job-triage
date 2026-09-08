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
    validate_scheduler_settings,
)

logger = logging.getLogger(__name__)


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
    logger.info(
        "automation_scheduler_started account_key=%s interval_seconds=%s poll_seconds=%s",
        account_key,
        settings.automation_scheduler_interval_seconds,
        poll_seconds,
    )
    while True:
        db = SessionLocal()
        try:
            await run_due_cycle_if_claimed(db, account_key=account_key, settings=settings)
        except Exception as exc:
            # A tick-level failure (e.g. a DB connectivity blip) must
            # never kill the whole worker process -- log sanitized only
            # (never the raw exception text/traceback, mirroring
            # app.services.scheduler's own convention) and keep polling.
            db.rollback()
            logger.warning(
                "automation_scheduler_poll_iteration_error error_type=%s",
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
        # other Settings misconfiguration -- fail closed with a clear
        # message here rather than an unhandled traceback.
        logger.error("automation_scheduler_configuration_error error_type=%s", type(exc).__name__)
        print(f"Automation scheduler configuration error: {exc}", file=sys.stderr)
        return 1

    if not settings.automation_scheduler_enabled:
        logger.info("automation_scheduler_disabled")
        print(
            "Automation scheduler is disabled (AUTOMATION_SCHEDULER_ENABLED=false) "
            "-- exiting without starting a poll loop."
        )
        return 0

    try:
        validate_scheduler_settings(settings)
    except SchedulerConfigurationError as exc:
        logger.error("automation_scheduler_configuration_error error_type=%s", type(exc).__name__)
        print(f"Automation scheduler configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        asyncio.run(_poll_loop(settings))
    except KeyboardInterrupt:
        logger.info("automation_scheduler_stopped_keyboard_interrupt")
        print("Automation scheduler stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
