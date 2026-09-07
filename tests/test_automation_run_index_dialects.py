"""S8A-001 (Codex re-review): `AutomationRunRecord`'s partial UNIQUE index
(`uq_automation_runs_one_running_per_account`) must describe EQUIVALENT
semantics on every dialect this project claims to support — SQLite
(today's deployment target) and PostgreSQL (documented as supported via
the `.[postgres]` extra in pyproject.toml, see app/core/config.py's
`database_url` docstring). This is a pure DDL-compilation test — it never
opens a real PostgreSQL connection or requires psycopg2/any driver to be
installed, since `CreateIndex(...).compile(dialect=...)` only needs the
dialect's SQL-generation rules, not a live connection.

If a future change to `app.db.models.AutomationRunRecord`'s `Index(...)`
call ever drops (or desyncs) one of `sqlite_where`/`postgresql_where`,
this test fails loudly rather than silently shipping a partial index that
only actually enforces "at most one RUNNING run per account" on ONE of
the two supported databases.
"""

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex

from app.db.models import AutomationRunRecord


def _the_partial_unique_index():
    (index,) = [
        idx
        for idx in AutomationRunRecord.__table__.indexes
        if idx.name == "uq_automation_runs_one_running_per_account"
    ]
    return index


def test_index_is_unique_on_account_key():
    index = _the_partial_unique_index()
    assert index.unique is True
    assert [col.name for col in index.columns] == ["account_key"]


def test_sqlite_compiles_the_partial_where_clause():
    index = _the_partial_unique_index()
    ddl = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
    assert "UNIQUE" in ddl
    assert "account_key" in ddl
    assert "WHERE status = 'RUNNING'" in ddl


def test_postgresql_compiles_the_same_partial_where_clause():
    index = _the_partial_unique_index()
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "UNIQUE" in ddl
    assert "account_key" in ddl
    assert "WHERE status = 'RUNNING'" in ddl


def test_sqlite_and_postgresql_predicates_are_textually_identical():
    """Not just "both have a WHERE clause" -- the actual predicate text
    must match, so the two dialects can never silently drift into
    enforcing different conditions."""
    index = _the_partial_unique_index()
    sqlite_ddl = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
    postgresql_ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    def _where_clause(ddl: str) -> str:
        return ddl.split("WHERE", 1)[1].strip()

    assert _where_clause(sqlite_ddl) == _where_clause(postgresql_ddl) == "status = 'RUNNING'"
