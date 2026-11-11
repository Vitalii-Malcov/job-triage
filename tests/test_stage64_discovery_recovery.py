"""Stage 64: crash recovery and idempotency of `stage22_refresh_ba --apply --create-new`.

Scenarios on a throw-away SQLite (never production): a run stopped after a partial write is
continued by the next run without duplicates, a temporarily locked database costs a vacancy
only a retry (not a duplicate or a lost row), two workers cannot write at once, and neither
a partial run nor its recovery touches Bewerbungen or their events.
"""

# ruff: noqa: F811  (the refresh tests' fixtures are imported and used as parameters)

import asyncio
import sqlite3

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError

from scripts import stage22_refresh_ba as cli
from tests.test_stage22_refresh_ba import (
    REF_A,
    REF_B,
    REF_C,
    Api,
    _env,  # noqa: F401  (autouse fixture of the refresh tests)
    db_path,  # noqa: F401  (fixture)
    posting,
    run,
    stored_job,
    table,
)


class _Detail:
    async def fetch_detail(self, ref):
        return "Python, SQL"


def _partial_create(db_path, creates, *, lose_after_checks):
    """create_jobs with a lease that is lost after N lease checks (2 per persisted vacancy:
    one in the fetch loop, one in the persist loop)."""
    calls = {"n": 0}

    def lost():
        calls["n"] += 1
        return calls["n"] > lose_after_checks

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with pytest.raises(cli.LeaseLostError) as info:
            asyncio.run(cli.create_jobs(engine, creates, _Detail(), is_lease_lost=lost))
    finally:
        engine.dispose()
    return info.value


def test_a_run_stopped_after_a_partial_write_is_continued_without_duplicates(
    db_path,
    tmp_path,
    capsys,
):
    postings = [posting(REF_A), posting(REF_B), posting(REF_C)]
    creates = [stored_job(p) for p in postings]
    protected_before = cli.protected_digest(db_path)

    # 3 fetch-loop checks pass, then 1 persist check passes -> exactly one vacancy written
    exc = _partial_create(db_path, creates, lose_after_checks=4)
    assert exc.created == 1
    assert table(db_path, "select count(*) from jobs") == [(1,)]
    assert cli.protected_digest(db_path) == protected_before

    # the next run continues the unfinished work: the rest is created, the first one is kept
    api = Api(postings)
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert "created: 2" in capsys.readouterr().out
    refs = table(db_path, "select source_reference from jobs order by source_reference")
    assert refs == [(REF_A,), (REF_B,), (REF_C,)]
    assert cli.protected_digest(db_path) == protected_before

    # and a further run changes nothing (no duplicates)
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert "created: 0" in capsys.readouterr().out
    assert table(db_path, "select count(*) from jobs") == [(3,)]
    assert table(
        db_path,
        "select count(*) from (select fingerprint from jobs group by 1 having count(*) > 1)",
    ) == [(0,)]


def test_a_temporarily_locked_database_stops_loudly_and_the_rerun_completes(
    db_path, tmp_path, capsys, monkeypatch
):
    # the writer would wait 5 s on a held lock; keep the test fast
    real_engine = cli._writer_engine

    def fast_writer(engine):
        writer = real_engine(engine)

        @event.listens_for(writer, "connect", insert=True)
        def _short_timeout(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA busy_timeout = 100")

        return writer

    monkeypatch.setattr(cli, "_writer_engine", fast_writer)
    other = sqlite3.connect(db_path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # the database is "temporarily unavailable" to writers
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        # the lock is met before the first write: the run stops loudly, never a silent loss
        with pytest.raises(OperationalError):
            asyncio.run(cli.create_jobs(engine, [stored_job(posting(REF_A))], _Detail()))
    finally:
        engine.dispose()
        other.execute("ROLLBACK")
        other.close()
    assert table(db_path, "select count(*) from jobs") == [(0,)]

    api = Api([posting(REF_A)])
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert "created: 1" in capsys.readouterr().out
    assert table(db_path, "select count(*) from jobs") == [(1,)]


def test_a_second_worker_cannot_write_while_the_first_holds_the_slot(db_path, tmp_path, capsys):
    with cli.exclusive_run_lock(db_path):
        code = run(
            db_path, Api([posting(REF_A)]), "--apply", "--create-new", snapshot=tmp_path / "s"
        )
    assert code == 2  # refused: nothing written
    assert table(db_path, "select count(*) from jobs") == [(0,)]


def test_the_same_reference_in_the_results_twice_is_created_once(db_path, tmp_path, capsys):
    api = Api([posting(REF_A), posting(REF_A)])
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert table(db_path, "select count(*) from jobs") == [(1,)]


def test_writers_pause_between_transactions_so_the_heartbeat_can_renew():
    assert 0.01 <= cli.WRITER_YIELD_SECONDS <= 0.5
