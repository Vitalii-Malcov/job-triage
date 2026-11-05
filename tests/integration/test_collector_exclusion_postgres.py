"""Review 2.3 on real PostgreSQL: concurrent collector entry points exclude each
other through the shared RUNNING slot (partial unique index), and exactly one
contender wins while the winner still holds it.

Runs at head (no Alembic cycling). Skipped unless `TEST_POSTGRES_URL` is set
(CI's zero-skip gate sets it); run `alembic upgrade head` against that database
first. Test-owned rows (account key below) are deleted before and after.
"""

import asyncio
import os
import threading

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.models import AutomationRunRecord
from app.services.collector_exclusion import CollectorRunInProgressError, run_collector_exclusively

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL not set")

ACCOUNT = "r23-exclusion-test@example.com"
STATS = {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}


@pytest.fixture()
def factory():
    engine = create_engine(POSTGRES_URL)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def cleanup():
        with session_factory() as db:
            db.execute(
                delete(AutomationRunRecord).where(AutomationRunRecord.account_key == ACCOUNT)
            )
            db.commit()

    cleanup()
    try:
        yield session_factory
    finally:
        cleanup()
        engine.dispose()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        gmail_username=ACCOUNT,
        xing_mailbox_username="xing@example.com",
        xing_mailbox_app_password="pw",
        bundesagentur_api_key="ba",
    )


def test_exactly_one_of_many_concurrent_manual_runs_wins(factory):
    holding = threading.Event()  # the winner is inside its run
    release = threading.Event()  # let the winner finish
    barrier = threading.Barrier(6, timeout=30)
    outcomes: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    async def hold(db, settings, *, is_lease_lost=None):
        holding.set()
        while not release.is_set():
            await asyncio.sleep(0.05)
        return STATS

    def contender():
        try:
            with factory() as db:
                barrier.wait()
                try:
                    asyncio.run(run_collector_exclusively(db, _settings(), "xing", hold))
                    result = "won"
                except CollectorRunInProgressError:
                    result = "refused"
            with guard:
                outcomes.append(result)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=contender) for _ in range(6)]
    for t in threads:
        t.start()
    assert holding.wait(timeout=30)
    # while the winner still holds the slot, every other contender is refused
    deadline_threads = [t for t in threads]
    for t in deadline_threads:
        t.join(timeout=0.5)
    with guard:
        refused_while_held = outcomes.count("refused")
    release.set()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert sorted(outcomes) == ["refused"] * 5 + ["won"]
    assert refused_while_held == 5
    with factory() as db:
        rows = db.scalars(
            select(AutomationRunRecord).where(AutomationRunRecord.account_key == ACCOUNT)
        ).all()
        assert [r.status for r in rows if r.status == "RUNNING"] == []
        assert sum(1 for r in rows if r.status == "COMPLETED") == 1


def test_slot_is_free_again_after_the_winner_finishes(factory):
    async def quick(db, settings, *, is_lease_lost=None):
        return STATS

    for _ in range(2):
        with factory() as db:
            assert asyncio.run(run_collector_exclusively(db, _settings(), "bundesagentur", quick))
