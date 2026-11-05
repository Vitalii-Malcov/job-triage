from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session_engine import enable_sqlite_foreign_keys


def _connect_args_for(database_url: str) -> dict:
    """Pure so it's directly unit-testable for both dialects (see
    tests/test_db_session_connect_args.py) without needing to reimport
    this module with a different DATABASE_URL.
    """
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    # PostgreSQL (psycopg/libpq) is the only other supported dialect, so a libpq-specific
    # option is safe here; a third dialect would need this branch to become dialect-aware.
    #
    # Without `connect_timeout`, connecting to a host that silently drops packets blocks on
    # the OS TCP timeout (60+ s observed), turning a DB outage into a hung request thread.
    # 10 s is applied PER resolved address by libpq, so "localhost" (IPv6 + IPv4) can take
    # about twice that -- still a bounded, diagnosable failure. `pool_pre_ping` (below) covers
    # stale pooled connections; this covers "cannot connect at all". See
    # docs/ADVERSARIAL_HARDENING_REPORT.md HARD-011.
    return {"connect_timeout": 10}


settings = get_settings()
connect_args = _connect_args_for(settings.database_url)
# pool_pre_ping issues a lightweight "is this connection still
# alive" check before handing a pooled connection to a request, so a
# connection that went stale while idle (DB restart, firewall/load-
# balancer idle timeout, managed-Postgres connection recycling) is
# transparently detected and replaced instead of surfacing as an
# unhandled OperationalError on the next query. A no-op for SQLite (no
# such failure mode there), harmless to set unconditionally.
engine = enable_sqlite_foreign_keys(
    create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
)
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
