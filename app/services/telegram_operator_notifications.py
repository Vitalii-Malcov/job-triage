"""Stage 16: Telegram notifications for Stage 15 lifecycle events.

Notifiable events (read from `application_events`, never written):

- a status change / operator override to RECEIVED, UNDER_REVIEW, INTERVIEW,
  TECHNICAL_INTERVIEW, OFFER or REJECTED;
- an inbound e-mail that Stage 15 recorded WITHOUT a status change because
  a terminal outcome (REJECTION / OFFER) needs operator confirmation. The
  notification says so; it never applies the outcome. Confirmation buttons
  are Stage 17 backlog -- the operator confirms with the Stage 15 tracker.

**Deduplication.** `telegram_lifecycle_notifications` holds at most one row
per event (UNIQUE). The row is claimed (SENDING) before the send, so a
re-run, a concurrent sweep or a replayed event finds it and sends nothing.
A provably FAILED send is retried on the same row (bounded); an UNCERTAIN
one is never retried. Delivery bookkeeping never touches the application.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import telegram_operator_repository as repo
from app.db.models import ApplicationEventRecord, ApplicationRecord
from app.models.application_lifecycle import ApplicationEventType
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.telegram_operator import TelegramDeliveryState
from app.services.telegram import TelegramSendOutcome
from app.services.telegram_operator_render import (
    LifecycleNotificationView,
    render_lifecycle_notification,
)
from app.services.telegram_operator_transport import TelegramOperatorTransport

logger = logging.getLogger(__name__)

NOTIFY_STATUSES = frozenset(
    {
        S.RECEIVED.value,
        S.UNDER_REVIEW.value,
        S.INTERVIEW.value,
        S.TECHNICAL_INTERVIEW.value,
        S.OFFER.value,
        S.REJECTED.value,
    }
)
_STATUS_EVENTS = frozenset(
    {ApplicationEventType.STATUS_CHANGED.value, ApplicationEventType.STATUS_OVERRIDDEN.value}
)
_CONFIRMABLE = frozenset({S.OFFER.value, S.REJECTED.value})
DEFAULT_SWEEP_LIMIT = 20
_SCAN_FACTOR = 10


def _metadata(event: ApplicationEventRecord) -> dict:
    try:
        value = json.loads(event.metadata_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_notification_view(
    event: ApplicationEventRecord, application: ApplicationRecord
) -> LifecycleNotificationView | None:
    """The view for a notifiable event, None otherwise."""
    meta = _metadata(event)
    if event.event_type in _STATUS_EVENTS and event.new_status in NOTIFY_STATUSES:
        return LifecycleNotificationView(
            company=application.company_name,
            role=application.role_title,
            event_type=event.event_type,
            origin=event.origin,
            previous_status=event.old_status,
            new_status=event.new_status,
            current_status=application.current_status,
            classification=meta.get("classification"),
        )
    if (
        event.event_type == ApplicationEventType.INBOUND_MESSAGE.value
        and meta.get("operator_confirmation_required") is True
        and meta.get("proposed_status") in _CONFIRMABLE
    ):
        return LifecycleNotificationView(
            company=application.company_name,
            role=application.role_title,
            event_type=event.event_type,
            origin=event.origin,
            previous_status=None,
            new_status=None,
            current_status=application.current_status,
            classification=meta.get("classification"),
            confirmation_required=True,
            proposed_status=meta.get("proposed_status"),
        )
    return None


@dataclass(frozen=True)
class PendingNotification:
    event_id: int
    application_id: int
    text: str
    ledger_state: str | None


def find_pending_notifications(
    db: Session, *, recorded_after: datetime, limit: int = DEFAULT_SWEEP_LIMIT
) -> list[PendingNotification]:
    """Notifiable events recorded at/after `recorded_after` that have no
    ledger row yet, or whose row is a retryable FAILED. Read-only."""
    events = db.scalars(
        select(ApplicationEventRecord)
        .where(ApplicationEventRecord.recorded_at >= recorded_after)
        .order_by(ApplicationEventRecord.id)
        .limit(limit * _SCAN_FACTOR)
    ).all()
    pending: list[PendingNotification] = []
    for event in events:
        ledger = repo.get_notification_for_event(db, event.id)
        if ledger is not None and not (
            ledger.delivery_state == TelegramDeliveryState.FAILED.value
            and ledger.attempt_count < repo.MAX_DELIVERY_ATTEMPTS
        ):
            continue
        application = db.get(ApplicationRecord, event.application_id)
        view = build_notification_view(event, application) if application else None
        if view is None:
            continue
        pending.append(
            PendingNotification(
                event_id=event.id,
                application_id=event.application_id,
                text=render_lifecycle_notification(view),
                ledger_state=ledger.delivery_state if ledger else None,
            )
        )
        if len(pending) >= limit:
            break
    return pending


@dataclass
class NotificationStats:
    sent: int = 0
    failed: int = 0
    uncertain: int = 0
    duplicate: int = 0
    event_ids: list[int] = field(default_factory=list)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def deliver_notification(
    db: Session, event_id: int, text: str, application_id: int, transport
) -> TelegramDeliveryState | None:
    """Claim, send and resolve ONE event's notification. None = already
    notified / claimed elsewhere (nothing sent)."""
    ledger = repo.get_notification_for_event(db, event_id)
    if ledger is None:
        try:
            ledger = repo.insert_sending_notification(
                db,
                application_event_id=event_id,
                application_id=application_id,
                text_sha256=_digest(text),
            )
        except IntegrityError:
            db.rollback()
            logger.info("telegram_notification_duplicate event_id=%s", event_id)
            return None
    elif not repo.reclaim_failed_notification(db, ledger):
        logger.info("telegram_notification_duplicate event_id=%s", event_id)
        return None
    try:
        result = await transport.send(text, None)
    except Exception as exc:
        repo.resolve_notification(
            db, ledger, TelegramDeliveryState.FAILED, error=type(exc).__name__
        )
        logger.warning(
            "telegram_transport_failed event_id=%s error_type=%s", event_id, type(exc).__name__
        )
        return TelegramDeliveryState.FAILED
    state = {
        TelegramSendOutcome.SENT: TelegramDeliveryState.SENT,
        TelegramSendOutcome.FAILED: TelegramDeliveryState.FAILED,
        TelegramSendOutcome.UNCERTAIN: TelegramDeliveryState.UNCERTAIN,
    }[result.outcome]
    repo.resolve_notification(db, ledger, state, message_id=result.message_id)
    if state is TelegramDeliveryState.SENT:
        logger.info("telegram_notification_sent event_id=%s", event_id)
    else:
        logger.warning("telegram_transport_failed event_id=%s outcome=%s", event_id, state.value)
    return state


async def deliver_lifecycle_notifications(
    db: Session,
    settings,
    transport: TelegramOperatorTransport,
    *,
    recorded_after: datetime,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> NotificationStats:
    stats = NotificationStats()
    if not settings.telegram_operator_control_enabled:
        logger.info("telegram_notification_skipped reason=disabled")
        return stats
    for item in find_pending_notifications(db, recorded_after=recorded_after, limit=limit):
        state = await deliver_notification(
            db, item.event_id, item.text, item.application_id, transport
        )
        if state is None:
            stats.duplicate += 1
            continue
        stats.event_ids.append(item.event_id)
        if state is TelegramDeliveryState.SENT:
            stats.sent += 1
        elif state is TelegramDeliveryState.FAILED:
            stats.failed += 1
        else:
            stats.uncertain += 1
    return stats
