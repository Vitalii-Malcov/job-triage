"""Persistence for Stage 9A `TelegramVacancyReviewRecord` -- the Telegram
vacancy feed's review/delivery state, one row per `JobRecord`.

Concurrency shape mirrors `app.db.telegram_digest_repository`: the first
write is an INSERT + IntegrityError-catch on `UNIQUE(job_id)`, and every
later state transition is a CAS `UPDATE ... WHERE id = :id AND state =
:expected`, so two concurrent collector runs (e.g. the automation cycle and
a manual `/run`) can never both claim the same card for sending.
"""

import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import TelegramVacancyReviewRecord

# Bounds how long a `SENDING` claim may sit before it is treated as
# abandoned (the claiming process crashed between claim and outcome).
# Generous relative to one send attempt (`telegram_timeout_seconds`,
# default 5s), same rationale as
# app.db.telegram_digest_repository.STALE_PENDING_TTL_SECONDS.
STALE_SENDING_TTL_SECONDS = 300.0

# A row whose send provably failed this many times stops being retried
# automatically (DELIVERY_FAILED) -- a message Telegram permanently
# rejects (e.g. 400 for its content) must not be resent on every run forever.
MAX_DELIVERY_ATTEMPTS = 5

# `secrets.token_urlsafe(12)` -> exactly 16 chars of [A-Za-z0-9_-].
CALLBACK_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16}")

# States from which an operator decision (Save/Skip) is accepted: the card
# must have (or may have, for UNCERTAIN) reached the chat. A decision can
# also be changed (SAVED <-> SKIPPED).
DECIDABLE_STATES = ("TELEGRAM_SENT", "DELIVERY_UNCERTAIN", "SAVED", "SKIPPED")
DECISION_STATES = ("SAVED", "SKIPPED")


class TelegramVacancyReviewRepositoryConsistencyError(Exception):
    """A persistence invariant that must always hold was violated anyway."""


def _new_callback_token() -> str:
    return secrets.token_urlsafe(12)


def _new_claim_token() -> str:
    # 128 random bits per claim (Codex S9A-CODEX-004): an identity that does
    # not repeat across release/reclaim cycles, processes or restarts, so a
    # stale claim's token can never become valid again.
    return secrets.token_urlsafe(16)


def get_review_for_job(db: Session, job_id: int) -> TelegramVacancyReviewRecord | None:
    return db.scalar(
        select(TelegramVacancyReviewRecord).where(TelegramVacancyReviewRecord.job_id == job_id)
    )


def get_review_by_callback_token(db: Session, token: str) -> TelegramVacancyReviewRecord | None:
    """Resolve a callback token to its row. A token that is not exactly the
    expected shape is rejected without querying -- callback_data is
    client-supplied input."""
    if not CALLBACK_TOKEN_PATTERN.fullmatch(token):
        return None
    return db.scalar(
        select(TelegramVacancyReviewRecord).where(
            TelegramVacancyReviewRecord.callback_token == token
        )
    )


def _queue_if_discovered(db: Session, record: TelegramVacancyReviewRecord) -> bool:
    now = datetime.now(UTC)
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(
            TelegramVacancyReviewRecord.id == record.id,
            TelegramVacancyReviewRecord.state == "DISCOVERED",
        )
        .values(state="QUEUED_FOR_REVIEW", queued_at=now, updated_at=now)
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return won


def ensure_review(db: Session, job_id: int, *, eligible: bool) -> TelegramVacancyReviewRecord:
    """Record that `job_id` was (re)collected. Creates the row on first
    sight -- `QUEUED_FOR_REVIEW` if `eligible`, else `DISCOVERED`. On a
    later sight, the only transition ever made here is `DISCOVERED ->
    QUEUED_FOR_REVIEW` (a re-score made it eligible). A row already queued,
    sent, decided, or failed is never touched, so re-collection can never
    re-send a card.
    """
    existing = get_review_for_job(db, job_id)
    if existing is None:
        now = datetime.now(UTC)
        record = TelegramVacancyReviewRecord(
            job_id=job_id,
            state="QUEUED_FOR_REVIEW" if eligible else "DISCOVERED",
            callback_token=_new_callback_token(),
            attempt_count=0,
            queued_at=now if eligible else None,
            created_at=now,
            updated_at=now,
        )
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = get_review_for_job(db, job_id)
            if existing is None:
                raise TelegramVacancyReviewRepositoryConsistencyError(
                    f"Expected a telegram_vacancy_reviews row for job_id={job_id} after a "
                    "UNIQUE constraint collision, but none was found."
                ) from None
        else:
            db.refresh(record)
            return record

    if eligible and existing.state == "DISCOVERED":
        _queue_if_discovered(db, existing)
    return existing


def dequeue(db: Session, record: TelegramVacancyReviewRecord) -> bool:
    """CAS `QUEUED_FOR_REVIEW -> DISCOVERED` for a job that stopped being
    eligible (re-scored down, or its application status moved on) before
    its card was sent."""
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(
            TelegramVacancyReviewRecord.id == record.id,
            TelegramVacancyReviewRecord.state == "QUEUED_FOR_REVIEW",
        )
        .values(state="DISCOVERED", queued_at=None, updated_at=datetime.now(UTC))
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return won


def list_queued(db: Session, *, limit: int) -> list[TelegramVacancyReviewRecord]:
    """Oldest-queued first, so a backlog drains in discovery order."""
    stmt = (
        select(TelegramVacancyReviewRecord)
        .where(TelegramVacancyReviewRecord.state == "QUEUED_FOR_REVIEW")
        .order_by(TelegramVacancyReviewRecord.queued_at.asc(), TelegramVacancyReviewRecord.id)
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def claim_for_sending(db: Session, record: TelegramVacancyReviewRecord) -> str | None:
    """CAS `QUEUED_FOR_REVIEW -> SENDING`. Exactly one concurrent caller
    wins; only the winner may call Telegram for this row.

    Returns the winner's fresh `claim_token`, or None if the claim was lost.
    The token is this claim's identity (Codex S9A-CODEX-004): every
    transition out of `SENDING` must present it in its UPDATE's WHERE
    clause, and every such transition clears it. A stale caller holding an
    older claim's token can therefore never resolve or release a newer
    claim on the same row."""
    claim_token = _new_claim_token()
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(
            TelegramVacancyReviewRecord.id == record.id,
            TelegramVacancyReviewRecord.state == "QUEUED_FOR_REVIEW",
        )
        .values(
            state="SENDING",
            claim_token=claim_token,
            attempt_count=TelegramVacancyReviewRecord.attempt_count + 1,
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return claim_token if won else None


def _owns_claim(claim_token: str) -> tuple:
    """WHERE predicates binding a `SENDING -> *` transition to the exact
    claim that acquired it. Enforced by the database UPDATE itself -- never
    a check on the caller's (possibly stale) ORM object."""
    return (
        TelegramVacancyReviewRecord.state == "SENDING",
        TelegramVacancyReviewRecord.claim_token == claim_token,
    )


def release_claim(
    db: Session, record: TelegramVacancyReviewRecord, *, claim_token: str, reason: str
) -> bool:
    """CAS `SENDING -> QUEUED_FOR_REVIEW` for a claim abandoned BEFORE any
    Telegram request was started (e.g. the run's lease was confirmed lost
    between claim and send, Codex S9A-CODEX-002). Nothing reached Telegram,
    so the row is known-not-sent: it goes back to the queue with its
    `queued_at` position kept and the claim's `attempt_count` increment
    undone, so an abandoned claim never consumes a delivery attempt.

    Only the claim identified by `claim_token` can be released (Codex
    S9A-CODEX-004): a repeated or stale release -- even after the row was
    re-claimed by another worker -- matches no row and changes nothing."""
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(
            TelegramVacancyReviewRecord.id == record.id,
            *_owns_claim(claim_token),
            TelegramVacancyReviewRecord.attempt_count > 0,
        )
        .values(
            state="QUEUED_FOR_REVIEW",
            claim_token=None,
            attempt_count=TelegramVacancyReviewRecord.attempt_count - 1,
            last_error=reason[:200],
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return won


def mark_sent(
    db: Session,
    record: TelegramVacancyReviewRecord,
    *,
    claim_token: str,
    message_id: int | None,
) -> bool:
    """CAS `SENDING -> TELEGRAM_SENT` for the claim `claim_token`, only after
    Telegram confirmed (2xx)."""
    now = datetime.now(UTC)
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(TelegramVacancyReviewRecord.id == record.id, *_owns_claim(claim_token))
        .values(
            state="TELEGRAM_SENT",
            claim_token=None,
            telegram_message_id=message_id,
            sent_at=now,
            last_error=None,
            updated_at=now,
        )
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return won


def mark_send_failed(
    db: Session,
    record: TelegramVacancyReviewRecord,
    *,
    claim_token: str,
    last_error: str,
    max_attempts: int = MAX_DELIVERY_ATTEMPTS,
) -> bool:
    """CAS `SENDING -> QUEUED_FOR_REVIEW` (retried by the next sweep) for a
    send under claim `claim_token` that provably did NOT reach Telegram --
    or `SENDING -> DELIVERY_FAILED` once `attempt_count` reached
    `max_attempts`. The cap is evaluated by the database against the row's
    current `attempt_count`, not the caller's ORM copy."""
    next_state = case(
        (TelegramVacancyReviewRecord.attempt_count >= max_attempts, "DELIVERY_FAILED"),
        else_="QUEUED_FOR_REVIEW",
    )
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(TelegramVacancyReviewRecord.id == record.id, *_owns_claim(claim_token))
        .values(
            state=next_state,
            claim_token=None,
            last_error=last_error[:200],
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return won


def mark_uncertain(
    db: Session, record: TelegramVacancyReviewRecord, *, claim_token: str, last_error: str
) -> bool:
    """CAS `SENDING -> DELIVERY_UNCERTAIN` for the claim `claim_token` --
    never automatically retried."""
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(TelegramVacancyReviewRecord.id == record.id, *_owns_claim(claim_token))
        .values(
            state="DELIVERY_UNCERTAIN",
            claim_token=None,
            last_error=last_error[:200],
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return won


def reconcile_stale_sending(
    db: Session,
    *,
    ttl_seconds: float = STALE_SENDING_TTL_SECONDS,
    now: datetime | None = None,
) -> int:
    """Bulk CAS `SENDING -> DELIVERY_UNCERTAIN` for claims older than
    `ttl_seconds` (the claiming process died mid-send, so whether the card
    reached Telegram is unknowable). A live claim is never matched. Returns
    the number of rows reconciled."""
    effective_now = now if now is not None else datetime.now(UTC)
    cutoff = effective_now - timedelta(seconds=ttl_seconds)
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(
            TelegramVacancyReviewRecord.state == "SENDING",
            TelegramVacancyReviewRecord.updated_at < cutoff,
        )
        .values(
            state="DELIVERY_UNCERTAIN",
            claim_token=None,
            last_error="stale_sending_reconciled",
            updated_at=effective_now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount or 0


def record_decision(db: Session, record: TelegramVacancyReviewRecord, decision: str) -> bool:
    """CAS the operator's Save/Skip decision. Returns True if the state
    changed; False if the row was already in `decision` or is not in a
    decidable state (never delivered). Never touches `JobRecord.status`."""
    if decision not in DECISION_STATES:
        raise ValueError(f"unsupported decision {decision!r}")
    observed_state = record.state
    if observed_state == decision or observed_state not in DECIDABLE_STATES:
        return False
    now = datetime.now(UTC)
    result = db.execute(
        update(TelegramVacancyReviewRecord)
        .where(
            TelegramVacancyReviewRecord.id == record.id,
            TelegramVacancyReviewRecord.state == observed_state,
        )
        .values(state=decision, decided_at=now, updated_at=now)
    )
    db.commit()
    won = result.rowcount == 1
    db.refresh(record)
    return won
