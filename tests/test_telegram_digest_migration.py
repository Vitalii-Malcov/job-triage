"""Stage 8E dialect-compatibility proof for `TelegramDigestDeliveryRecord`
and its CAS UPDATEs -- mirrors
tests/test_automation_schedule_migration.py's pure DDL-compilation
approach (no real PostgreSQL connection/driver required). Like
`AutomationScheduleRecord.account_key`, the identity here
(`UNIQUE(account_key, digest_date)`) is a plain `UniqueConstraint` and
every state transition is a plain conditional `UPDATE ... WHERE` -- no
dialect-specific predicate, so there is nothing SQLite-only to keep in
sync in the first place. These tests exist to prove that claim stays
true.
"""

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.dialects import postgresql, sqlite

from app.db.models import TelegramDigestDeliveryRecord


def test_account_date_unique_constraint_is_a_plain_portable_constraint():
    (constraint,) = [
        uc
        for uc in TelegramDigestDeliveryRecord.__table__.constraints
        if getattr(uc, "name", None) == "uq_telegram_digest_deliveries_account_date"
    ]
    assert [col.name for col in constraint.columns] == ["account_key", "digest_date"]


def _mark_sent_statement():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return (
        update(TelegramDigestDeliveryRecord)
        .where(
            TelegramDigestDeliveryRecord.id == 1,
            TelegramDigestDeliveryRecord.status == "PENDING",
        )
        .values(status="SENT", sent_at=now, updated_at=now)
    )


def test_mark_sent_update_compiles_on_sqlite():
    compiled = str(_mark_sent_statement().compile(dialect=sqlite.dialect()))
    assert "UPDATE telegram_digest_deliveries" in compiled
    assert "status" in compiled


def test_mark_sent_update_compiles_on_postgresql():
    compiled = str(_mark_sent_statement().compile(dialect=postgresql.dialect()))
    assert "UPDATE telegram_digest_deliveries" in compiled
    assert "status" in compiled


def test_mark_sent_update_never_uses_a_sqlite_only_construct():
    stmt = _mark_sent_statement()
    sqlite_ddl = str(stmt.compile(dialect=sqlite.dialect()))
    postgresql_ddl = str(stmt.compile(dialect=postgresql.dialect()))
    for forbidden in ("PRAGMA", "sqlite_", "ON CONFLICT", "INSERT OR"):
        assert forbidden not in sqlite_ddl
        assert forbidden not in postgresql_ddl


def test_status_check_constraint_lists_all_four_valid_states():
    (constraint,) = [
        cc
        for cc in TelegramDigestDeliveryRecord.__table__.constraints
        if getattr(cc, "name", None) == "ck_telegram_digest_deliveries_status_valid"
    ]
    sqltext = str(constraint.sqltext)
    for status in ("PENDING", "SENT", "FAILED", "UNCERTAIN"):
        assert status in sqltext
