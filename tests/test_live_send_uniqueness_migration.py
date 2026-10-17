"""Stage 12 migration a7c3e9f1b2d4: partial unique indexes enforcing at most
one live (PENDING/SENT/UNCERTAIN) send per inbound message
(`response_draft_sends`) and per follow-up anchor (`follow_up_sends`).

SQLite here (the project's deployment database);
tests/integration/test_live_send_uniqueness_postgres.py proves the same
index under real PostgreSQL concurrency.
"""

from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from alembic.ddl.impl import DefaultImpl
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVISION = "a7c3e9f1b2d4"
PREVIOUS = "e8b1c4f7a2d9"
TABLES = {
    "response_draft_sends": ("uq_response_draft_sends_live_per_message", "response_draft_id"),
    "follow_up_sends": ("uq_follow_up_sends_live_per_anchor", "follow_up_proposal_id"),
}


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _insert_send(
    conn,
    table: str,
    *,
    target_id: int,
    gmail_message_id: int,
    status: str,
    account="me@example.com",
):
    owner_column = TABLES[table][1]
    conn.execute(
        text(
            f"INSERT INTO {table} (account_key, {owner_column}, approval_id, gmail_message_id, "
            "status, attempt_count, send_attempted, created_at, updated_at) "
            "VALUES (:account, :target, 1, :msg, :status, 1, 0, "
            "'2026-10-13 00:00:00', '2026-10-13 00:00:00')"
        ),
        {"account": account, "target": target_id, "msg": gmail_message_id, "status": status},
    )


def _index_names(engine, table: str) -> set[str]:
    return {index["name"] for index in inspect(engine).get_indexes(table)}


def test_migration_is_the_single_head_and_chains_from_previous() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg(Path("unused.db")))
    # Stage 15 added b5d2e8f4a1c7 on top; this revision must stay on the
    # single head's ancestry, chained from PREVIOUS.
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION in {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_upgrade_creates_both_partial_unique_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "live_send.db"
    upgrade(_cfg(db_path), REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        for table, (index_name, _owner) in TABLES.items():
            assert index_name in _index_names(engine, table)
            sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"),
                {"n": index_name},
            ).scalar_one()
            assert "UNIQUE" in sql.upper()
            assert "account_key" in sql and "gmail_message_id" in sql
            assert "status IN ('PENDING', 'SENT', 'UNCERTAIN')" in sql


@pytest.mark.parametrize("table", sorted(TABLES))
def test_index_rejects_a_second_live_row_for_the_same_target(tmp_path: Path, table) -> None:
    db_path = tmp_path / "live_send.db"
    upgrade(_cfg(db_path), REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _insert_send(conn, table, target_id=1, gmail_message_id=10, status="SENT")
    for status in ("PENDING", "SENT", "UNCERTAIN"):
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            with engine.begin() as conn:
                _insert_send(conn, table, target_id=2, gmail_message_id=10, status=status)


@pytest.mark.parametrize("table", sorted(TABLES))
def test_index_allows_failed_rows_other_targets_and_other_accounts(tmp_path: Path, table) -> None:
    db_path = tmp_path / "live_send.db"
    upgrade(_cfg(db_path), REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _insert_send(conn, table, target_id=1, gmail_message_id=10, status="FAILED")
        _insert_send(conn, table, target_id=2, gmail_message_id=10, status="FAILED")
        _insert_send(conn, table, target_id=3, gmail_message_id=10, status="PENDING")
        _insert_send(conn, table, target_id=4, gmail_message_id=11, status="SENT")
        _insert_send(
            conn,
            table,
            target_id=5,
            gmail_message_id=10,
            status="SENT",
            account="other@example.com",
        )
        count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    assert count == 5


@pytest.mark.parametrize("table", sorted(TABLES))
def test_upgrade_refuses_existing_live_duplicates_without_touching_data(
    tmp_path: Path, table
) -> None:
    db_path = tmp_path / "live_send.db"
    cfg = _cfg(db_path)
    upgrade(cfg, PREVIOUS)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _insert_send(conn, table, target_id=1, gmail_message_id=10, status="SENT")
        _insert_send(conn, table, target_id=2, gmail_message_id=10, status="UNCERTAIN")

    with pytest.raises(RuntimeError) as excinfo:
        upgrade(cfg, REVISION)
    message = str(excinfo.value)
    assert table in message
    assert "me@example.com" not in message  # counts only, never identities

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            PREVIOUS
        )
        assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 2
    for other_table, (index_name, _owner) in TABLES.items():
        assert index_name not in _index_names(engine, other_table)


def test_upgrade_accepts_existing_failed_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "live_send.db"
    cfg = _cfg(db_path)
    upgrade(cfg, PREVIOUS)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for table in TABLES:
            _insert_send(conn, table, target_id=1, gmail_message_id=10, status="FAILED")
            _insert_send(conn, table, target_id=2, gmail_message_id=10, status="SENT")
    upgrade(cfg, REVISION)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            REVISION
        )


def test_downgrade_drops_indexes_and_keeps_rows_then_upgrade_restores(tmp_path: Path) -> None:
    db_path = tmp_path / "live_send.db"
    cfg = _cfg(db_path)
    upgrade(cfg, REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for table in TABLES:
            _insert_send(conn, table, target_id=1, gmail_message_id=10, status="SENT")

    downgrade(cfg, PREVIOUS)
    for table, (index_name, _owner) in TABLES.items():
        assert index_name not in _index_names(engine, table)
    with engine.connect() as conn:
        for table in TABLES:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1

    upgrade(cfg, REVISION)
    for table, (index_name, _owner) in TABLES.items():
        assert index_name in _index_names(engine, table)


FIRST_INDEX, SECOND_INDEX = (
    "uq_response_draft_sends_live_per_message",
    "uq_follow_up_sends_live_per_anchor",
)


def _fail_creating(monkeypatch, index_name: str) -> None:
    """Make Alembic's DDL layer fail when (and only when) it creates
    `index_name` -- after any index created before it really exists."""
    original = DefaultImpl.create_index

    def create_index(self, index, **kw):
        if index.name == index_name:
            raise sqlalchemy.exc.OperationalError("CREATE INDEX", {}, Exception("injected"))
        return original(self, index, **kw)

    monkeypatch.setattr(DefaultImpl, "create_index", create_index)


def _revision(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_second_index_failure_leaves_no_partial_state_and_retry_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    """S12-002 (Codex review): SQLite DDL is not transactional, so the first
    index would otherwise survive the failed upgrade and block every retry."""
    db_path = tmp_path / "live_send.db"
    cfg = _cfg(db_path)
    upgrade(cfg, PREVIOUS)
    engine = create_engine(f"sqlite:///{db_path}")

    with monkeypatch.context() as patched:
        _fail_creating(patched, SECOND_INDEX)
        with pytest.raises(sqlalchemy.exc.OperationalError):
            upgrade(cfg, REVISION)

    assert _revision(engine) == PREVIOUS
    for table, (index_name, _owner) in TABLES.items():
        assert index_name not in _index_names(engine, table)

    upgrade(cfg, REVISION)  # injected failure removed
    assert _revision(engine) == REVISION
    for table, (index_name, _owner) in TABLES.items():
        assert index_name in _index_names(engine, table)


def test_failed_upgrade_never_drops_an_index_that_existed_before_it(tmp_path: Path) -> None:
    db_path = tmp_path / "live_send.db"
    cfg = _cfg(db_path)
    upgrade(cfg, PREVIOUS)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(f"CREATE INDEX {SECOND_INDEX} ON follow_up_sends (account_key, gmail_message_id)")
        )

    with pytest.raises(sqlalchemy.exc.OperationalError):
        upgrade(cfg, REVISION)  # second CREATE hits "already exists"

    assert _revision(engine) == PREVIOUS
    assert FIRST_INDEX not in _index_names(engine, "response_draft_sends")
    assert SECOND_INDEX in _index_names(engine, "follow_up_sends")  # pre-existing, kept


def test_cleanup_failure_is_reported_never_swallowed(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "live_send.db"
    cfg = _cfg(db_path)
    upgrade(cfg, PREVIOUS)
    engine = create_engine(f"sqlite:///{db_path}")

    def drop_index(self, index, **kw):
        raise sqlalchemy.exc.OperationalError("DROP INDEX", {}, Exception("injected"))

    with monkeypatch.context() as patched:
        _fail_creating(patched, SECOND_INDEX)
        patched.setattr(DefaultImpl, "drop_index", drop_index)
        with pytest.raises(RuntimeError, match=FIRST_INDEX):
            upgrade(cfg, REVISION)

    assert _revision(engine) == PREVIOUS


def test_models_declare_the_same_partial_indexes_as_the_migration() -> None:
    """Model/migration parity: the ORM metadata (used by create_all in the
    SQLite test suites) and the migration must describe the same index."""
    import importlib.util

    from app.db.models import (
        LIVE_SEND_STATUS_PREDICATE,
        FollowUpSendRecord,
        ResponseDraftSendRecord,
    )

    spec = importlib.util.spec_from_file_location(
        "live_send_migration",
        PROJECT_ROOT / "alembic" / "versions" / f"{REVISION}_live_send_uniqueness_per_target.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration._LIVE_PREDICATE == LIVE_SEND_STATUS_PREDICATE

    for model, (index_name, _owner) in (
        (ResponseDraftSendRecord, TABLES["response_draft_sends"]),
        (FollowUpSendRecord, TABLES["follow_up_sends"]),
    ):
        (index,) = [i for i in model.__table__.indexes if i.name == index_name]
        assert index.unique is True
        assert [c.name for c in index.columns] == ["account_key", "gmail_message_id"]
        for dialect in ("sqlite", "postgresql"):
            where = index.dialect_options[dialect]["where"]
            assert str(where) == LIVE_SEND_STATUS_PREDICATE
        assert migration._INDEXES[index_name] == model.__tablename__
