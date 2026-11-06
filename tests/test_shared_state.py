"""Task 23: process-shared rate limits, failed-auth budget and UI sessions
(`SHARED_STATE_BACKEND=database`) plus the single-process guard of the default backend.

"Two workers" are simulated with separate limiter / revocation objects (each with its own
in-memory state) that share one database.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.db.models  # noqa: F401  (registers every table on Base.metadata)
from app.core.config import Settings, get_settings
from app.core.single_process_lock import (
    SecondWebProcessError,
    SingleProcessLock,
    acquire_if_required,
    lock_path_for,
)
from app.db.base import Base
from app.security import rate_limit as rl
from app.security import shared_state
from app.ui import auth as ui_auth


def _request(host: str = "10.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={})


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'shared.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(eng)
    shared_state._last_sweep.clear()
    yield eng
    eng.dispose()


@pytest.fixture()
def shared(engine, monkeypatch):
    """The database backend, pointed at the scratch engine."""
    monkeypatch.setenv("SHARED_STATE_BACKEND", "database")
    get_settings.cache_clear()
    shared_state.set_engine_provider(lambda: engine)
    yield engine
    shared_state.set_engine_provider(None)
    get_settings.cache_clear()


def _count(engine, table="shared_rate_limit_hits") -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608


class TestStore:
    def test_the_limit_is_enforced_per_identity_and_limiter(self, shared):
        for _ in range(3):
            assert shared_state.rate_hit("a", "10.0.0.1", max_requests=3, window_seconds=60)
        assert shared_state.rate_hit("a", "10.0.0.1", max_requests=3, window_seconds=60) is None
        assert shared_state.rate_hit("a", "10.0.0.2", max_requests=3, window_seconds=60)
        assert shared_state.rate_hit("b", "10.0.0.1", max_requests=3, window_seconds=60)

    def test_the_window_slides(self, shared):
        for moment in (100.0, 101.0, 102.0):
            assert shared_state.rate_hit("a", "x", max_requests=3, window_seconds=10, now=moment)
        assert shared_state.rate_hit("a", "x", max_requests=3, window_seconds=10, now=105.0) is None
        # the first hit (t=100) is now older than the 10 s window
        assert shared_state.rate_hit("a", "x", max_requests=3, window_seconds=10, now=110.5)
        assert shared_state.rate_hit("a", "x", max_requests=3, window_seconds=10, now=110.6) is None

    def test_a_refund_frees_exactly_its_own_slot(self, shared):
        first = shared_state.rate_hit("a", "x", max_requests=1, window_seconds=60)
        assert shared_state.rate_hit("a", "x", max_requests=1, window_seconds=60) is None
        shared_state.rate_refund("other", "x", first)  # another limiter's refund is a no-op
        shared_state.rate_refund("a", "someone-else", first)  # so is another client's
        assert shared_state.rate_hit("a", "x", max_requests=1, window_seconds=60) is None
        shared_state.rate_refund("a", "x", first)
        assert shared_state.rate_hit("a", "x", max_requests=1, window_seconds=60)

    def test_no_client_address_is_stored_in_clear(self, shared):
        shared_state.rate_hit("a", "203.0.113.77", max_requests=3, window_seconds=60)
        with shared.connect() as conn:
            identity = conn.execute(
                text("SELECT identity FROM shared_rate_limit_hits")
            ).scalar_one()
        assert "203.0.113" not in identity and len(identity) == 64

    def test_the_table_stays_bounded_for_new_identities_only(self, shared, monkeypatch):
        monkeypatch.setattr(shared_state, "MAX_LIVE_ROWS_PER_LIMITER", 3)
        for n in range(3):
            assert shared_state.rate_hit("a", f"id{n}", max_requests=5, window_seconds=60)
        assert shared_state.rate_hit("a", "newcomer", max_requests=5, window_seconds=60) is None
        assert shared_state.rate_hit("a", "id0", max_requests=5, window_seconds=60)  # known client

    def test_expired_rows_of_other_identities_are_swept_for_a_newcomer(self, shared, monkeypatch):
        monkeypatch.setattr(shared_state, "MAX_LIVE_ROWS_PER_LIMITER", 2)
        shared_state.rate_hit("a", "old1", max_requests=5, window_seconds=10, now=100.0)
        shared_state.rate_hit("a", "old2", max_requests=5, window_seconds=10, now=100.0)
        assert shared_state.rate_hit("a", "new", max_requests=5, window_seconds=10, now=500.0)

    def test_a_database_error_is_reported_not_swallowed(self, shared, monkeypatch):
        broken = create_engine("sqlite:///C:/definitely/missing/dir/x.db")
        shared_state.set_engine_provider(lambda: broken)
        with pytest.raises(shared_state.SharedStateUnavailableError):
            shared_state.rate_hit("a", "x", max_requests=3, window_seconds=60)


class TestLimiterAcrossWorkers:
    def test_two_workers_share_one_budget(self, shared):
        worker_a = rl._RateLimiter(name="shared-test", detail="limited")
        worker_b = rl._RateLimiter(name="shared-test", detail="limited")
        for limiter in (worker_a, worker_b, worker_a):
            limiter.check(_request(), max_requests=3, window_seconds=60)
        with pytest.raises(HTTPException) as refused:
            worker_b.check(_request(), max_requests=3, window_seconds=60)
        assert refused.value.status_code == 429 and refused.value.detail == "limited"
        # nothing of this lives in the workers' own memory
        assert not worker_a.buckets and not worker_b.buckets

    def test_a_store_outage_fails_closed_with_503(self, shared):
        broken = create_engine("sqlite:///C:/definitely/missing/dir/x.db")
        shared_state.set_engine_provider(lambda: broken)
        limiter = rl._RateLimiter(name="shared-test", detail="limited")
        with pytest.raises(HTTPException) as refused:
            limiter.check(_request(), max_requests=3, window_seconds=60)
        assert refused.value.status_code == 503

    def test_the_failed_auth_budget_is_shared_and_refundable(self, shared, monkeypatch):
        monkeypatch.setattr(rl, "AUTH_FAILURE_LIMIT", 2)
        first = rl.begin_auth_attempt(_request())
        rl.begin_auth_attempt(_request())
        with pytest.raises(HTTPException) as locked:
            rl.begin_auth_attempt(_request())  # a "second worker" sees the same two failures
        assert locked.value.status_code == 429
        rl.end_auth_attempt_ok(_request(), first)  # a valid credential refunds ITS attempt
        rl.begin_auth_attempt(_request())

    def test_ipv6_addresses_share_one_slash_64_budget(self, shared, monkeypatch):
        monkeypatch.setattr(rl, "AUTH_FAILURE_LIMIT", 1)
        rl.begin_auth_attempt(_request("2001:db8:1:2::1"))
        with pytest.raises(HTTPException):
            rl.begin_auth_attempt(_request("2001:db8:1:2:ffff::9"))

    def test_the_default_backend_never_touches_the_shared_tables(self, engine, monkeypatch):
        monkeypatch.delenv("SHARED_STATE_BACKEND", raising=False)
        get_settings.cache_clear()
        shared_state.set_engine_provider(lambda: engine)
        try:
            limiter = rl._RateLimiter(name="memory-test", detail="limited")
            limiter.check(_request(), max_requests=3, window_seconds=60)
            rl.begin_auth_attempt(_request("10.9.9.9"))
            rl._auth_failure_limiter.buckets.clear()
        finally:
            shared_state.set_engine_provider(None)
            get_settings.cache_clear()
        assert _count(engine) == 0 and len(limiter.buckets) == 1

    def test_every_limiter_has_a_unique_stable_name(self):
        limiters = [v for v in vars(rl).values() if isinstance(v, rl._RateLimiter)]
        names = [limiter.name for limiter in limiters]
        assert len(limiters) >= 19 and len(set(names)) == len(names)


class TestUiSessions:
    def test_a_logout_on_one_worker_is_honoured_by_another(self, shared):
        worker_a, worker_b = ui_auth.SessionRevocations(), ui_auth.SessionRevocations()
        assert not worker_b.is_revoked("a" * 32)
        assert worker_a.revoke("a" * 32, 4_000_000_000)
        assert worker_b.is_revoked("a" * 32)
        assert worker_a.revoke("a" * 32, 4_000_000_000)  # idempotent

    def test_a_restart_keeps_the_revocation(self, shared):
        ui_auth.SessionRevocations().revoke("b" * 32, 4_000_000_000)
        assert ui_auth.SessionRevocations().is_revoked("b" * 32)  # a brand-new "process"

    def test_a_full_table_fails_the_logout_instead_of_forgetting(self, shared, monkeypatch):
        monkeypatch.setattr(ui_auth.SessionRevocations, "CAPACITY", 2)
        revocations = ui_auth.SessionRevocations()
        assert revocations.revoke("1" * 32, 4_000_000_000)
        assert revocations.revoke("2" * 32, 4_000_000_000)
        assert not revocations.revoke("3" * 32, 4_000_000_000)
        assert revocations.is_revoked("1" * 32) and not revocations.is_revoked("3" * 32)

    def test_expired_revocations_make_room(self, shared, monkeypatch):
        monkeypatch.setattr(ui_auth.SessionRevocations, "CAPACITY", 1)
        revocations = ui_auth.SessionRevocations()
        assert revocations.revoke("1" * 32, 1_000, now=500.0)
        assert revocations.revoke("2" * 32, 4_000_000_000, now=2_000.0)

    def test_an_unreachable_store_fails_closed(self, shared):
        broken = create_engine("sqlite:///C:/definitely/missing/dir/x.db")
        shared_state.set_engine_provider(lambda: broken)
        revocations = ui_auth.SessionRevocations()
        assert revocations.is_revoked("c" * 32) is True  # cookie refused
        assert revocations.revoke("c" * 32, 4_000_000_000) is False  # logout reported as failed

    def test_cookies_are_valid_across_workers_only_with_the_shared_backend(
        self, shared, monkeypatch
    ):
        settings = Settings(_env_file=None, api_key="k" * 12)
        monkeypatch.setattr(ui_auth, "_PROCESS_NONCE", "nonce-of-worker-a")
        key_a = ui_auth.signing_key(settings)
        token = ui_auth.issue_session_token(key_a, hours=1)
        monkeypatch.setattr(ui_auth, "_PROCESS_NONCE", "nonce-of-worker-b")
        key_b = ui_auth.signing_key(settings)
        assert key_a == key_b and ui_auth.session_token_valid(token, key_b)

    def test_the_default_backend_still_binds_a_cookie_to_its_process(self, monkeypatch):
        monkeypatch.delenv("SHARED_STATE_BACKEND", raising=False)
        get_settings.cache_clear()
        try:
            settings = Settings(_env_file=None, api_key="k" * 12)
            monkeypatch.setattr(ui_auth, "_PROCESS_NONCE", "nonce-of-worker-a")
            key_a = ui_auth.signing_key(settings)
            token = ui_auth.issue_session_token(key_a, hours=1)
            monkeypatch.setattr(ui_auth, "_PROCESS_NONCE", "nonce-of-worker-b")
            assert not ui_auth.session_token_valid(token, ui_auth.signing_key(settings))
        finally:
            get_settings.cache_clear()


class TestSingleProcessGuard:
    def test_a_second_holder_is_refused_and_the_lock_is_released_after(self, tmp_path):
        first, second = (
            SingleProcessLock(tmp_path / "x.lock"),
            SingleProcessLock(tmp_path / "x.lock"),
        )
        first.acquire()
        with pytest.raises(SecondWebProcessError, match="SHARED_STATE_BACKEND=database"):
            second.acquire()
        first.release()
        second.acquire()
        second.release()

    def test_the_lock_follows_the_database_url(self):
        assert lock_path_for("sqlite:///a.db") != lock_path_for("sqlite:///b.db")
        assert lock_path_for("sqlite:///a.db") == lock_path_for("sqlite:///a.db")

    def test_only_the_memory_backend_takes_the_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.core.single_process_lock.lock_path_for",
            lambda url, directory="": tmp_path / "g.lock",
        )
        memory = Settings(_env_file=None, single_process_lock_enabled=True)
        held = acquire_if_required(memory)
        try:
            assert held is not None
            with pytest.raises(SecondWebProcessError):
                acquire_if_required(memory)
        finally:
            held.release()
        shared_backend = Settings(_env_file=None, shared_state_backend="database")
        assert acquire_if_required(shared_backend) is None  # workers are supported there
        disabled = Settings(_env_file=None, single_process_lock_enabled=False)
        assert acquire_if_required(disabled) is None

    def test_the_app_lifespan_takes_and_releases_the_lock(self, tmp_path, monkeypatch):
        import asyncio

        import app.main as main

        monkeypatch.setattr(
            "app.core.single_process_lock.lock_path_for",
            lambda url, directory="": tmp_path / "life.lock",
        )
        monkeypatch.setenv("SINGLE_PROCESS_LOCK_ENABLED", "true")
        get_settings.cache_clear()
        started = []

        async def fake_start_bot(settings):
            started.append(1)

        async def fake_stop_bot(application):
            return None

        monkeypatch.setattr(main, "start_bot", fake_start_bot)
        monkeypatch.setattr(main, "stop_bot", fake_stop_bot)

        async def scenario():
            async with main.lifespan(main.app):
                # a second web process starting now is refused
                with pytest.raises(SecondWebProcessError):
                    async with main.lifespan(main.app):
                        pass
            # after shutdown the lock is free again
            async with main.lifespan(main.app):
                pass

        try:
            asyncio.run(scenario())
        finally:
            get_settings.cache_clear()
        assert started == [1, 1]

    def test_the_settings_defaults_keep_production_behaviour(self, monkeypatch):
        monkeypatch.delenv("SINGLE_PROCESS_LOCK_ENABLED", raising=False)  # conftest disables it
        defaults = Settings(_env_file=None)
        assert defaults.shared_state_backend == "memory"
        assert defaults.single_process_lock_enabled is True
        with pytest.raises(ValueError):
            Settings(_env_file=None, shared_state_backend="redis")


def test_session_factory_for_tests_uses_the_scratch_engine(shared):
    # sanity: the store writes through the injected engine, not the application's
    Session = sessionmaker(bind=shared)
    with Session() as db:
        shared_state.rate_hit("a", "x", max_requests=1, window_seconds=60)
        assert db.execute(text("SELECT count(*) FROM shared_rate_limit_hits")).scalar_one() == 1


class TestReviewFixes:
    def test_a_stale_refund_token_cannot_remove_a_newer_reservation(self, shared):
        """SQLite reuses deleted primary keys unless the table is AUTOINCREMENT; the token is also
        bound to its own client, so an old token can never refund someone else's charge."""
        old = shared_state.rate_hit("a", "client-1", max_requests=5, window_seconds=10, now=100.0)
        # the old hit expires and is swept away
        shared_state.rate_hit("a", "client-1", max_requests=5, window_seconds=10, now=500.0)
        with shared.begin() as conn:
            conn.execute(text("DELETE FROM shared_rate_limit_hits WHERE hit_at < 400"))
        newer = shared_state.rate_hit("a", "client-2", max_requests=5, window_seconds=10, now=501.0)
        assert newer != old  # an id is never handed out twice
        shared_state.rate_refund("a", "client-1", old)  # the delayed refund of the OLD charge
        assert _count(shared) == 2  # client-2's charge (and client-1's newer one) survive
        shared_state.rate_refund("a", "client-1", newer)  # a token of another client: no effect
        assert _count(shared) == 2

    def test_the_sqlite_table_is_autoincrement(self, shared):
        with shared.connect() as conn:
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'shared_rate_limit_hits'")
            ).scalar_one()
        assert "AUTOINCREMENT" in ddl.upper()

    def test_the_sweep_runs_outside_the_hit_and_only_every_interval(self, shared, monkeypatch):
        for who in ("old1", "old2"):
            shared_state.rate_hit("a", who, max_requests=5, window_seconds=10, now=100.0)
        assert _count(shared) == 2
        shared_state._last_sweep.clear()
        shared_state.rate_hit("a", "fresh", max_requests=5, window_seconds=10, now=500.0)
        assert _count(shared) == 1  # the expired rows of OTHER clients were swept
        shared_state.rate_hit("a", "old3", max_requests=5, window_seconds=10, now=100.0)
        shared_state.rate_hit("a", "x", max_requests=5, window_seconds=10, now=500.0)
        assert _count(shared) == 3  # within the interval: no second sweep (cost bounded)

    def test_the_cap_counts_live_rows_only(self, shared, monkeypatch):
        monkeypatch.setattr(shared_state, "MAX_LIVE_ROWS_PER_LIMITER", 2)
        monkeypatch.setattr(shared_state, "_SWEEP_INTERVAL_SECONDS", 10**9)  # no cleanup at all
        for who in ("e1", "e2", "e3"):
            shared_state.rate_hit("a", who, max_requests=5, window_seconds=10, now=100.0)
        # three EXPIRED rows are present, but they do not count against a newcomer
        assert shared_state.rate_hit("a", "n1", max_requests=5, window_seconds=10, now=500.0)

    def test_documented_request_cost_is_real(self, shared):
        """An authenticated API request in shared mode: one generic check + one failed-auth
        reserve + one refund. The docs state the measured bound; keep them honest."""
        from sqlalchemy import event

        statements: list[str] = []

        @event.listens_for(shared, "before_cursor_execute")
        def count(conn, cursor, statement, *args):
            statements.append(statement)

        request = _request("192.0.2.77")
        rl._generic_limiter.check(request, max_requests=60, window_seconds=60)
        token = rl.begin_auth_attempt(request)
        rl.end_auth_attempt_ok(request, token)
        event.remove(shared, "before_cursor_execute", count)
        assert 0 < len(statements) <= 20, len(statements)

    @pytest.mark.asyncio
    async def test_the_ui_login_does_its_database_work_off_the_event_loop(self, monkeypatch):
        import app.ui.routes as ui_routes

        offloaded: list[str] = []

        async def spy(func, *args, **kwargs):
            offloaded.append(func.__name__)
            return func(*args, **kwargs)

        def fake_enforce(request):
            return None

        def fake_begin(request):
            return 1.0

        def fake_end(request, token):
            return None

        monkeypatch.setattr(ui_routes, "run_in_threadpool", spy)
        monkeypatch.setattr("app.security.rate_limit.enforce_ui_login_rate_limit", fake_enforce)
        monkeypatch.setattr("app.security.rate_limit.begin_auth_attempt", fake_begin)
        monkeypatch.setattr("app.security.rate_limit.end_auth_attempt_ok", fake_end)
        monkeypatch.setattr(ui_routes, "api_key_matches", lambda submitted, key: True)

        class FakeRequest:
            headers = {"content-length": "0"}
            client = SimpleNamespace(host="127.0.0.1")
            cookies = {}

            async def stream(self):
                if False:
                    yield b""

        await ui_routes.login(FakeRequest())
        assert offloaded == ["fake_enforce", "fake_begin", "fake_end"]


class TestLockIdentity:
    def test_spellings_of_one_sqlite_file_share_a_lock(self, tmp_path, monkeypatch):
        from app.core.single_process_lock import database_identity

        monkeypatch.chdir(tmp_path)
        absolute = (tmp_path / "job.db").as_posix()
        assert (
            database_identity("sqlite:///./job.db")
            == database_identity("sqlite:///job.db")
            == database_identity(f"sqlite:///{absolute}")
        )

    def test_the_same_relative_url_from_another_directory_is_another_database(
        self, tmp_path, monkeypatch
    ):
        from app.core.single_process_lock import database_identity

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        monkeypatch.chdir(tmp_path / "a")
        first = database_identity("sqlite:///job.db")
        monkeypatch.chdir(tmp_path / "b")
        assert database_identity("sqlite:///job.db") != first

    def test_postgres_credentials_are_not_part_of_the_identity(self):
        from app.core.single_process_lock import database_identity

        one = database_identity("postgresql+psycopg://alice:secret@DB.example:5432/jobs")
        two = database_identity("postgresql+psycopg://bob:other@db.example:5432/jobs")
        assert one == two and "secret" not in one and "alice" not in one
        assert one != database_identity("postgresql+psycopg://alice:secret@db.example:5432/other")

    def test_the_lock_directory_is_configurable(self, tmp_path):
        from app.core.single_process_lock import lock_path_for

        assert lock_path_for("sqlite:///a.db", str(tmp_path)).parent == tmp_path
        assert lock_path_for("sqlite:///a.db").parent != tmp_path


class TestRoundTwoFixes:
    def test_one_sweep_drains_a_backlog_larger_than_a_batch(self, shared, monkeypatch):
        monkeypatch.setattr(shared_state, "_SWEEP_BATCH", 3)
        with shared.begin() as conn:
            for n in range(10):
                conn.execute(
                    text(
                        "INSERT INTO shared_rate_limit_hits (limiter, identity, hit_at) "
                        "VALUES ('a', :i, 1.0)"
                    ),
                    {"i": f"id{n}"},
                )
        shared_state._last_sweep.clear()
        shared_state._sweep_expired("a", cutoff=100.0)
        assert _count(shared) == 0

    def test_new_clients_are_refused_at_the_storage_ceiling_even_when_the_rows_are_expired(
        self, shared, monkeypatch
    ):
        monkeypatch.setattr(shared_state, "_SWEEP_INTERVAL_SECONDS", 10**9)  # no cleanup
        for who in ("e1", "e2", "e3", "e4"):  # stored while the caps are still high
            shared_state.rate_hit("a", who, max_requests=5, window_seconds=10, now=100.0)
        monkeypatch.setattr(shared_state, "MAX_LIVE_ROWS_PER_LIMITER", 2)
        monkeypatch.setattr(shared_state, "STORAGE_CEILING_FACTOR", 2)
        # four EXPIRED rows are stored = the ceiling (2 x 2): a newcomer is refused ...
        assert (
            shared_state.rate_hit("a", "n1", max_requests=5, window_seconds=10, now=500.0) is None
        )
        # ... while a client that already has rows keeps working
        assert shared_state.rate_hit("a", "e1", max_requests=5, window_seconds=10, now=500.0)

    def test_postgres_identity_uses_the_effective_target(self):
        from app.core.single_process_lock import database_identity

        base = database_identity("postgresql+psycopg://u:p@db.example/jobs")
        assert base == database_identity("postgresql+psycopg://u:p@DB.example:5432/jobs")
        assert base == database_identity("postgresql://x:y@db.example:5432/jobs")
        assert base == database_identity(
            "postgresql+psycopg://u:p@other.example/jobs?host=db.example&port=5432"
        )
        assert base == database_identity("postgresql+psycopg://ignored/jobs?host=db.example:5432")
        assert base == database_identity(
            "postgresql+psycopg://ignored:6543/jobs?host=db.example:5432"
        )
        assert base != database_identity("postgresql+psycopg://u:p@db.example:6543/jobs")

    def test_a_missing_lock_directory_is_created(self, tmp_path):
        lock = SingleProcessLock(tmp_path / "fresh" / "nested" / "x.lock")
        lock.acquire()
        lock.release()
        assert (tmp_path / "fresh" / "nested").is_dir()

    def test_an_unprovisionable_lock_location_is_a_clear_error(self, tmp_path):
        from app.core.single_process_lock import SingleProcessLockError

        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(SingleProcessLockError, match="SINGLE_PROCESS_LOCK_DIR"):
            SingleProcessLock(blocker / "sub" / "x.lock").acquire()
