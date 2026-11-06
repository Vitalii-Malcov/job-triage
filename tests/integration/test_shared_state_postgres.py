"""Task 23 on real PostgreSQL: several "workers" (separate engines, separate connections) share one
rate-limit budget, one failed-auth budget and one revocation table without overshooting.

Runs at head (no Alembic cycling). Skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first. Test-owned rows (limiter names / session ids
below) are deleted before and after.
"""

import os
import threading

import pytest
from sqlalchemy import create_engine, delete, func, select, text

from app.db.models import SharedRateLimitHit, SharedUiSessionRevocation
from app.security import shared_state

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL not set")

LIMITERS = ("r23-pg-a", "r23-pg-b", "r23-pg-cap")
SID_PREFIX = "r23pg"


def _sid(n: int) -> str:
    return f"{SID_PREFIX}{n:027d}"


@pytest.fixture()
def workers():
    """Four independent engines = four "worker processes" sharing the database."""
    engines = [create_engine(POSTGRES_URL, pool_pre_ping=True) for _ in range(4)]
    cleaner = engines[0]

    def cleanup():
        with cleaner.begin() as conn:
            conn.execute(delete(SharedRateLimitHit).where(SharedRateLimitHit.limiter.in_(LIMITERS)))
            conn.execute(
                delete(SharedUiSessionRevocation).where(
                    SharedUiSessionRevocation.sid.like(f"{SID_PREFIX}%")
                )
            )

    cleanup()
    try:
        yield engines
    finally:
        cleanup()
        for engine in engines:
            engine.dispose()


def _run_threads(target, args_list):
    errors: list[BaseException] = []
    results: list = []
    guard = threading.Lock()
    barrier = threading.Barrier(len(args_list), timeout=60)

    def run(args):
        try:
            barrier.wait()
            outcome = target(*args)
            with guard:
                results.append(outcome)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(a,)) for a in args_list]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    return results


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    shared_state.set_engine_provider(None)


def test_parallel_workers_never_overshoot_one_budget(workers):
    """40 simultaneous requests from 4 workers for ONE client and a budget of 7: exactly 7 win."""
    shared_state.set_engine_provider(lambda: workers[hash(threading.get_ident()) % len(workers)])
    results = _run_threads(
        lambda n: shared_state.rate_hit(
            LIMITERS[0], "203.0.113.9", max_requests=7, window_seconds=300
        ),
        [(n,) for n in range(40)],
    )
    assert sum(1 for r in results if r is not None) == 7
    with workers[0].connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(SharedRateLimitHit)
                .where(SharedRateLimitHit.limiter == LIMITERS[0])
            ).scalar_one()
            == 7
        )


def test_distinct_clients_do_not_consume_each_others_budget(workers):
    shared_state.set_engine_provider(lambda: workers[hash(threading.get_ident()) % len(workers)])
    results = _run_threads(
        lambda n: shared_state.rate_hit(
            LIMITERS[1], f"198.51.100.{n % 5}", max_requests=3, window_seconds=300
        ),
        [(n,) for n in range(50)],
    )
    # 5 clients x 3 allowed
    assert sum(1 for r in results if r is not None) == 15


def test_a_refund_race_never_breaks_the_budget(workers):
    """Reserve up to the limit, refund half of the winners in parallel, then exactly the freed
    slots can be re-reserved."""
    shared_state.set_engine_provider(lambda: workers[hash(threading.get_ident()) % len(workers)])
    won = [
        hit
        for hit in _run_threads(
            lambda n: shared_state.rate_hit(
                LIMITERS[0], "192.0.2.5", max_requests=6, window_seconds=300
            ),
            [(n,) for n in range(20)],
        )
        if hit is not None
    ]
    assert len(won) == 6
    _run_threads(
        lambda hit: shared_state.rate_refund(LIMITERS[0], "192.0.2.5", hit), [(h,) for h in won[:3]]
    )
    again = _run_threads(
        lambda n: shared_state.rate_hit(
            LIMITERS[0], "192.0.2.5", max_requests=6, window_seconds=300
        ),
        [(n,) for n in range(10)],
    )
    assert sum(1 for r in again if r is not None) == 3


def test_concurrent_logouts_respect_the_capacity(workers):
    shared_state.set_engine_provider(lambda: workers[hash(threading.get_ident()) % len(workers)])
    results = _run_threads(
        lambda n: shared_state.session_revoke(_sid(n), 4_000_000_000, capacity=5),
        [(n,) for n in range(20)],
    )
    assert sum(1 for r in results if r) == 5
    with workers[0].connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM shared_ui_session_revocations WHERE sid LIKE :p"),
            {"p": f"{SID_PREFIX}%"},
        ).scalar_one()
    assert count == 5


def test_a_logout_on_one_worker_is_seen_by_every_other_worker_at_once(workers):
    shared_state.set_engine_provider(lambda: workers[0])
    assert shared_state.session_revoke(_sid(900), 4_000_000_000, capacity=100)
    for engine in workers[1:]:
        shared_state.set_engine_provider(lambda engine=engine: engine)
        assert shared_state.session_is_revoked(_sid(900))
        assert not shared_state.session_is_revoked(_sid(901))


def test_the_same_session_revoked_concurrently_is_stored_once(workers):
    shared_state.set_engine_provider(lambda: workers[hash(threading.get_ident()) % len(workers)])
    results = _run_threads(
        lambda n: shared_state.session_revoke(_sid(950), 4_000_000_000, capacity=100),
        [(n,) for n in range(12)],
    )
    assert all(results)
    with workers[0].connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM shared_ui_session_revocations WHERE sid = :s"),
                {"s": _sid(950)},
            ).scalar_one()
            == 1
        )


def test_the_row_cap_holds_under_concurrent_newcomers(workers, monkeypatch):
    monkeypatch.setattr(shared_state, "MAX_LIVE_ROWS_PER_LIMITER", 4)
    shared_state.set_engine_provider(lambda: workers[hash(threading.get_ident()) % len(workers)])
    results = _run_threads(
        lambda n: shared_state.rate_hit(
            LIMITERS[2], f"10.99.0.{n}", max_requests=3, window_seconds=300
        ),
        [(n,) for n in range(12)],
    )
    granted = sum(1 for r in results if r is not None)
    # Different identities take different advisory locks, so the cap is a soft bound under a race
    # (it may overshoot by at most the number of simultaneous newcomers) -- but it never grows
    # without bound: the NEXT newcomers are refused.
    assert 4 <= granted <= 12
    assert (
        shared_state.rate_hit(LIMITERS[2], "10.99.1.1", max_requests=3, window_seconds=300) is None
    )


def test_concurrent_identities_with_expired_rows_never_deadlock(workers):
    """Each request deletes its OWN expired rows; the cross-identity sweep runs in a separate
    transaction with SKIP LOCKED. Many rounds of two identities cleaning up at once must finish
    without a deadlock (which would surface as an unavailable-store error)."""
    shared_state.set_engine_provider(lambda: workers[hash(threading.get_ident()) % len(workers)])
    for round_number in range(25):
        shared_state._last_sweep.clear()
        for who in ("c1", "c2", "c3", "c4"):
            shared_state.rate_hit(
                LIMITERS[1], who, max_requests=5, window_seconds=10, now=100.0 + 100 * round_number
            )
        results = _run_threads(
            lambda who, moment=500.0 + 100 * round_number: shared_state.rate_hit(
                LIMITERS[1], who, max_requests=5, window_seconds=10, now=moment
            ),
            [("c1",), ("c2",), ("c3",), ("c4",), ("c1",), ("c2",)],
        )
        assert all(r is not None for r in results)
