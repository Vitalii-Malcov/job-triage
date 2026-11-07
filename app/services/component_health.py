"""Component health for GET /api/v1/ready and /api/v1/health/components (audit OPS-02).

Three separate signals:
- liveness   (`/health`)      -- the process answers; touches nothing.
- readiness  (`/ready`)       -- database at the expected revision AND, only if a bot token is
                                 configured, the Telegram bot is actually polling.
- components (`/health/components`) -- coarse per-component states for an operator.

The bot state is process-local on purpose: the bot lives in the web process. A deliberately
disabled bot (no token) is DISABLED and never makes readiness fail. The scheduler is a separate
process, so its failure degrades the component report but does not pull the web container out of
rotation.
"""

import threading
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models.automation import AutomationRunRecord, AutomationScheduleRecord


class BotState(StrEnum):
    DISABLED = "disabled"  # no token: intentionally off
    STARTING = "starting"
    RUNNING = "running"
    RETRYING = "retrying"  # a start attempt failed; the supervisor is backing off
    FAILED = "failed"  # terminal (e.g. invalid token) or supervisor crashed
    STOPPED = "stopped"  # shutting down


_lock = threading.Lock()
_telegram_state = BotState.DISABLED


def set_telegram_state(state: BotState) -> None:
    global _telegram_state
    with _lock:
        _telegram_state = state


def telegram_state() -> BotState:
    with _lock:
        return _telegram_state


def telegram_ready() -> bool:
    """True when the bot is not required (disabled) or is running."""
    return telegram_state() in (BotState.DISABLED, BotState.RUNNING)


# The scheduler polls every few seconds but only advances `next_run_at` per slot; a schedule
# overdue by more than this many intervals means no scheduler is claiming it.
_SCHEDULE_OVERDUE_INTERVALS = 2


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def scheduler_state(db: Session, settings: Settings, now: datetime | None = None) -> str:
    """disabled | ok | stale | unknown."""
    if not (settings.automation_scheduler_enabled or settings.telegram_daily_digest_enabled):
        return "disabled"
    if not settings.automation_scheduler_enabled:
        return "unknown"  # digest-only: no schedule row to inspect
    now = now or datetime.now(UTC)
    rows = db.scalars(select(AutomationScheduleRecord)).all()
    if not rows:
        return "unknown"
    limit = timedelta(
        seconds=settings.automation_scheduler_interval_seconds * _SCHEDULE_OVERDUE_INTERVALS
        + settings.automation_scheduler_poll_seconds
    )
    return "stale" if any(now - _aware(r.next_run_at) > limit for r in rows) else "ok"


def expired_run_leases(db: Session, now: datetime | None = None) -> int:
    """RUNNING automation runs whose lease has lapsed (a crashed holder)."""
    now = now or datetime.now(UTC)
    rows = db.scalars(
        select(AutomationRunRecord).where(AutomationRunRecord.status == "RUNNING")
    ).all()
    return sum(
        1 for r in rows if r.lease_expires_at is not None and _aware(r.lease_expires_at) < now
    )


def components_report(db: Session | None, settings: Settings, database_ready: bool) -> dict:
    """Coarse states only -- never exception text, account keys or counts of private data."""
    bot = telegram_state()
    report: dict[str, str] = {
        "database": "ok" if database_ready else "down",
        "telegram": bot.value,
        "scheduler": "unknown",
        "collector_leases": "unknown",
    }
    if db is not None and database_ready:
        try:
            report["scheduler"] = scheduler_state(db, settings)
            report["collector_leases"] = "stale" if expired_run_leases(db) else "ok"
        except Exception:  # noqa: BLE001 -- a report must never raise
            pass
    return report
