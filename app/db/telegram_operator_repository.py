"""Stage 16: persistence primitives for Telegram operator control.

Thin, like `app.db.application_repository`: no business rules. Card and
notification delivery transitions are CAS `UPDATE ... WHERE id = :id AND
delivery_state = :expected` statements that commit immediately (delivery
bookkeeping must survive a crash mid-send). Decisions are INSERT-only and
are NOT committed here -- the workflow service commits a decision together
with the lifecycle event it implies (see
`app.services.telegram_operator_control`).
"""

import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.datetime_utils import ensure_utc
from app.db.models import (
    ApplicationOperatorDecisionRecord,
    TelegramLifecycleNotificationRecord,
    TelegramOperatorCardRecord,
)
from app.models.telegram_operator import (
    FINAL_DECISIONS,
    OperatorDecision,
    OperatorDecisionView,
    TelegramDeliveryState,
)

D = TelegramDeliveryState

# `secrets.token_urlsafe(12)` -> exactly 16 chars of [A-Za-z0-9_-].
CALLBACK_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16}")
# A provably failed send is retried at most this many attempts in total.
MAX_DELIVERY_ATTEMPTS = 5
# A SENDING claim older than this belongs to a process that died mid-send:
# whether Telegram got the message is unknowable -> UNCERTAIN.
STALE_SENDING_TTL = timedelta(minutes=5)
MAX_ERROR_LENGTH = 60


def _now() -> datetime:
    return datetime.now(UTC)


def new_callback_token() -> str:
    return secrets.token_urlsafe(12)


def target_key(application_id: int, review_package_id: int | None) -> str:
    package = str(review_package_id) if review_package_id is not None else "none"
    return f"app:{application_id}:pkg:{package}"


# --- cards ---------------------------------------------------------------------


def get_card_by_token(db: Session, token: str) -> TelegramOperatorCardRecord | None:
    """Resolve a callback token. A token of the wrong shape is refused
    without a query -- callback_data is client-supplied input."""
    if not isinstance(token, str) or not CALLBACK_TOKEN_PATTERN.fullmatch(token):
        return None
    return db.scalar(
        select(TelegramOperatorCardRecord).where(TelegramOperatorCardRecord.callback_token == token)
    )


def get_card_for_target(db: Session, key: str) -> TelegramOperatorCardRecord | None:
    return db.scalar(
        select(TelegramOperatorCardRecord).where(TelegramOperatorCardRecord.target_key == key)
    )


def insert_sending_card(
    db: Session,
    *,
    application_id: int,
    review_package_id: int | None,
    approve_offered: bool,
    card_text_sha256: str,
) -> TelegramOperatorCardRecord:
    """INSERT a new card already claimed for sending (attempt 1). A UNIQUE
    violation on `target_key` propagates: the caller re-reads."""
    now = _now()
    record = TelegramOperatorCardRecord(
        application_id=application_id,
        review_package_id=review_package_id,
        target_key=target_key(application_id, review_package_id),
        callback_token=new_callback_token(),
        delivery_state=D.SENDING.value,
        approve_offered=approve_offered,
        attempt_count=1,
        card_text_sha256=card_text_sha256,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def reclaim_failed_card(
    db: Session,
    record: TelegramOperatorCardRecord,
    *,
    approve_offered: bool,
    card_text_sha256: str,
    max_attempts: int = MAX_DELIVERY_ATTEMPTS,
) -> bool:
    """CAS `FAILED -> SENDING` (one more attempt) while attempts remain."""
    result = db.execute(
        update(TelegramOperatorCardRecord)
        .where(
            TelegramOperatorCardRecord.id == record.id,
            TelegramOperatorCardRecord.delivery_state == D.FAILED.value,
            TelegramOperatorCardRecord.attempt_count < max_attempts,
        )
        .values(
            delivery_state=D.SENDING.value,
            attempt_count=TelegramOperatorCardRecord.attempt_count + 1,
            approve_offered=approve_offered,
            card_text_sha256=card_text_sha256,
            updated_at=_now(),
        )
    )
    db.commit()
    db.refresh(record)
    return result.rowcount == 1


def resolve_card(
    db: Session,
    record: TelegramOperatorCardRecord,
    outcome: TelegramDeliveryState,
    *,
    message_id: int | None = None,
    error: str | None = None,
) -> bool:
    """CAS `SENDING -> SENT | FAILED | UNCERTAIN`."""
    now = _now()
    values: dict[str, Any] = {"delivery_state": outcome.value, "updated_at": now}
    if outcome is D.SENT:
        values.update(telegram_message_id=message_id, sent_at=now, last_error=None)
    else:
        values["last_error"] = (error or outcome.value)[:MAX_ERROR_LENGTH]
    result = db.execute(
        update(TelegramOperatorCardRecord)
        .where(
            TelegramOperatorCardRecord.id == record.id,
            TelegramOperatorCardRecord.delivery_state == D.SENDING.value,
        )
        .values(**values)
    )
    db.commit()
    db.refresh(record)
    return result.rowcount == 1


def reconcile_stale_card(db: Session, record: TelegramOperatorCardRecord) -> bool:
    """CAS a SENDING card older than `STALE_SENDING_TTL` to UNCERTAIN."""
    cutoff = _now() - STALE_SENDING_TTL
    if record.delivery_state != D.SENDING.value or ensure_utc(record.updated_at) >= cutoff:
        return False
    result = db.execute(
        update(TelegramOperatorCardRecord)
        .where(
            TelegramOperatorCardRecord.id == record.id,
            TelegramOperatorCardRecord.delivery_state == D.SENDING.value,
            TelegramOperatorCardRecord.updated_at < cutoff,
        )
        .values(
            delivery_state=D.UNCERTAIN.value,
            last_error="stale_sending_reconciled",
            updated_at=_now(),
        )
    )
    db.commit()
    db.refresh(record)
    return result.rowcount == 1


# --- decisions ---------------------------------------------------------------------


def to_decision_view(record: ApplicationOperatorDecisionRecord) -> OperatorDecisionView:
    return OperatorDecisionView(
        id=record.id,
        application_id=record.application_id,
        card_id=record.card_id,
        review_package_id=record.review_package_id,
        target_key=record.target_key,
        decision=OperatorDecision(record.decision),
        supersedes_decision_id=record.supersedes_decision_id,
        actor=record.actor,
        reason=record.reason,
        lifecycle_correlation_key=record.lifecycle_correlation_key,
        created_at=ensure_utc(record.created_at),
        metadata=json.loads(record.metadata_json or "{}"),
    )


def list_decisions_for_target(db: Session, key: str) -> list[ApplicationOperatorDecisionRecord]:
    return list(
        db.scalars(
            select(ApplicationOperatorDecisionRecord)
            .where(ApplicationOperatorDecisionRecord.target_key == key)
            .order_by(ApplicationOperatorDecisionRecord.id)
        ).all()
    )


def list_decisions_for_application(
    db: Session, application_id: int
) -> list[ApplicationOperatorDecisionRecord]:
    return list(
        db.scalars(
            select(ApplicationOperatorDecisionRecord)
            .where(ApplicationOperatorDecisionRecord.application_id == application_id)
            .order_by(ApplicationOperatorDecisionRecord.id)
        ).all()
    )


def get_decision_by_callback_ref(
    db: Session, callback_ref: str
) -> ApplicationOperatorDecisionRecord | None:
    return db.scalar(
        select(ApplicationOperatorDecisionRecord).where(
            ApplicationOperatorDecisionRecord.callback_ref == callback_ref
        )
    )


def effective_final_decision(
    records: list[ApplicationOperatorDecisionRecord],
) -> ApplicationOperatorDecisionRecord | None:
    """The ONE rule for "the operator's current final decision" on a target
    (records of one `target_key`, any order): the APPROVE/SKIP row that no
    other row supersedes. Decisions are append-only; a Telegram press is
    refused once a final decision exists, and an override always names the
    effective final it replaces in `supersedes_decision_id` -- so in a
    well-formed history there is exactly one such head. If there were more
    (never expected), the newest wins. LATER is non-final and never
    replaces or revokes a final decision."""
    finals = [r for r in records if OperatorDecision(r.decision) in FINAL_DECISIONS]
    superseded = {r.supersedes_decision_id for r in records if r.supersedes_decision_id}
    heads = [r for r in finals if r.id not in superseded]
    return max(heads, key=lambda r: r.id) if heads else None


def review_withdrawn_by_operator(db: Session, review_package_id: int) -> bool:
    """True when the effective final Stage 16 decision of ANY target bound
    to this review package is SKIP (including APPROVE -> SKIP overrides).
    Such a package must not be consumed downstream as an approval, even
    though its Stage 6E status stays APPROVED (history). No Stage 16
    decision at all -> False: legacy/direct Stage 6E approvals keep their
    meaning."""
    records = db.scalars(
        select(ApplicationOperatorDecisionRecord)
        .where(ApplicationOperatorDecisionRecord.review_package_id == review_package_id)
        .order_by(ApplicationOperatorDecisionRecord.id)
    ).all()
    by_target: dict[str, list[ApplicationOperatorDecisionRecord]] = {}
    for record in records:
        by_target.setdefault(record.target_key, []).append(record)
    for target_records in by_target.values():
        final = effective_final_decision(target_records)
        if final is not None and OperatorDecision(final.decision) is OperatorDecision.SKIP:
            return True
    return False


def count_decisions(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ApplicationOperatorDecisionRecord)) or 0


def add_decision(db: Session, **values: Any) -> ApplicationOperatorDecisionRecord:
    """Stage (flush, no commit) one decision row in the caller's
    transaction."""
    metadata = values.pop("metadata", None) or {}
    record = ApplicationOperatorDecisionRecord(
        metadata_json=json.dumps(metadata, sort_keys=True, ensure_ascii=False),
        created_at=_now(),
        **values,
    )
    db.add(record)
    db.flush()
    return record


# --- lifecycle notifications ---------------------------------------------------------


def get_notification_for_event(
    db: Session, application_event_id: int
) -> TelegramLifecycleNotificationRecord | None:
    return db.scalar(
        select(TelegramLifecycleNotificationRecord).where(
            TelegramLifecycleNotificationRecord.application_event_id == application_event_id
        )
    )


def insert_sending_notification(
    db: Session, *, application_event_id: int, application_id: int, text_sha256: str
) -> TelegramLifecycleNotificationRecord:
    """INSERT a ledger row already claimed for sending. A UNIQUE violation
    on `application_event_id` (a concurrent sweep) propagates."""
    now = _now()
    record = TelegramLifecycleNotificationRecord(
        application_event_id=application_event_id,
        application_id=application_id,
        delivery_state=D.SENDING.value,
        attempt_count=1,
        text_sha256=text_sha256,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def reclaim_failed_notification(
    db: Session,
    record: TelegramLifecycleNotificationRecord,
    *,
    max_attempts: int = MAX_DELIVERY_ATTEMPTS,
) -> bool:
    result = db.execute(
        update(TelegramLifecycleNotificationRecord)
        .where(
            TelegramLifecycleNotificationRecord.id == record.id,
            TelegramLifecycleNotificationRecord.delivery_state == D.FAILED.value,
            TelegramLifecycleNotificationRecord.attempt_count < max_attempts,
        )
        .values(
            delivery_state=D.SENDING.value,
            attempt_count=TelegramLifecycleNotificationRecord.attempt_count + 1,
            updated_at=_now(),
        )
    )
    db.commit()
    db.refresh(record)
    return result.rowcount == 1


def resolve_notification(
    db: Session,
    record: TelegramLifecycleNotificationRecord,
    outcome: TelegramDeliveryState,
    *,
    message_id: int | None = None,
    error: str | None = None,
) -> bool:
    now = _now()
    values: dict[str, Any] = {"delivery_state": outcome.value, "updated_at": now}
    if outcome is D.SENT:
        values.update(telegram_message_id=message_id, sent_at=now, last_error=None)
    else:
        values["last_error"] = (error or outcome.value)[:MAX_ERROR_LENGTH]
    result = db.execute(
        update(TelegramLifecycleNotificationRecord)
        .where(
            TelegramLifecycleNotificationRecord.id == record.id,
            TelegramLifecycleNotificationRecord.delivery_state == D.SENDING.value,
        )
        .values(**values)
    )
    db.commit()
    db.refresh(record)
    return result.rowcount == 1
