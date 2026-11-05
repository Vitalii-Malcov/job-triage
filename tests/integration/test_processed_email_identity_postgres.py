"""Review 2.1 on real PostgreSQL: `mark_message_processed` is race-free and
the (source, message_id) identity allows the same Message-ID for two sources.

Runs at head (no Alembic cycling). Skipped unless `TEST_POSTGRES_URL` is set
(CI's zero-skip gate sets it); run `alembic upgrade head` against that
database first. Test-owned markers are deleted in `finally`.
"""

import os
import threading

import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models import ProcessedEmailMessage
from app.db.repositories import is_message_processed, mark_message_processed

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL not set")

SOURCES = ("r21-test-a", "r21-test-b")
MID = "<review-2-1@mail.example>"


@pytest.fixture()
def sessions():
    engine = create_engine(POSTGRES_URL)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def cleanup():
        with factory() as db:
            db.execute(
                delete(ProcessedEmailMessage).where(ProcessedEmailMessage.source.in_(SOURCES))
            )
            db.commit()

    cleanup()
    try:
        yield factory
    finally:
        cleanup()
        engine.dispose()


def _count(db, source) -> int:
    return db.scalar(
        select(func.count())
        .select_from(ProcessedEmailMessage)
        .where(ProcessedEmailMessage.source == source, ProcessedEmailMessage.message_id == MID)
    )


def test_same_message_id_for_two_sources(sessions):
    with sessions() as db:
        mark_message_processed(db, SOURCES[0], MID)
        mark_message_processed(db, SOURCES[1], MID)
        assert is_message_processed(db, SOURCES[0], MID) and is_message_processed(
            db, SOURCES[1], MID
        )


def test_many_concurrent_marks_yield_one_row_and_no_errors(sessions):
    barrier = threading.Barrier(12, timeout=30)
    errors: list[BaseException] = []

    def worker():
        try:
            with sessions() as db:
                barrier.wait()
                mark_message_processed(db, SOURCES[0], MID)
                # the session stays usable with no caller-side rollback
                assert is_message_processed(db, SOURCES[0], MID)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    with sessions() as db:
        assert _count(db, SOURCES[0]) == 1


def test_overlap_with_an_uncommitted_competing_insert(sessions):
    """A competing transaction has INSERTed (uncommitted) the same key; the
    marker call blocks on it, then converges without raising once it commits."""
    holder = sessions()
    holder.execute(
        text(
            "INSERT INTO processed_email_messages (source, message_id, processed_at) "
            "VALUES (:s, :m, now())"
        ),
        {"s": SOURCES[0], "m": MID},
    )
    outcome: list[object] = []

    def contender():
        try:
            with sessions() as db:
                mark_message_processed(db, SOURCES[0], MID)
                outcome.append("ok")
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=contender)
    thread.start()
    try:
        thread.join(timeout=2)
        assert thread.is_alive(), "the contender should be waiting on the uncommitted row"
    finally:
        # Always release the competing transaction (its locks would otherwise
        # block later tests that cycle the shared schema), then reap the worker.
        holder.commit()
        holder.close()
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert outcome == ["ok"]
    with sessions() as db:
        assert _count(db, SOURCES[0]) == 1
