"""Review finding 2.3: one mutual-exclusion rule for every collector entry point.

The automation lease (`AutomationRunRecord`: one RUNNING row per account key, a
lease with a heartbeat, crash recovery by expiry) used to protect automation
cycles only. Manual runs (`POST /collectors/*/run`, Telegram `/run`) called the
collectors with no lock, so two runs could work the same IMAP mailbox and the
same XING scan-progress row at once, or overlap an automation cycle.

`run_collector_exclusively` makes a manual run claim the SAME RUNNING slot an
automation cycle claims, so manual-vs-manual, manual-vs-automation and
automation-vs-manual all exclude each other, across processes, with the same
crash recovery. `run_automation_cycle` is unchanged: it already holds the slot
and calls the collectors directly (never through this wrapper).

Covered entry points: REST collector endpoints, Telegram `/run`, automation
cycles (scheduler and REST) and the operator CLI that changes vacancy data
(`scripts/stage22_refresh_ba.py --apply`, through `hold_collector_slots`; its own
file lock stays as an additional process-local guard). Automation cycles claim ONE
key, so after claiming they verify that no live run holds the other actual key
(`app.services.automation._yield_to_live_run_under_another_key`).

Account keys. The REST automation endpoint scopes a cycle by the normalized
`GMAIL_USERNAME`, the scheduler by `AUTOMATION_SCHEDULER_ACCOUNT_KEY` exactly as
configured (case preserved). The collectors themselves are global (one XING
mailbox), so a manual run claims the slot for EVERY distinct ACTUAL key of those
two (sorted, so two contenders cannot deadlock); the REST key is kept even when
Gmail is unset (the empty key).

Ownership contract:
- the configuration is checked BEFORE a slot is taken (a not-configured
  collector never touches the table);
- a heartbeat renews each lease on its own session while the collector runs;
- on normal return or on an exception the request session is rolled back first,
  then each run is finalized by the holder/unexpired-lease CAS
  (`finish_run`); a lost lease is reported as `CollectorRunLeaseLostError`,
  never as success;
- on cancellation the collector's worker thread (XING's IMAP fetch runs in
  `asyncio.to_thread`) may still be using the request session, so nothing is
  finalized and the session is not touched: the leases are extended once to
  cover the IMAP deadline and then left to expire (fail closed -- the slot
  stays RUNNING until the thread must have ended, then crash recovery
  reconciles it).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import CollectorNotConfiguredError
from app.db.automation_repository import (
    AUTOMATION_RUN_LEASE_TTL_SECONDS,
    MANUAL_HOLDER_PREFIX,
    create_running_run,
    finish_run,
    new_run_lease_holder_token,
    renew_run_lease,
)
from app.db.models import AutomationRunRecord
from app.providers.email.base import normalize_account_key
from app.providers.email.imap_deadline import IMAP_SESSION_DEADLINE_SECONDS
from app.services.automation import _RunLeaseHeartbeat
from app.services.offload import run_off_loop
from app.utils.config_flags import is_configured

logger = logging.getLogger(__name__)

MANUAL_RESULT_PREFIX = "manual:"
# After a cancellation the leases are extended to cover the longest a still
# running IMAP worker thread can live, plus a margin.
CANCELLED_LEASE_EXTENSION_SECONDS = IMAP_SESSION_DEADLINE_SECONDS + 60.0


class CollectorRunInProgressError(Exception):
    """Another collector or automation run holds the account's RUNNING slot.
    Nothing was started. Mapped to 409 / a short chat reply."""


class CollectorRunLeaseLostError(Exception):
    """The run's lease was lost (or could not be finalized) while it executed:
    exclusivity can no longer be proven, so the run is reported as failed even
    though the collector returned. Mapped to 409 / a short chat reply."""


@dataclass
class _Held:
    run: AutomationRunRecord
    run_id: int  # scalar: never touch the (expirable) ORM object on the cancel path
    holder: str
    heartbeat: _RunLeaseHeartbeat


def lock_account_keys(settings) -> tuple[str, ...]:
    """The distinct normalized account keys whose RUNNING slot a manual run
    must hold (sorted); `("",)` when neither identity is configured."""
    # The scheduler persists its key exactly as configured (stripped, case
    # preserved) while the REST automation endpoint persists the normalized
    # Gmail identity: hold both ACTUAL keys so every holder is excluded.
    # The REST key is kept even when empty (Gmail unset): REST automation runs
    # under "" then and must stay excluded.
    keys = {normalize_account_key(settings.gmail_username or "")}
    scheduler_key = (settings.automation_scheduler_account_key or "").strip()
    if scheduler_key:
        keys.add(scheduler_key)
    return tuple(sorted(keys))


def assert_collector_configured(collector_name: str, settings) -> None:
    """Side-effect-free twin of the checks at the top of `run_bundesagentur` /
    `run_xing`, so a misconfigured collector fails before a slot is claimed."""
    from app.collectors.bundesagentur import is_api_key_configured

    if collector_name == "bundesagentur":
        from app.core.config import reveal

        if not is_api_key_configured(reveal(settings.bundesagentur_api_key)):
            raise CollectorNotConfiguredError(
                "Bundesagentur collector is not configured: set BUNDESAGENTUR_API_KEY."
            )
    elif collector_name == "xing":
        if not is_configured(settings.xing_mailbox_username) or not is_configured(
            settings.xing_mailbox_app_password
        ):
            raise CollectorNotConfiguredError(
                "XING mailbox collector is not configured: set "
                "XING_MAILBOX_USERNAME and XING_MAILBOX_APP_PASSWORD."
            )
    else:
        raise ValueError(f"unknown collector: {collector_name}")


def _step_result(stats: dict) -> tuple[str, dict]:
    """(run status, AutomationRunStepResult-shaped dict) for a returned summary,
    classified like `app.services.automation._run_step`."""
    counters = {k: int(v) for k, v in stats.items() if isinstance(v, (int, bool)) and v is not None}
    attempted = counters.get("created", 0) + counters.get("updated", 0) + counters.get("failed", 0)
    failed = counters.get("failed", 0)
    if attempted == 0 or failed == 0:
        status = "ok"
    elif failed == attempted:
        status = "failed"
    else:
        status = "partial"
    if status == "ok" and (counters.get("deadline_exceeded") or counters.get("failed_queries")):
        status = "partial"
    run_status = {"ok": "COMPLETED", "partial": "PARTIAL", "failed": "FAILED"}[status]
    return run_status, {"status": status, "counters": counters, "error_type": None}


def _acquire(db: Session, settings, collector_name: str) -> list[_Held]:
    ttl = AUTOMATION_RUN_LEASE_TTL_SECONDS
    held: list[_Held] = []
    try:
        for key in lock_account_keys(settings):
            holder = new_run_lease_holder_token(f"{MANUAL_HOLDER_PREFIX}{collector_name}")
            run, created = create_running_run(db, account_key=key, holder=holder, ttl_seconds=ttl)
            if not created:
                raise CollectorRunInProgressError(
                    f"{collector_name}: a collector or automation run is already in progress"
                )
            heartbeat = _RunLeaseHeartbeat(
                db, run.id, holder=holder, ttl_seconds=ttl, interval_seconds=ttl / 3
            )
            held.append(_Held(run, run.id, holder, heartbeat))
            heartbeat.start()
    except BaseException:
        _stop_heartbeats(held)
        _finalize(db, held, "FAILED", {}, "ManualRunNotStarted", rollback=True)
        raise
    return held


def _stop_heartbeats(held: list[_Held]) -> bool:
    """Stop and join every heartbeat. True only if each one terminated and none
    reported a lost lease -- anything else means exclusivity is not provable."""
    for item in held:
        item.heartbeat.stop()
    return not any(item.heartbeat.lease_lost.is_set() or item.heartbeat.is_alive() for item in held)


def _finalize(
    db: Session,
    held: list[_Held],
    status: str,
    results: dict,
    error_summary: str | None,
    *,
    rollback: bool,
) -> bool:
    """Heartbeats MUST already be stopped (`_stop_heartbeats`). Finish every run;
    True only if each finish CAS won (holder still owns an unexpired lease)."""
    if rollback:
        db.rollback()
    ok = True
    for item in held:
        try:
            finished = finish_run(
                db,
                item.run,
                holder=item.holder,
                status=status,
                results=results,
                error_summary=error_summary,
            )
        except Exception as exc:
            db.rollback()
            logger.warning("manual_collector_finish_error error_type=%s", type(exc).__name__)
            ok = False
            continue
        if finished is None:
            ok = False
    return ok


def _extend_after_cancel(db: Session, held: list[_Held]) -> None:
    """Cancellation: keep the slots RUNNING long enough for a still-running
    worker thread to finish, using a separate session (the request session
    may still be in use by that thread)."""
    factory = sessionmaker(bind=db.get_bind())
    for item in held:
        item.heartbeat.stop()
    with factory() as side:
        for item in held:
            try:
                renew_run_lease(
                    side,
                    item.run_id,
                    holder=item.holder,
                    ttl_seconds=CANCELLED_LEASE_EXTENSION_SECONDS,
                )
            except Exception as exc:
                side.rollback()
                logger.warning(
                    "manual_collector_cancel_extend_error error_type=%s", type(exc).__name__
                )


@dataclass
class SlotHold:
    """What `hold_collector_slots` hands its body: `lease_lost()` (check between units of
    work) and `stats` (counters recorded on the finished run)."""

    lease_lost: Callable[[], bool]
    stats: dict = field(default_factory=dict)


@contextmanager
def hold_collector_slots(db: Session, settings, collector_name: str) -> Iterator[SlotHold]:
    """Synchronous twin of `run_collector_exclusively` for operator CLIs that change
    vacancy data (`scripts/stage22_refresh_ba.py --apply`): the body runs while the
    account's RUNNING slot(s) are held with a heartbeat, so the scheduler, REST and
    Telegram runs are refused meanwhile and vice versa.

    Raises `CollectorRunInProgressError` before the body runs when a slot is taken, and
    `CollectorRunLeaseLostError` after it when exclusivity could not be proven (the run is
    then recorded FAILED, never COMPLETED). An exception from the body finalizes the runs
    FAILED and propagates."""
    held = _acquire(db, settings, collector_name)
    hold = SlotHold(lease_lost=lambda: any(item.heartbeat.lease_lost.is_set() for item in held))
    step_name = f"{MANUAL_RESULT_PREFIX}{collector_name}"
    try:
        yield hold
    except BaseException as exc:
        error_type = type(exc).__name__
        failed = {step_name: {"status": "failed", "counters": None, "error_type": error_type}}
        _stop_heartbeats(held)
        _finalize(db, held, "FAILED", failed, f"{step_name}: failed ({error_type})", rollback=True)
        raise
    run_status, step = _step_result(hold.stats)
    if _stop_heartbeats(held):
        ok = _finalize(db, held, run_status, {step_name: step}, None, rollback=False)
    else:
        lost = {step_name: {"status": "failed", "counters": None, "error_type": "LeaseLost"}}
        _finalize(db, held, "FAILED", lost, f"{step_name}: failed (LeaseLost)", rollback=False)
        ok = False
    if not ok:
        raise CollectorRunLeaseLostError(
            f"{collector_name}: ownership of the run was lost before it could be recorded"
        )


async def run_collector_exclusively(
    db: Session,
    settings,
    collector_name: str,
    runner: Callable[..., Awaitable[dict]],
) -> dict:
    """`_run_exclusively` on a private event loop in a worker thread (review 2.4):
    claiming the slot, the collector run, heartbeat shutdown and finalization all
    block on the database, so none of it may run on the web loop. A cancelled
    caller waits for the worker before the session can be closed
    (`app.services.offload`)."""
    return await run_off_loop(lambda: _run_exclusively(db, settings, collector_name, runner))


async def _run_exclusively(
    db: Session,
    settings,
    collector_name: str,
    runner: Callable[..., Awaitable[dict]],
) -> dict:
    """Run `runner(db, settings, is_lease_lost=...)` while holding the account's
    RUNNING slot(s). Raises `CollectorNotConfiguredError` (before any slot),
    `CollectorRunInProgressError`, `CollectorRunLeaseLostError`, or whatever
    the runner raises (after the runs were finalized FAILED)."""
    assert_collector_configured(collector_name, settings)
    held = _acquire(db, settings, collector_name)
    step_name = f"{MANUAL_RESULT_PREFIX}{collector_name}"

    def lease_lost() -> bool:
        return any(item.heartbeat.lease_lost.is_set() for item in held)

    try:
        stats = await runner(db, settings, is_lease_lost=lease_lost)
    except asyncio.CancelledError:
        _extend_after_cancel(db, held)
        raise
    except BaseException as exc:
        error_type = type(exc).__name__
        failed = {step_name: {"status": "failed", "counters": None, "error_type": error_type}}
        _stop_heartbeats(held)
        _finalize(db, held, "FAILED", failed, f"{step_name}: failed ({error_type})", rollback=True)
        raise

    run_status, step = _step_result(stats)
    # Stop and verify the heartbeats BEFORE anything is recorded as success: a
    # renewal that failed (or a thread that will not stop) during shutdown means
    # exclusivity cannot be proven, so the run is recorded as FAILED, not COMPLETED.
    healthy = _stop_heartbeats(held)
    if healthy:
        ok = _finalize(db, held, run_status, {step_name: step}, None, rollback=False)
    else:
        lost = {step_name: {"status": "failed", "counters": None, "error_type": "LeaseLost"}}
        _finalize(db, held, "FAILED", lost, f"{step_name}: failed (LeaseLost)", rollback=False)
        ok = False
    if not healthy or not ok:
        raise CollectorRunLeaseLostError(
            f"{collector_name}: ownership of the run was lost before it could be recorded"
        )
    return stats
