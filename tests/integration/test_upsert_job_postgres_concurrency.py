"""HARD-007 verification pass (adversarial hardening r1): upgrades the
SQLite-only reproduction of `app.db.repositories.upsert_job`'s
concurrent-new-fingerprint race
(tests/test_repository.py::test_concurrent_insert_of_the_same_new_job_can_raise_unhandled_integrity_error)
to real PostgreSQL-dialect evidence, per the explicit instruction that a
SQLite-only concurrency claim is not sufficient production evidence.

**The scenario.** Two independent Sessions/connections race to
`upsert_job()` the exact same brand-new (never-before-seen) fingerprint.
A single outer `threading.Barrier` alone was tried first and found
UNRELIABLE against real PostgreSQL round-trip timing: on some runs one
thread's full SELECT-INSERT-COMMIT completed before the other thread's
own SELECT even ran, so the second thread correctly saw the first's
already-committed row and took the UPDATE branch instead of racing at
all -- a real result, just not the race this module exists to prove. A
SECOND barrier is therefore injected at the exact
`get_job_by_fingerprint` read point (via monkeypatching the bare
module-level name `upsert_job` itself calls, not by touching
`upsert_job`'s own code) so both threads are GUARANTEED to have already
observed `existing is None` before either is allowed to proceed to its
INSERT -- deterministically reproducing the true race every run.

**What this proves (evidence for HARD-007, NOT a fix):**
- `JobRecord.fingerprint`'s DB-level UNIQUE constraint
  (`uq_jobs_fingerprint`) really does hold under genuine PostgreSQL
  concurrency -- exactly one row is ever created, never two.
- The exact exception class the LOSING thread receives on real
  PostgreSQL (psycopg wraps this as `sqlalchemy.exc.IntegrityError`,
  confirmed below -- not a generic `OperationalError` or a driver-level
  surprise).
- Whether the losing Session remains usable after catching that
  exception WITHOUT an explicit `rollback()` first (PostgreSQL, unlike
  SQLite, poisons the entire transaction after any error until an
  explicit ROLLBACK -- this is a real, dialect-specific behavior
  difference worth confirming directly rather than assuming).
- That a subsequent, unrelated, legitimate `upsert_job()` call on the
  SAME losing Session succeeds normally once the poisoned transaction is
  rolled back -- i.e. the caller (once it learns to `rollback()`, which
  `upsert_job` itself currently does NOT do for this specific race) is
  not left permanently stuck.

Per this session's explicit instruction, `upsert_job` is NOT fixed here
-- this only upgrades HARD-007's evidence from SQLite to real
PostgreSQL and is reserved for Codex review.

**Local execution:** skipped automatically unless `TEST_POSTGRES_URL` is
set. Run `alembic upgrade head` against that same database first --
this module has no `Base.metadata.create_all` fallback, matching every
other PostgreSQL integration test in this directory.

**CI:** wired into `.github/workflows/ci.yml`'s existing
`scheduler-postgres` job.
"""

import os
import threading

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import JobRecord
from app.db.repositories import _fingerprint, upsert_job
from app.models.job import Job, JobScore

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

FINGERPRINT_MARKER = "hard-007-pg-concurrent-insert"


@pytest.fixture()
def pg_session_factory():
    engine = create_engine(TEST_POSTGRES_URL, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    cleanup = session_factory()
    try:
        # NOTE: filter on `title` (or `url`), NOT `fingerprint` --
        # `fingerprint` is a SHA-256 hex digest and can never contain the
        # literal marker substring. Filtering on the hash was a bug
        # caught while developing this test: a leftover row from a prior
        # run was never actually cleaned up, silently turning the
        # "brand-new fingerprint" race into a "both see the same
        # already-existing row" non-race that always passed both threads
        # through the UPDATE branch -- a false negative, not a false
        # positive, but still worth flagging as exactly the kind of
        # test-quality issue this hardening pass's own Phase 18 looks for.
        cleanup.execute(delete(JobRecord).where(JobRecord.title.like(f"%{FINGERPRINT_MARKER}%")))
        cleanup.commit()
    finally:
        cleanup.close()
    yield session_factory
    engine.dispose()


def _make_job(uid: int) -> Job:
    return Job(
        source="bundesagentur",
        title=f"Race Engineer {FINGERPRINT_MARKER}",
        company="RaceCo",
        location="Berlin",
        url=f"https://example.com/jobs/{FINGERPRINT_MARKER}-{uid}",
    )


def _synchronized_get_job_by_fingerprint(read_barrier: threading.Barrier):
    """Wraps the REAL `get_job_by_fingerprint` with a second barrier so
    both racing threads are guaranteed to have already completed their
    read (and observed `existing is None`) before either is released to
    proceed to `upsert_job`'s INSERT branch -- makes the true race
    deterministic instead of depending on real PostgreSQL round-trip
    timing (see module docstring).
    """
    from app.db.repositories import get_job_by_fingerprint as real_get_job_by_fingerprint

    def _wrapped(db, job):
        result = real_get_job_by_fingerprint(db, job)
        read_barrier.wait(timeout=5)
        return result

    return _wrapped


class TestRealConcurrentNewJobInsert:
    def test_only_one_row_created_loser_gets_integrity_error(self, pg_session_factory, monkeypatch):
        same_new_job = _make_job(1)
        score = JobScore(score=80, recommendation="APPLY")
        fingerprint = _fingerprint(same_new_job)

        read_barrier = threading.Barrier(2)
        monkeypatch.setattr(
            "app.db.repositories.get_job_by_fingerprint",
            _synchronized_get_job_by_fingerprint(read_barrier),
        )

        results: dict[int, object] = {}

        def worker(index: int) -> None:
            session = pg_session_factory()
            try:
                results[index] = upsert_job(session, same_new_job, score)
            except BaseException as exc:  # noqa: BLE001
                results[index] = exc
            finally:
                session.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        outcomes = list(results.values())
        successes = [o for o in outcomes if isinstance(o, tuple)]
        failures = [o for o in outcomes if isinstance(o, BaseException)]

        # Exactly one thread's INSERT durably wins.
        assert len(successes) == 1
        assert len(failures) == 1

        # The exact exception class real PostgreSQL/psycopg produces via
        # SQLAlchemy for this race -- confirms it's a clean, recognizable
        # IntegrityError, not an opaque driver-level surprise.
        loser_exc = failures[0]
        assert isinstance(loser_exc, IntegrityError)
        assert (
            "uq_jobs_fingerprint" in str(loser_exc.orig) or "unique" in str(loser_exc.orig).lower()
        )

        verify = pg_session_factory()
        try:
            rows = verify.scalars(
                select(JobRecord).where(JobRecord.fingerprint == fingerprint)
            ).all()
            assert len(rows) == 1
        finally:
            verify.close()

    def test_losing_session_is_poisoned_until_explicit_rollback_then_usable_again(
        self, pg_session_factory, monkeypatch
    ):
        """PostgreSQL-specific behavior (does NOT reproduce on SQLite):
        after ANY statement fails inside a transaction, PostgreSQL
        refuses every further statement in that same transaction
        ("current transaction is aborted, commands ignored until end of
        transaction block") until an explicit ROLLBACK. Confirms
        `upsert_job`'s caller -- not `upsert_job` itself, which does not
        currently catch this race's IntegrityError at all -- MUST
        rollback before reusing the Session, and confirms that doing so
        is sufficient to make the Session fully usable again (no need to
        discard/recreate it).
        """
        same_new_job = _make_job(2)
        score = JobScore(score=80, recommendation="APPLY")

        read_barrier = threading.Barrier(2)
        monkeypatch.setattr(
            "app.db.repositories.get_job_by_fingerprint",
            _synchronized_get_job_by_fingerprint(read_barrier),
        )

        sessions: dict[int, object] = {}
        results: dict[int, object] = {}

        def worker(index: int) -> None:
            session = pg_session_factory()
            sessions[index] = session
            try:
                results[index] = upsert_job(session, same_new_job, score)
            except BaseException as exc:  # noqa: BLE001
                results[index] = exc
            # Deliberately NOT closing the session here -- the main
            # thread needs to inspect the loser's session state (poisoned
            # vs. usable) below, from outside this thread, only after
            # this thread has already finished touching it.

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        winner_index = next(i for i, r in results.items() if isinstance(r, tuple))
        loser_index = next(i for i, r in results.items() if isinstance(r, BaseException))
        loser_exc = results[loser_index]
        loser_session = sessions[loser_index]

        assert isinstance(loser_exc, IntegrityError)

        # Restore the REAL (unwrapped) get_job_by_fingerprint before the
        # follow-up "subsequent normal work succeeds" check below -- the
        # 2-party read_barrier only has meaning for the deliberately
        # racing pair above; reusing the still-patched version for a
        # solo call would block for the full 5s timeout waiting for a
        # second party that will never arrive, then raise
        # BrokenBarrierError (confirmed empirically while developing
        # this test -- not a hypothetical concern).
        monkeypatch.undo()

        try:
            # Before rollback: PostgreSQL has poisoned this transaction --
            # even a trivial, unrelated read must fail.
            with pytest.raises(Exception):  # noqa: B017, PT011 -- exact type is driver-level (InFailedSqlTransaction)
                loser_session.execute(select(func.count()).select_from(JobRecord))

            # After an explicit rollback, the Session is fully usable
            # again -- both for a trivial read AND for a genuinely new,
            # unrelated upsert_job call (proves the caller isn't
            # permanently stuck, just needs to know to rollback first).
            loser_session.rollback()
            loser_session.execute(select(func.count()).select_from(JobRecord))

            unrelated_job = _make_job(3)
            record, created = upsert_job(loser_session, unrelated_job, score)
            assert created is True
        finally:
            sessions[winner_index].close()
            loser_session.close()
