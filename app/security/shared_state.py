"""Task 23: process-shared rate-limit and UI-session state, kept in the EXISTING database.

Why. The default backend (`SHARED_STATE_BACKEND=memory`) keeps every rate limiter, the failed-
authentication budget, UI logout revocations and the cookie nonce in one process; it is correct
only for the documented topology of ONE web process. With `SHARED_STATE_BACKEND=database` every
worker reads and writes the same two small tables, so limits, lockouts and revocations mean the
same in every process and survive a restart. No new service (Redis etc.) is involved.

Semantics. A sliding window over wall-clock time (`time.time()`: the value must mean the same in
every process). The decision "count < max, then record" is atomic across processes:

* PostgreSQL: a transaction-scoped advisory lock keyed by (limiter, identity), then one conditional
  INSERT ... SELECT ... WHERE count < max;
* SQLite: the single conditional INSERT statement itself is atomic under the database write lock.

Client identities are stored as SHA-256 digests, never as addresses. Any database error is raised
as `SharedStateUnavailableError`: callers fail CLOSED (429/503/not logged in), never open.

The UI request session is read-only (see `app/ui/readonly.py`); this module deliberately uses its
OWN write-capable engine (`app.db.session.engine`) for these two transient tables only.
"""

import hashlib
import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Live rows one limiter may hold. A newcomer beyond this is refused (fail closed): rotating source
# addresses cannot grow the table without bound. Existing identities keep working.
MAX_LIVE_ROWS_PER_LIMITER = 100_000
# Hard storage ceiling per limiter, live AND expired-but-not-yet-swept rows together: a NEW
# client is refused beyond it (backpressure when cleanup cannot keep up).
STORAGE_CEILING_FACTOR = 2


class SharedStateUnavailableError(Exception):
    """The shared store could not be used. Carries no SQL, parameters or addresses."""


def enabled() -> bool:
    return get_settings().shared_state_backend == "database"


def _default_engine() -> Engine:
    from app.db.session import engine

    return engine


_engine_provider: Callable[[], Engine] = _default_engine


def set_engine_provider(provider: Callable[[], Engine] | None) -> None:
    """Tests (and nothing else) point the store at a scratch database."""
    global _engine_provider
    _engine_provider = provider or _default_engine


def identity_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _session() -> Session:
    return Session(_engine_provider(), autoflush=False, expire_on_commit=False)


def _unavailable(exc: BaseException) -> SharedStateUnavailableError:
    logger.warning("shared_state_unavailable error_type=%s", type(exc).__name__)
    return SharedStateUnavailableError("shared state store unavailable")


def _is_postgresql(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _advisory_lock(db: Session, key: str) -> None:
    if _is_postgresql(db):
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"shared-state:{key}"},
        )


# ---------------------------------------------------------------- rate limits


_SWEEP_INTERVAL_SECONDS = 30.0
_SWEEP_BATCH = 500
# One sweep drains up to this many batches (a backlog is not left to a 500-rows-per-interval drip).
_SWEEP_MAX_BATCHES = 40
_last_sweep: dict[str, float] = {}
_sweep_guard = threading.Lock()


def _sweep_expired(limiter: str, cutoff: float) -> None:
    """Bounded cleanup of expired rows of OTHER identities, in its own short transaction and at
    most every `_SWEEP_INTERVAL_SECONDS` per process and limiter. It never runs inside the
    per-identity transaction: deleting another identity's rows there could wait on (and deadlock
    with) a concurrent transaction that is deleting its own. PostgreSQL skips rows another
    transaction already holds. Correctness never depends on it: counting always filters by
    time; this only keeps the table small."""
    moment = time.monotonic()
    with _sweep_guard:
        if moment - _last_sweep.get(limiter, -_SWEEP_INTERVAL_SECONDS) < _SWEEP_INTERVAL_SECONDS:
            return
        _last_sweep[limiter] = moment
    try:
        for _ in range(_SWEEP_MAX_BATCHES):
            with _session() as db:
                lock_clause = " FOR UPDATE SKIP LOCKED" if _is_postgresql(db) else ""
                deleted = db.execute(
                    text(
                        "DELETE FROM shared_rate_limit_hits WHERE id IN ("  # noqa: S608
                        "SELECT id FROM shared_rate_limit_hits "
                        "WHERE limiter = :l AND hit_at < :cutoff ORDER BY id "
                        f"LIMIT :n{lock_clause})"
                    ),
                    {"l": limiter, "cutoff": cutoff, "n": _SWEEP_BATCH},
                ).rowcount
                db.commit()
            if deleted < _SWEEP_BATCH:
                break
    except SQLAlchemyError as exc:
        logger.warning("shared_state_sweep_failed error_type=%s", type(exc).__name__)


def rate_hit(
    limiter: str,
    identity: str,
    *,
    max_requests: int,
    window_seconds: float,
    now: float | None = None,
) -> int | None:
    """Record one hit unless `identity` already holds `max_requests` live hits on `limiter`.
    Returns the hit id (the refund token), or None when the limit is reached (or the limiter is
    full and `identity` is new)."""
    moment = time.time() if now is None else now
    cutoff = moment - window_seconds
    digest = identity_hash(identity)
    _sweep_expired(limiter, cutoff)
    try:
        with _session() as db:
            _advisory_lock(db, f"{limiter}:{digest}")
            db.execute(
                text(
                    "DELETE FROM shared_rate_limit_hits "
                    "WHERE limiter = :l AND identity = :i AND hit_at < :cutoff"
                ),
                {"l": limiter, "i": digest, "cutoff": cutoff},
            )
            live = db.execute(
                text(
                    "SELECT count(*) FROM shared_rate_limit_hits "
                    "WHERE limiter = :l AND identity = :i"
                ),
                {"l": limiter, "i": digest},
            ).scalar_one()
            if live == 0:
                # a NEW identity: refuse it once the limiter holds MAX_LIVE_ROWS_PER_LIMITER LIVE
                # rows (known clients keep their own budget, so the real ceiling is that cap plus
                # distinct identities x budget). Only live rows count, so cleanup is optional.
                total = db.execute(
                    text(
                        "SELECT count(*) FROM shared_rate_limit_hits "
                        "WHERE limiter = :l AND hit_at >= :cutoff"
                    ),
                    {"l": limiter, "cutoff": cutoff},
                ).scalar_one()
                stored = db.execute(
                    text("SELECT count(*) FROM shared_rate_limit_hits WHERE limiter = :l"),
                    {"l": limiter},
                ).scalar_one()
                if (
                    total >= MAX_LIVE_ROWS_PER_LIMITER
                    or stored >= MAX_LIVE_ROWS_PER_LIMITER * STORAGE_CEILING_FACTOR
                ):
                    db.commit()
                    return None
            row = db.execute(
                text(
                    "INSERT INTO shared_rate_limit_hits (limiter, identity, hit_at) "
                    "SELECT CAST(:l AS VARCHAR), CAST(:i AS VARCHAR), "
                    "CAST(:now AS DOUBLE PRECISION) "
                    "WHERE (SELECT count(*) FROM shared_rate_limit_hits "
                    "WHERE limiter = :l AND identity = :i AND hit_at >= :cutoff) < :max "
                    "RETURNING id"
                ),
                {"l": limiter, "i": digest, "now": moment, "cutoff": cutoff, "max": max_requests},
            ).first()
            db.commit()
            return None if row is None else int(row[0])
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None


def rate_refund(limiter: str, identity: str, hit_id: int) -> None:
    """Undo exactly the hit that `rate_hit` returned for this limiter AND identity (a successful
    authentication). The token alone is not trusted: it must still belong to the same client."""
    try:
        with _session() as db:
            db.execute(
                text(
                    "DELETE FROM shared_rate_limit_hits "
                    "WHERE id = :id AND limiter = :l AND identity = :i"
                ),
                {"id": hit_id, "l": limiter, "i": identity_hash(identity)},
            )
            db.commit()
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None


def rate_clear(limiter: str | None = None) -> None:
    """Test helper: forget the recorded hits of one limiter (or all)."""
    try:
        with _session() as db:
            if limiter is None:
                db.execute(text("DELETE FROM shared_rate_limit_hits"))
            else:
                db.execute(
                    text("DELETE FROM shared_rate_limit_hits WHERE limiter = :l"), {"l": limiter}
                )
            db.commit()
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None


# ----------------------------------------------------------- session revocations


def session_revoke(sid: str, expiry: int, *, capacity: int, now: float | None = None) -> bool:
    """Record a logout until the cookie's own expiry. False when the table is full of
    unexpired revocations (the caller then reports the logout as failed)."""
    moment = time.time() if now is None else now
    try:
        with _session() as db:
            _advisory_lock(db, "ui-session-revocations")
            db.execute(
                text("DELETE FROM shared_ui_session_revocations WHERE expires_at <= :now"),
                {"now": int(moment)},
            )
            known = db.execute(
                text("SELECT 1 FROM shared_ui_session_revocations WHERE sid = :sid"), {"sid": sid}
            ).first()
            if known is not None:
                db.commit()
                return True
            live = db.execute(
                text("SELECT count(*) FROM shared_ui_session_revocations")
            ).scalar_one()
            if live >= capacity:
                db.commit()
                return False
            db.execute(
                text(
                    "INSERT INTO shared_ui_session_revocations (sid, expires_at) "
                    "VALUES (:sid, :expiry) ON CONFLICT (sid) DO NOTHING"
                ),
                {"sid": sid, "expiry": int(expiry)},
            )
            db.commit()
            return True
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None


def session_is_revoked(sid: str) -> bool:
    try:
        with _session() as db:
            return (
                db.execute(
                    text("SELECT 1 FROM shared_ui_session_revocations WHERE sid = :sid"),
                    {"sid": sid},
                ).first()
                is not None
            )
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None


def session_clear() -> None:
    """Test helper."""
    try:
        with _session() as db:
            db.execute(text("DELETE FROM shared_ui_session_revocations"))
            db.commit()
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None
