from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _connect_args_for(database_url: str) -> dict:
    """Pure so it's directly unit-testable for both dialects (see
    tests/test_db_session_connect_args.py) without needing to reimport
    this module with a different DATABASE_URL.
    """
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    # HARD-011 (adversarial hardening r1): without an explicit
    # connect_timeout, a connection attempt to a host that isn't
    # actively refusing (DB process stopped but the port/network path
    # still accepts a TCP handshake attempt at the OS/container level,
    # or a firewall silently drops packets) blocks on the OS's own TCP
    # connect timeout -- measured live during this hardening pass to
    # exceed 60+ seconds with nothing bounding it. That turned a DB
    # outage into an indefinitely hung request thread instead of a
    # fast, clear error. A 10s-per-address bound converts that into a
    # clean, finite OperationalError. Note: libpq/psycopg apply this
    # PER resolved address, not once overall -- a hostname resolving to
    # both an IPv6 and an IPv4 address (e.g. "localhost") can still take
    # up to ~2x this value before failing, confirmed live during this
    # hardening pass (~20s observed, not 10s) -- still a bounded,
    # diagnosable failure instead of an indefinite hang, which is the
    # actual fix; it is not a promise of an exact wall-clock number.
    # `pool_pre_ping` (below) bounds STALE pooled connections; this
    # bounds the separate "can't connect at all" case pre_ping alone
    # doesn't cover, since pre_ping's own health-check query opens a
    # connection the same unbounded way if none exists yet. See
    # docs/ADVERSARIAL_HARDENING_REPORT.md HARD-011.
    return {"connect_timeout": 10}


settings = get_settings()
connect_args = _connect_args_for(settings.database_url)
# AUD-007: pool_pre_ping issues a lightweight "is this connection still
# alive" check before handing a pooled connection to a request, so a
# connection that went stale while idle (DB restart, firewall/load-
# balancer idle timeout, managed-Postgres connection recycling) is
# transparently detected and replaced instead of surfacing as an
# unhandled OperationalError on the next query. A no-op for SQLite (no
# such failure mode there), harmless to set unconditionally.
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_migrations_if_enabled() -> None:
    """Optionally apply Alembic migrations on startup.

    Disabled by default (Settings.alembic_auto_upgrade=False). This is meant
    for local dev/tests convenience only. Production deployments must run
    `alembic upgrade head` explicitly (manually or in CI/CD) before starting
    the app — schema changes should never happen implicitly on boot there.
    """
    if not settings.alembic_auto_upgrade:
        return

    from alembic.command import upgrade
    from alembic.config import Config

    alembic_cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    upgrade(alembic_cfg, "head")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
