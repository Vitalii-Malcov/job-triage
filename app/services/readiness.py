"""Stage 13A operational readiness -- GET /api/v1/ready.

`/health` stays a cheap process-liveness check that never touches the
database. Readiness is the separate, stricter signal the Docker healthcheck
uses: it succeeds only when the database can be reached, a read-only query
succeeds, and the database's Alembic revision is EXACTLY the application's
single expected head. `ALEMBIC_AUTO_UPGRADE` is off in production, so a
schema behind (or ahead of) the code is a real "not ready" state that this
check reports instead of repairing -- it never migrates, creates, or writes.

**Bounded end to end (Codex P2-05).** The Docker healthcheck times out after
5 seconds, and the request engine's own settings are far looser
(`connect_timeout=10` per resolved address, a 30 s QueuePool checkout
timeout). Wrapping those in an HTTP-level timeout would leave the check
itself still running after the response. Instead readiness uses its OWN
engine, built once, with every stage bounded at the driver level:

- pool acquisition: `NullPool` -- no shared pool, so readiness never queues
  behind (or exhausts) the request pool; each check opens and closes one
  connection.
- connection establishment: PostgreSQL `connect_timeout=2` (libpq's minimum
  effective value), plus `tcp_user_timeout`/keepalives so a peer that dies
  mid-check is detected in ~2 s instead of the OS's minutes-long default.
- the query: PostgreSQL `statement_timeout=1500` ms set in the connection
  startup options, so even a lock wait (e.g. a migration holding ACCESS
  EXCLUSIVE on `alembic_version`) ends server-side.

Worst case against the supported compose PostgreSQL (one resolved address
for the `db` host): ~2 s connect + ~1.5 s query, under the 5 s healthcheck
timeout. A hostname resolving to several addresses multiplies the connect
bound per address (libpq behaviour, see app.db.session's HARD-011 note).

SQLite (dev/pilot) is opened read-only via a `mode=ro` URI with a 1 s busy
timeout: a missing file is never created and a writer holding the lock
cannot stall the check beyond that.
"""

import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

POSTGRES_CONNECT_TIMEOUT_SECONDS = 2
POSTGRES_STATEMENT_TIMEOUT_MS = 1500
POSTGRES_TCP_USER_TIMEOUT_MS = 1500
SQLITE_BUSY_TIMEOUT_SECONDS = 1.0


@lru_cache
def expected_alembic_heads() -> tuple[str, ...]:
    """The application's migration-graph heads. Cached: migration files
    cannot change inside a running container. Readiness requires exactly
    one -- a multi-head graph fails closed rather than picking one."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    return tuple(script.get_heads())


def readiness_url(database_url: str) -> tuple[URL, dict]:
    """`(url, connect_args)` for the readiness engine -- same database as the
    request engine, opened with the bounds described in the module docstring.
    """
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        database = url.database
        if not database or database == ":memory:":
            # Nothing persistent to check; mode=ro on an in-memory db cannot
            # see the request engine's schema, so this reports not ready.
            return url, {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
        return (
            url.set(
                database=Path(database).resolve().as_uri(), query={"mode": "ro", "uri": "true"}
            ),
            {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS, "check_same_thread": False},
        )
    return url, {
        "connect_timeout": POSTGRES_CONNECT_TIMEOUT_SECONDS,
        "options": f"-c statement_timeout={POSTGRES_STATEMENT_TIMEOUT_MS}",
        "tcp_user_timeout": POSTGRES_TCP_USER_TIMEOUT_MS,
        "keepalives": 1,
        "keepalives_idle": 1,
        "keepalives_interval": 1,
        "keepalives_count": 1,
    }


def build_readiness_engine(database_url: str) -> Engine:
    url, connect_args = readiness_url(database_url)
    return create_engine(url, connect_args=connect_args, poolclass=NullPool)


@lru_cache
def _default_readiness_engine() -> Engine:
    return build_readiness_engine(get_settings().database_url)


def check_readiness(engine: Engine | None = None) -> bool:
    """True only if the database answers a read-only query and holds exactly
    one Alembic revision equal to the application's single head. Never raises:
    every failure is logged by category (never the exception text, which can
    carry connection details) and reported as not ready.

    Stage 13B (Stage 13A P3): resolving the application's heads and building
    the default readiness engine are inside the same fail-closed contract --
    an unexpected setup failure (unreadable migration scripts, a malformed
    DATABASE_URL, a missing driver) is a sanitized "not ready", never an
    unhandled exception that would surface as a generic HTTP 500."""
    try:
        heads = expected_alembic_heads()
    except Exception as exc:  # noqa: BLE001 -- any failure means "not ready"
        logger.error(
            "readiness_not_ready reason=application_head_unavailable error_type=%s",
            type(exc).__name__,
        )
        return False
    if len(heads) != 1:
        logger.error("readiness_not_ready reason=application_head_count heads=%d", len(heads))
        return False

    try:
        engine = engine if engine is not None else _default_readiness_engine()
    except Exception as exc:  # noqa: BLE001 -- any failure means "not ready"
        logger.error(
            "readiness_not_ready reason=engine_unavailable error_type=%s", type(exc).__name__
        )
        return False
    try:
        with engine.connect() as conn:
            revisions = [
                row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            ]
    except Exception as exc:  # noqa: BLE001 -- any failure means "not ready"
        logger.warning(
            "readiness_not_ready reason=database_unavailable error_type=%s", type(exc).__name__
        )
        return False

    if revisions != [heads[0]]:
        logger.warning(
            "readiness_not_ready reason=schema_mismatch revision_rows=%d", len(revisions)
        )
        return False
    return True
