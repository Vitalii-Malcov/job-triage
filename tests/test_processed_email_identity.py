"""Review 2.1: processed_email_messages identity is (source, message_id)."""

import threading
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import ProcessedEmailMessage
from app.db.repositories import is_message_processed, mark_message_processed

MID = "<abc123@mail.example>"


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'processed.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _count(db) -> int:
    return db.scalar(select(func.count()).select_from(ProcessedEmailMessage))


def test_two_sources_may_hold_the_same_message_id(factory):
    with factory() as db:
        mark_message_processed(db, "xing", MID)
        mark_message_processed(db, "other", MID)
        assert is_message_processed(db, "xing", MID)
        assert is_message_processed(db, "other", MID)
        assert not is_message_processed(db, "third", MID)
        assert _count(db) == 2


def test_same_source_twice_is_idempotent_and_keeps_the_first_row(factory):
    with factory() as db:
        mark_message_processed(db, "xing", MID)
        first = db.execute(
            select(ProcessedEmailMessage.id, ProcessedEmailMessage.processed_at)
        ).one()
        mark_message_processed(db, "xing", MID)
        again = db.execute(
            select(ProcessedEmailMessage.id, ProcessedEmailMessage.processed_at)
        ).one()
        assert tuple(first) == tuple(again)


def test_a_stale_check_cannot_make_the_insert_raise(factory, monkeypatch):
    """The check-then-insert window: another session inserts between the
    existence check and the INSERT. The write must still succeed silently."""
    from app.db import repositories

    with factory() as other:
        real_check = repositories.is_message_processed

        def racing_check(db, source, message_id):
            answer = real_check(db, source, message_id)
            other.add(ProcessedEmailMessage(source=source, message_id=message_id))
            other.commit()  # the rival wins the race
            return answer

        monkeypatch.setattr(repositories, "is_message_processed", racing_check)
        with factory() as db:
            repositories.mark_message_processed(db, "xing", MID)  # must not raise
            monkeypatch.setattr(repositories, "is_message_processed", real_check)
            assert _count(db) == 1


def test_concurrent_marks_converge_on_one_row(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    barrier = threading.Barrier(8, timeout=30)
    errors: list[BaseException] = []

    def worker():
        try:
            with sessions() as db:
                barrier.wait()
                mark_message_processed(db, "xing", MID)
                assert is_message_processed(db, "xing", MID)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    with sessions() as db:
        assert _count(db) == 1
    engine.dispose()


def test_model_declares_the_composite_identity_only():
    table = ProcessedEmailMessage.__table__
    uniques = [
        (c.name, sorted(col.name for col in c.columns))
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("uq_processed_email_messages_source_message_id", ["message_id", "source"]) in uniques
    assert table.c.message_id.unique is not True
    assert all(not ix.unique for ix in table.indexes)


def _cfg(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _indexes(engine):
    return {
        ix["name"]: ix["unique"] for ix in inspect(engine).get_indexes("processed_email_messages")
    }


def test_migration_round_trip_keeps_rows_and_swaps_the_identity(tmp_path, repository_head):
    db_path = tmp_path / "mig.db"
    cfg = _cfg(db_path)
    upgrade(cfg, "f1a8c3d5e7b9")
    engine = create_engine(f"sqlite:///{db_path}")
    assert _indexes(engine)["ix_processed_email_messages_message_id"]
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO processed_email_messages (source, message_id, processed_at) "
                "VALUES ('xing', '<a@x>', CURRENT_TIMESTAMP), ('xing', '<b@x>', CURRENT_TIMESTAMP)"
            )
        )

    upgrade(cfg, "head")
    assert not _indexes(engine)["ix_processed_email_messages_message_id"]
    uniques = inspect(engine).get_unique_constraints("processed_email_messages")
    assert any(u["column_names"] == ["source", "message_id"] for u in uniques)
    with engine.begin() as c:  # a second source with an existing Message-ID is now legal
        c.execute(
            text(
                "INSERT INTO processed_email_messages (source, message_id, processed_at) "
                "VALUES ('other', '<a@x>', CURRENT_TIMESTAMP)"
            )
        )
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM processed_email_messages")).scalar() == 3

    # downgrade is refused BEFORE any DDL while a cross-source duplicate exists
    with pytest.raises(RuntimeError, match="more than one source"):
        downgrade(cfg, "f1a8c3d5e7b9")
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar() == repository_head
        )
        assert c.execute(text("SELECT COUNT(*) FROM processed_email_messages")).scalar() == 3
    assert not _indexes(engine)["ix_processed_email_messages_message_id"]
    assert not any(t.startswith("_alembic_tmp") for t in inspect(engine).get_table_names())

    with engine.begin() as c:
        c.execute(text("DELETE FROM processed_email_messages WHERE source = 'other'"))
    downgrade(cfg, "f1a8c3d5e7b9")
    assert _indexes(engine)["ix_processed_email_messages_message_id"]
    assert not any(
        u["column_names"] == ["source", "message_id"]
        for u in inspect(engine).get_unique_constraints("processed_email_messages")
    )
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM processed_email_messages")).scalar() == 2
    upgrade(cfg, "head")
    assert not _indexes(engine)["ix_processed_email_messages_message_id"]
