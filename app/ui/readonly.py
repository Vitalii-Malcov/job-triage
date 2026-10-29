"""Stage 20: the read-only database session every /ui page uses.

Read-only is enforced below SQLAlchemy's transaction handling, so neither a
rollback nor a pooled connection's reuse can undo it (Codex plan review P1):

- SQLite: `PRAGMA query_only = ON` is executed on the raw DBAPI connection
  when it is opened (a connection setting, not part of a transaction);
- PostgreSQL: the libpq startup option `-c default_transaction_read_only=on`
  makes every transaction on the connection read-only.

On top of that the session refuses to flush pending ORM changes
(`ReadOnlySessionError`) and is always rolled back. Any write in the call
graph -- ORM or Core DML -- therefore raises instead of committing.
"""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


class ReadOnlySessionError(RuntimeError):
    """Something tried to write through the read-only UI session."""


def _sqlite_query_only(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA query_only = ON")
    finally:
        cursor.close()


def build_readonly_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url, connect_args={"check_same_thread": False}, pool_pre_ping=True
        )
        event.listen(engine, "connect", _sqlite_query_only)
        return engine
    if database_url.startswith("postgresql"):
        return create_engine(
            database_url,
            connect_args={"connect_timeout": 10, "options": "-c default_transaction_read_only=on"},
            pool_pre_ping=True,
        )
    raise ValueError("the UI supports SQLite and PostgreSQL database URLs only")


def _refuse_flush(session: Session, _context, _instances) -> None:
    if session.new or session.dirty or session.deleted:
        raise ReadOnlySessionError("the UI session is read-only")


def readonly_sessionmaker(engine: Engine) -> sessionmaker:
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    event.listen(factory, "before_flush", _refuse_flush)
    return factory


@lru_cache(maxsize=4)
def _factory_for(database_url: str) -> sessionmaker:
    return readonly_sessionmaker(build_readonly_engine(database_url))


def get_ui_db() -> Generator[Session, None, None]:
    db = _factory_for(get_settings().database_url)()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
