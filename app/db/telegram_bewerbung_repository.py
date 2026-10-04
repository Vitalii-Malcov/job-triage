"""Persistence for the Stage 9B Telegram Bewerbung preparation ledger
(`TelegramBewerbungPreparationRecord`) -- one row per vacancy-review row.

Two independently fenced state machines live on that row (see the model's
docstring):

- **Preparation** `PREPARING -> PREPARED | FAILED`. A claim is a fresh
  `generation` (monotonically increasing) plus a fresh random
  `prep_claim_token`. Every `PREPARING -> *` transition matches `id`,
  `state = 'PREPARING'`, `generation` and `prep_claim_token` in the SQL
  UPDATE itself -- never a check on a possibly stale ORM object -- so a
  stale worker cannot publish, fail, or attach artifacts to a newer
  generation. Publication does NOT commit: the caller commits it together
  with the letter it may have just flushed, or rolls both back.
- **Preview delivery** `NONE -> SENDING -> SENT | FAILED | UNCERTAIN`,
  Stage 9A-style external-side-effect fencing: `SENDING` is committed
  before the Telegram request, and only the exact `preview_claim_token`
  (on the same `generation`/`package_token`) resolves it. Stale `SENDING`
  is resolved to `UNCERTAIN`, never `FAILED`.

All mutating functions except `publish_preparation` own their commit, like
`app.db.telegram_vacancy_review_repository`.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import TelegramBewerbungPreparationRecord as Prep

# A preparation is local, deterministic work (no network); a claim older
# than this is treated as crashed/abandoned and counts as a failed attempt.
PREPARATION_STALE_SECONDS = 120.0
# Generous relative to one Telegram send (default timeout 5 s); same value
# as the Stage 9A card-delivery stale TTL.
PREVIEW_STALE_SECONDS = 300.0
# Genuine preparation attempts per unchanged input identity.
MAX_PREPARATION_ATTEMPTS = 5

PREVIEW_OUTCOMES = ("SENT", "FAILED", "UNCERTAIN")

# `secrets.token_urlsafe(12)` -> exactly 16 chars of [A-Za-z0-9_-].
PACKAGE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16}")


@dataclass(frozen=True)
class PreparationClaim:
    """The identity of one owned preparation generation."""

    preparation_id: int
    generation: int
    token: str


@dataclass(frozen=True)
class PreviewClaim:
    """The identity of one owned preview delivery."""

    preparation_id: int
    generation: int
    package_token: str
    token: str


def _now() -> datetime:
    return datetime.now(UTC)


def _new_claim_token() -> str:
    return secrets.token_urlsafe(16)


def _new_package_token() -> str:
    return secrets.token_urlsafe(12)


def get_preparation(db: Session, preparation_id: int) -> Prep | None:
    return db.get(Prep, preparation_id)


def get_preparation_for_review(db: Session, review_id: int) -> Prep | None:
    return db.scalar(select(Prep).where(Prep.review_id == review_id))


def get_preparation_by_package_token(db: Session, token: str) -> Prep | None:
    """Resolve a preview button's opaque capability. A token that is not
    exactly the expected shape is rejected without querying -- callback
    data is client-supplied input."""
    if not PACKAGE_TOKEN_PATTERN.fullmatch(token):
        return None
    return db.scalar(select(Prep).where(Prep.package_token == token))


def _cas(stmt):
    """Single-row CAS UPDATEs never synchronize in-session ORM objects: the
    outcome is decided by the database predicate alone, and callers re-read
    the row afterwards instead of trusting an evaluated in-memory copy."""
    return stmt.execution_options(synchronize_session=False)


def _owns_preparation(claim: PreparationClaim) -> tuple:
    return (
        Prep.id == claim.preparation_id,
        Prep.state == "PREPARING",
        Prep.generation == claim.generation,
        Prep.prep_claim_token == claim.token,
    )


def create_preparation_claim(
    db: Session, review_id: int, *, input_identity: str
) -> PreparationClaim | None:
    """First-ever press for `review_id`: INSERT the row directly in
    `PREPARING` (generation 1, attempt 1). A concurrent first press loses on
    UNIQUE(review_id): its transaction is rolled back and None returned, so
    the caller re-reads the winner's row."""
    token = _new_claim_token()
    now = _now()
    record = Prep(
        review_id=review_id,
        state="PREPARING",
        generation=1,
        input_identity=input_identity,
        prep_claim_token=token,
        prep_claim_started_at=now,
        attempt_count=1,
        preview_state="NONE",
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(record)
    return PreparationClaim(record.id, record.generation, token)


def claim_preparation(
    db: Session,
    record: Prep,
    *,
    input_identity: str,
    max_attempts: int = MAX_PREPARATION_ATTEMPTS,
) -> PreparationClaim | None:
    """CAS `PREPARED | FAILED -> PREPARING` as a NEW generation, from the
    exact state/generation the caller observed. An unchanged input identity
    consumes one more attempt and is refused once `max_attempts` is reached;
    a changed identity starts a fresh budget. Refused while a preview send
    of the current package is in flight (`preview_state = 'SENDING'`)."""
    if record.state not in ("PREPARED", "FAILED"):
        return None
    observed_id, observed_generation = record.id, record.generation
    token = _new_claim_token()
    now = _now()
    same_identity = record.input_identity == input_identity
    conditions = [
        Prep.id == record.id,
        Prep.state == record.state,
        Prep.generation == record.generation,
        Prep.input_identity == record.input_identity,
        Prep.preview_state != "SENDING",
    ]
    if same_identity:
        conditions.append(Prep.attempt_count < max_attempts)
        attempts = Prep.attempt_count + 1
    else:
        attempts = 1
    result = db.execute(
        _cas(update(Prep))
        .where(*conditions)
        .values(
            state="PREPARING",
            generation=Prep.generation + 1,
            input_identity=input_identity,
            prep_claim_token=token,
            prep_claim_started_at=now,
            attempt_count=attempts,
            last_error=None,
            updated_at=now,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return PreparationClaim(observed_id, observed_generation + 1, token)


def reconcile_stale_preparation(
    db: Session,
    record: Prep,
    *,
    ttl_seconds: float = PREPARATION_STALE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """CAS the EXACT observed claim (`generation` + token) `PREPARING ->
    FAILED` if it started more than `ttl_seconds` ago. Preparation has no
    external side effect, so FAILED (retryable) is safe. The crashed attempt
    already counted when it was claimed."""
    if record.state != "PREPARING" or record.prep_claim_token is None:
        return False
    effective_now = now if now is not None else _now()
    cutoff = effective_now - timedelta(seconds=ttl_seconds)
    result = db.execute(
        _cas(update(Prep))
        .where(
            Prep.id == record.id,
            Prep.state == "PREPARING",
            Prep.generation == record.generation,
            Prep.prep_claim_token == record.prep_claim_token,
            Prep.prep_claim_started_at < cutoff,
        )
        .values(
            state="FAILED",
            prep_claim_token=None,
            last_error="STALE_PREPARATION",
            updated_at=effective_now,
        )
    )
    db.commit()
    return result.rowcount == 1


def fail_preparation(db: Session, claim: PreparationClaim, *, error_code: str) -> bool:
    """CAS the owned claim `PREPARING -> FAILED` with a fixed, sanitized
    error code (never an exception message)."""
    result = db.execute(
        _cas(update(Prep))
        .where(*_owns_preparation(claim))
        .values(
            state="FAILED",
            prep_claim_token=None,
            last_error=error_code[:200],
            updated_at=_now(),
        )
    )
    db.commit()
    return result.rowcount == 1


def publish_preparation(
    db: Session,
    claim: PreparationClaim,
    *,
    match_id: int,
    cv_draft_id: int,
    bewerbung_draft_id: int,
    preview_text: str,
    preview_renderer_version: str,
) -> str | None:
    """CAS the owned claim `PREPARING -> PREPARED`, pinning the exact
    package and its frozen preview, with a fresh `package_token` and the
    preview state reset to NONE. **Does not commit** -- the caller commits
    it in the same transaction as a just-flushed letter, or rolls back both
    when this returns None (ownership lost)."""
    package_token = _new_package_token()
    result = db.execute(
        _cas(update(Prep))
        .where(*_owns_preparation(claim))
        .values(
            state="PREPARED",
            prep_claim_token=None,
            match_id=match_id,
            cv_draft_id=cv_draft_id,
            bewerbung_draft_id=bewerbung_draft_id,
            package_token=package_token,
            preview_text=preview_text,
            preview_renderer_version=preview_renderer_version,
            preview_state="NONE",
            preview_message_id=None,
            preview_sent_at=None,
            last_error=None,
            updated_at=_now(),
        )
    )
    return package_token if result.rowcount == 1 else None


def claim_preview(
    db: Session,
    record: Prep,
    *,
    allowed_states: tuple[str, ...],
    expected_generation: int | None = None,
    expected_package_token: str | None = None,
) -> PreviewClaim | None:
    """CAS `preview_state IN allowed_states -> SENDING` for the exact
    observed PREPARED package (`generation` + `package_token`), committed
    BEFORE any Telegram request. Exactly one concurrent caller wins.

    A caller holding a capability passes the generation/package token it
    originally authorized; the SQL predicate then uses THOSE values, never
    ones a replacement generation may have since loaded into `record`."""
    if record.state != "PREPARED" or record.package_token is None:
        return None
    if "SENDING" in allowed_states:
        raise ValueError("A preview claim can never be taken over from SENDING.")
    generation = record.generation if expected_generation is None else expected_generation
    package_token = (
        record.package_token if expected_package_token is None else expected_package_token
    )
    observed = (record.id, generation, package_token)
    token = _new_claim_token()
    now = _now()
    result = db.execute(
        _cas(update(Prep))
        .where(
            Prep.id == record.id,
            Prep.state == "PREPARED",
            Prep.generation == generation,
            Prep.package_token == package_token,
            Prep.preview_state.in_(allowed_states),
        )
        .values(
            preview_state="SENDING",
            preview_claim_token=token,
            preview_claim_started_at=now,
            updated_at=now,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return PreviewClaim(*observed, token)


def resolve_preview(
    db: Session, claim: PreviewClaim, outcome: str, *, message_id: int | None = None
) -> bool:
    """CAS the owned preview delivery `SENDING -> SENT | FAILED |
    UNCERTAIN`. Only the exact preview token on the same generation and
    package can resolve it."""
    if outcome not in PREVIEW_OUTCOMES:
        raise ValueError(f"Unsupported preview outcome: {outcome}")
    now = _now()
    values: dict = {
        "preview_state": outcome,
        "preview_claim_token": None,
        "updated_at": now,
    }
    if outcome == "SENT":
        values["preview_message_id"] = message_id
        values["preview_sent_at"] = now
    result = db.execute(
        _cas(update(Prep))
        .where(
            Prep.id == claim.preparation_id,
            Prep.state == "PREPARED",
            Prep.generation == claim.generation,
            Prep.package_token == claim.package_token,
            Prep.preview_state == "SENDING",
            Prep.preview_claim_token == claim.token,
        )
        .values(**values)
    )
    db.commit()
    return result.rowcount == 1


def reconcile_stale_preview(
    db: Session,
    record: Prep,
    *,
    ttl_seconds: float = PREVIEW_STALE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """CAS the EXACT observed stale preview send `SENDING -> UNCERTAIN`: the
    Telegram request may have been delivered, so it is never treated as a
    definite failure and never retried automatically."""
    if record.preview_state != "SENDING" or record.preview_claim_token is None:
        return False
    effective_now = now if now is not None else _now()
    cutoff = effective_now - timedelta(seconds=ttl_seconds)
    result = db.execute(
        _cas(update(Prep))
        .where(
            Prep.id == record.id,
            Prep.preview_state == "SENDING",
            Prep.preview_claim_token == record.preview_claim_token,
            Prep.preview_claim_started_at < cutoff,
        )
        .values(
            preview_state="UNCERTAIN",
            preview_claim_token=None,
            updated_at=effective_now,
        )
    )
    db.commit()
    return result.rowcount == 1
