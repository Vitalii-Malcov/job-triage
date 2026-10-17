"""Engine hardening shared by every project-created database engine.

Kept free of import-time side effects (unlike app/db/session.py, which builds
the application engine from settings on import) so CLIs and tests can apply
the same rules to their own engines.

**SQLite foreign keys (Codex S15 P2).** SQLite ignores FOREIGN KEY clauses,
including ON DELETE RESTRICT/CASCADE, unless every connection runs
`PRAGMA foreign_keys = ON`. Without it an `applications` row could be
deleted while its `application_events` survive as orphans -- PostgreSQL
already refuses that. The listener below makes SQLite behave like
PostgreSQL for every pooled connection. Alembic migrations keep their own
connection settings (batch table rebuilds need foreign keys off).
"""

from sqlalchemy import event
from sqlalchemy.engine import Engine


def _set_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


def enable_sqlite_foreign_keys(engine: Engine) -> Engine:
    """Turn on FK enforcement for every connection of a SQLite `engine`
    (no-op for other dialects). Idempotent."""
    if engine.dialect.name == "sqlite" and not event.contains(
        engine, "connect", _set_sqlite_foreign_keys
    ):
        event.listen(engine, "connect", _set_sqlite_foreign_keys)
    return engine
