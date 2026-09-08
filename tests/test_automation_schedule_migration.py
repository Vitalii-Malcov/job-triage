"""Stage 8B dialect-compatibility proof for `AutomationScheduleRecord`
and `claim_due_schedule`'s CAS UPDATE -- mirrors
tests/test_automation_run_index_dialects.py's pure DDL-compilation
approach (no real PostgreSQL connection/driver required -- compiling
against `sqlalchemy.dialects.postgresql.dialect()` only needs the
dialect's SQL-generation rules).

Unlike `AutomationRunRecord`'s partial unique index (S8A-001, needing an
explicit `sqlite_where`/`postgresql_where` split), `AutomationScheduleRecord.account_key`
is fully unique -- a plain `UniqueConstraint`, and `claim_due_schedule`'s
CAS is a plain conditional `UPDATE ... WHERE`, so there is no
dialect-specific predicate to keep in sync in the first place. These
tests exist to prove that claim stays true: no SQLite-only assumption
(`sqlite_where`, a SQLite-only pragma, a SQLite-only function) is ever
introduced.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.dialects import postgresql, sqlite

from app.db.models import AutomationScheduleRecord


def test_account_key_unique_constraint_is_a_plain_portable_constraint():
    (constraint,) = [
        uc
        for uc in AutomationScheduleRecord.__table__.constraints
        if getattr(uc, "name", None) == "uq_automation_schedules_account_key"
    ]
    assert [col.name for col in constraint.columns] == ["account_key"]


def _claim_update_statement():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    observed = now - timedelta(hours=1)
    new_next_run_at = now + timedelta(seconds=3600)
    return (
        update(AutomationScheduleRecord)
        .where(
            AutomationScheduleRecord.account_key == "me@example.com",
            AutomationScheduleRecord.next_run_at == observed,
            AutomationScheduleRecord.next_run_at <= now,
        )
        .values(next_run_at=new_next_run_at, last_claimed_at=now, updated_at=now)
    )


def test_claim_update_compiles_on_sqlite():
    stmt = _claim_update_statement()
    compiled = str(stmt.compile(dialect=sqlite.dialect()))
    assert "UPDATE automation_schedules" in compiled
    assert "account_key" in compiled
    assert "next_run_at" in compiled


def test_claim_update_compiles_on_postgresql():
    stmt = _claim_update_statement()
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "UPDATE automation_schedules" in compiled
    assert "account_key" in compiled
    assert "next_run_at" in compiled


def test_claim_update_never_uses_a_sqlite_only_construct():
    """No `sqlite_where`, no `PRAGMA`, no SQLite-only function -- the
    statement is built purely from portable SQLAlchemy Core constructs,
    so its compiled form must never contain SQLite-dialect-only SQL
    syntax that would silently fail (or behave differently) on
    PostgreSQL.
    """
    stmt = _claim_update_statement()
    sqlite_ddl = str(stmt.compile(dialect=sqlite.dialect()))
    postgresql_ddl = str(stmt.compile(dialect=postgresql.dialect()))
    for forbidden in ("PRAGMA", "sqlite_", "ON CONFLICT", "INSERT OR"):
        assert forbidden not in sqlite_ddl
        assert forbidden not in postgresql_ddl
