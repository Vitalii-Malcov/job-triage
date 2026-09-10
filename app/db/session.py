from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
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
