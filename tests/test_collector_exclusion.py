"""Review 2.3: manual collector runs hold the same RUNNING slot automation cycles
hold (app/services/collector_exclusion.py)."""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.collectors.base import CollectorNotConfiguredError
from app.core.config import Settings
from app.db.automation_repository import create_running_run
from app.db.base import Base
from app.db.models import AutomationRunRecord
from app.services import collector_exclusion as ce
from app.services.collector_exclusion import (
    CollectorRunInProgressError,
    CollectorRunLeaseLostError,
    lock_account_keys,
    run_collector_exclusively,
)

STATS = {
    "fetched": 2,
    "created": 1,
    "updated": 1,
    "skipped_invalid": 0,
    "failed": 0,
    "deadline_exceeded": False,
}


def _settings(**overrides) -> Settings:
    data = {
        "_env_file": None,
        "gmail_username": "Me@Example.com",
        "xing_mailbox_username": "xing@example.com",
        "xing_mailbox_app_password": "app-password",
        "bundesagentur_api_key": "ba-key",
    }
    data.update(overrides)
    return Settings(**data)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'exclusion.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


async def _wait(event: threading.Event, timeout: float = 20.0) -> None:
    """Await a threading.Event without blocking the test loop (the runner runs
    on the offload worker's own loop, so asyncio primitives cannot be shared)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting for the worker"
        await asyncio.sleep(0.01)


def _gated_runner(gate: threading.Event, started: threading.Event):
    async def slow(db, settings, *, is_lease_lost=None):
        started.set()
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return STATS

    return slow


def _runs(factory):
    with factory() as db:
        return list(db.scalars(select(AutomationRunRecord).order_by(AutomationRunRecord.id)))


async def _ok(db, settings, *, is_lease_lost=None):
    return dict(STATS)


class TestLockKeys:
    def test_identical_identities_share_one_key(self):
        settings = _settings(automation_scheduler_account_key="  me@example.com ")
        assert lock_account_keys(settings) == ("me@example.com",)

    def test_scheduler_key_is_held_exactly_as_the_scheduler_persists_it(self):
        settings = _settings(automation_scheduler_account_key="  Me@EXAMPLE.com ")
        assert lock_account_keys(settings) == ("Me@EXAMPLE.com", "me@example.com")

    def test_distinct_identities_give_sorted_keys(self):
        settings = _settings(automation_scheduler_account_key="zed@example.com")
        assert lock_account_keys(settings) == ("me@example.com", "zed@example.com")

    def test_unset_identities_use_the_empty_key(self):
        assert lock_account_keys(_settings(gmail_username="")) == ("",)

    def test_the_rest_key_is_kept_even_when_gmail_is_unset(self):
        settings = _settings(gmail_username="", automation_scheduler_account_key="zed@example.com")
        assert lock_account_keys(settings) == ("", "zed@example.com")


class TestHappyPathAndFailures:
    @pytest.mark.asyncio
    async def test_success_records_a_labelled_completed_run_and_frees_the_slot(self, factory):
        with factory() as db:
            stats = await run_collector_exclusively(db, _settings(), "xing", _ok)
        assert stats == STATS
        [run] = _runs(factory)
        assert run.status == "COMPLETED" and run.finished_at is not None
        results = json.loads(run.results_json)
        assert list(results) == ["manual:xing"]
        assert results["manual:xing"]["status"] == "ok"
        assert results["manual:xing"]["counters"]["created"] == 1
        # the slot is free again
        with factory() as db:
            await run_collector_exclusively(db, _settings(), "xing", _ok)
        assert [r.status for r in _runs(factory)] == ["COMPLETED", "COMPLETED"]

    @pytest.mark.asyncio
    async def test_not_configured_fails_before_any_slot_is_claimed(self, factory):
        settings = _settings(xing_mailbox_app_password="")
        called = []

        async def runner(db, settings, *, is_lease_lost=None):
            called.append(1)
            return STATS

        with factory() as db, pytest.raises(CollectorNotConfiguredError):
            await run_collector_exclusively(db, settings, "xing", runner)
        assert called == [] and _runs(factory) == []

        settings = _settings(bundesagentur_api_key="  ")
        with factory() as db, pytest.raises(CollectorNotConfiguredError):
            await run_collector_exclusively(db, settings, "bundesagentur", runner)
        assert _runs(factory) == []

    @pytest.mark.asyncio
    async def test_runner_exception_finalizes_failed_releases_the_slot_and_propagates(
        self, factory
    ):
        class Boom(RuntimeError):
            pass

        async def runner(db, settings, *, is_lease_lost=None):
            raise Boom("secret upstream detail")

        with factory() as db, pytest.raises(Boom):
            await run_collector_exclusively(db, _settings(), "bundesagentur", runner)
        [run] = _runs(factory)
        assert run.status == "FAILED"
        assert "secret upstream detail" not in (run.error_summary or "") + run.results_json
        assert json.loads(run.results_json)["manual:bundesagentur"]["error_type"] == "Boom"
        with factory() as db:  # slot reusable
            await run_collector_exclusively(db, _settings(), "bundesagentur", _ok)

    @pytest.mark.parametrize(
        ("stats", "expected"),
        [
            ({"created": 1, "updated": 0, "failed": 0}, "COMPLETED"),
            ({"created": 0, "updated": 0, "failed": 0}, "COMPLETED"),
            ({"created": 1, "updated": 0, "failed": 1}, "PARTIAL"),
            ({"created": 0, "updated": 0, "failed": 2}, "FAILED"),
            ({"created": 1, "updated": 0, "failed": 0, "deadline_exceeded": True}, "PARTIAL"),
            ({"created": 1, "updated": 0, "failed": 0, "failed_queries": 1}, "PARTIAL"),
        ],
    )
    def test_returned_counters_are_classified_like_automation(self, stats, expected):
        run_status, step = ce._step_result(stats)
        assert run_status == expected
        assert step["error_type"] is None


class TestExclusion:
    @pytest.mark.asyncio
    async def test_a_live_run_blocks_a_manual_run_without_starting_it(self, factory):
        with factory() as db:
            create_running_run(db, account_key="me@example.com", holder="automation:1")
        called = []

        async def runner(db, settings, *, is_lease_lost=None):
            called.append(1)
            return STATS

        with factory() as db, pytest.raises(CollectorRunInProgressError):
            await run_collector_exclusively(db, _settings(), "xing", runner)
        assert called == []

    @pytest.mark.asyncio
    async def test_two_manual_runs_cannot_overlap_and_the_slot_returns_afterwards(self, factory):
        gate, started = threading.Event(), threading.Event()
        slow = _gated_runner(gate, started)

        async def first():
            with factory() as db:
                return await run_collector_exclusively(db, _settings(), "xing", slow)

        task = asyncio.create_task(first())
        await _wait(started)
        with factory() as db, pytest.raises(CollectorRunInProgressError):
            await run_collector_exclusively(db, _settings(), "bundesagentur", _ok)
        gate.set()
        assert await task == STATS
        with factory() as db:
            assert await run_collector_exclusively(db, _settings(), "bundesagentur", _ok) == STATS

    @pytest.mark.asyncio
    async def test_a_manual_run_blocks_an_automation_cycle(self, factory):
        from app.services.automation import (
            AutomationRunAlreadyInProgressError,
            run_automation_cycle,
        )

        gate, started = threading.Event(), threading.Event()
        slow = _gated_runner(gate, started)

        async def manual():
            with factory() as db:
                return await run_collector_exclusively(db, _settings(), "xing", slow)

        task = asyncio.create_task(manual())
        await _wait(started)
        with factory() as db, pytest.raises(AutomationRunAlreadyInProgressError):
            await run_automation_cycle(db, account_key="me@example.com", settings=_settings())
        gate.set()
        await task

    @pytest.mark.asyncio
    async def test_scheduler_identity_variant_is_also_held(self, factory):
        """Manual run holds BOTH keys; a live run on the scheduler's key blocks it
        and the first key it had taken is released again (nothing left RUNNING)."""
        settings = _settings(automation_scheduler_account_key="zed@example.com")
        with factory() as db:
            create_running_run(db, account_key="zed@example.com", holder="scheduler:1")
        with factory() as db, pytest.raises(CollectorRunInProgressError):
            await run_collector_exclusively(db, settings, "xing", _ok)
        statuses = {r.account_key: r.status for r in _runs(factory)}
        assert statuses["zed@example.com"] == "RUNNING"
        assert statuses["me@example.com"] == "FAILED"  # taken, then released unstarted

    @pytest.mark.asyncio
    async def test_crashed_holders_expired_lease_is_reclaimed(self, factory):
        with factory() as db:
            run, _ = create_running_run(db, account_key="me@example.com", holder="crashed:1")
            db.execute(
                update(AutomationRunRecord)
                .where(AutomationRunRecord.id == run.id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
            )
            db.commit()
        with factory() as db:
            assert await run_collector_exclusively(db, _settings(), "xing", _ok) == STATS
        statuses = [r.status for r in _runs(factory)]
        assert statuses == ["FAILED", "COMPLETED"]


class TestMixedCaseSchedulerKey:
    @pytest.mark.asyncio
    async def test_live_scheduler_lease_with_mixed_case_blocks_a_manual_run(self, factory):
        settings = _settings(automation_scheduler_account_key="ZED@Example.com")
        with factory() as db:
            create_running_run(db, account_key="ZED@Example.com", holder="scheduler:1")
        with factory() as db, pytest.raises(CollectorRunInProgressError):
            await run_collector_exclusively(db, settings, "xing", _ok)

    @pytest.mark.asyncio
    async def test_manual_run_blocks_the_scheduler_under_its_own_spelling(self, factory):
        from app.services.automation import (
            AutomationRunAlreadyInProgressError,
            run_automation_cycle,
        )

        settings = _settings(automation_scheduler_account_key="ZED@Example.com")
        gate, started = threading.Event(), threading.Event()
        slow = _gated_runner(gate, started)

        async def manual():
            with factory() as db:
                return await run_collector_exclusively(db, settings, "xing", slow)

        task = asyncio.create_task(manual())
        await _wait(started)
        with factory() as db, pytest.raises(AutomationRunAlreadyInProgressError):
            await run_automation_cycle(db, account_key="ZED@Example.com", settings=settings)
        gate.set()
        await task


class TestOwnership:
    @pytest.mark.asyncio
    async def test_expired_lease_at_finalization_is_reported_not_succeeded(self, factory):
        async def runner(db, settings, *, is_lease_lost=None):
            with factory() as side:  # the lease lapses while the collector runs
                side.execute(
                    update(AutomationRunRecord).values(
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                    )
                )
                side.commit()
            return STATS

        with factory() as db, pytest.raises(CollectorRunLeaseLostError):
            await run_collector_exclusively(db, _settings(), "xing", runner)
        # nothing overwrote the row: still RUNNING until crash recovery reconciles it
        [run] = _runs(factory)
        assert run.status == "RUNNING"

    @pytest.mark.asyncio
    async def test_old_holder_returning_after_replacement_cannot_finalize(self, factory):
        async def runner(db, settings, *, is_lease_lost=None):
            with factory() as side:
                side.execute(
                    update(AutomationRunRecord).values(
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                    )
                )
                side.commit()
                # another process reclaims the slot and starts its own run
                create_running_run(side, account_key="me@example.com", holder="replacement:1")
            return STATS

        with factory() as db, pytest.raises(CollectorRunLeaseLostError):
            await run_collector_exclusively(db, _settings(), "xing", runner)
        rows = {r.lease_holder: r.status for r in _runs(factory)}
        assert rows["replacement:1"] == "RUNNING"  # the new owner's state is untouched

    @pytest.mark.asyncio
    async def test_runner_sees_the_heartbeat_lease_state(self, factory, monkeypatch):
        seen = []

        async def runner(db, settings, *, is_lease_lost=None):
            seen.append(is_lease_lost())
            return STATS

        with factory() as db:
            await run_collector_exclusively(db, _settings(), "xing", runner)
        assert seen == [False]

    @pytest.mark.asyncio
    async def test_cancellation_keeps_the_slot_held_for_a_possibly_running_worker(self, factory):
        async def runner(db, settings, *, is_lease_lost=None):
            raise asyncio.CancelledError

        with factory() as db, pytest.raises(asyncio.CancelledError):
            await run_collector_exclusively(db, _settings(), "xing", runner)
        [run] = _runs(factory)
        assert run.status == "RUNNING"  # not finalized: a worker thread may still be using it
        expires = run.lease_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        assert expires > datetime.now(UTC) + timedelta(seconds=ce.IMAP_SESSION_DEADLINE_SECONDS)
        with factory() as db, pytest.raises(CollectorRunInProgressError):
            await run_collector_exclusively(db, _settings(), "xing", _ok)

    @pytest.mark.asyncio
    async def test_a_run_longer_than_the_ttl_keeps_its_lease_via_the_heartbeat(
        self, factory, monkeypatch
    ):
        monkeypatch.setattr(ce, "AUTOMATION_RUN_LEASE_TTL_SECONDS", 3.0)

        async def runner(db, settings, *, is_lease_lost=None):
            await asyncio.sleep(7.0)  # > 2x TTL; the heartbeat (every 1.0s) must renew
            assert not is_lease_lost()
            return STATS

        monkeypatch.setattr(
            "app.services.collector_exclusion.create_running_run",
            lambda db, **kw: create_running_run(db, **{**kw, "ttl_seconds": 3.0}),
        )
        with factory() as db:
            assert await run_collector_exclusively(db, _settings(), "xing", runner) == STATS
        assert [r.status for r in _runs(factory)] == ["COMPLETED"]


class TestEndpoints:
    def test_rest_returns_409_for_contention_and_lease_loss(self, factory, monkeypatch):
        from fastapi.testclient import TestClient

        from app.db.session import get_db
        from app.main import app
        from app.security import rate_limit

        settings = _settings(api_key="k", rate_limit_requests=1000)

        def override():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override
        for target in ("app.security.auth", "app.security.rate_limit", "app.api.routers.common"):
            monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
        rate_limit._xing_requests.clear()
        rate_limit._collector_requests.clear()
        try:
            with factory() as db:
                create_running_run(db, account_key="me@example.com", holder="automation:1")
            with TestClient(app) as client:
                for path in ("xing", "bundesagentur"):
                    response = client.post(
                        f"/api/v1/collectors/{path}/run", headers={"X-API-Key": "k"}
                    )
                    assert response.status_code == 409, (path, response.text)
                    assert "already in progress" in response.json()["detail"]
        finally:
            app.dependency_overrides.clear()


class TestDigest:
    def test_digest_skips_manual_rows_when_picking_the_latest_automation_run(self, factory):
        from app.services.telegram_digest import build_digest_text

        with factory() as db:
            for holder, results in (
                (None, {"bundesagentur": {"status": "ok", "counters": None}}),
                ("manual_collector:xing:1", {"manual:xing": {"status": "ok", "counters": None}}),
            ):
                db.add(
                    AutomationRunRecord(
                        account_key="me@example.com",
                        status="COMPLETED",
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                        lease_holder=holder,
                        results_json=json.dumps(results),
                    )
                )
                db.commit()
            text = build_digest_text(db, "me@example.com")
        assert "Latest run: #1" in text and "#2" not in text


class TestReviewRound1:
    @pytest.mark.asyncio
    async def test_cancellation_with_two_keys_never_touches_the_request_session(self, factory):
        from sqlalchemy import event

        settings = _settings(automation_scheduler_account_key="zed@example.com")
        statements: list[str] = []

        async def runner(db, settings, *, is_lease_lost=None):
            # from here on only the (simulated) surviving worker may use `db`
            event.listen(
                db.get_bind(),
                "before_cursor_execute",
                lambda conn, cur, stmt, params, ctx, many: statements.append(
                    f"{id(conn.engine)}:{stmt[:30]}"
                ),
            )
            statements.clear()
            raise asyncio.CancelledError

        with factory() as db, pytest.raises(asyncio.CancelledError):
            await run_collector_exclusively(db, settings, "xing", runner)
        # the lease extension used a SEPARATE session; its statements are UPDATEs only.
        assert all(
            not stmt.split(":", 1)[1].lstrip().upper().startswith("SELECT") for stmt in statements
        )
        rows = _runs(factory)
        assert [r.status for r in rows] == ["RUNNING", "RUNNING"]

    @pytest.mark.asyncio
    async def test_a_renewal_failing_during_heartbeat_shutdown_is_not_success(
        self, factory, monkeypatch
    ):
        real_stop = ce._RunLeaseHeartbeat.stop

        def stop_then_lose(self):
            real_stop(self)
            self.lease_lost.set()  # an in-flight renewal failed while joining

        monkeypatch.setattr(ce._RunLeaseHeartbeat, "stop", stop_then_lose)
        with factory() as db, pytest.raises(CollectorRunLeaseLostError):
            await run_collector_exclusively(db, _settings(), "xing", _ok)
        # the stored outcome must agree with the error: never COMPLETED
        assert [r.status for r in _runs(factory)] == ["FAILED"]

    @pytest.mark.asyncio
    async def test_a_heartbeat_that_will_not_stop_is_a_failed_not_a_successful_run(
        self, factory, monkeypatch
    ):
        monkeypatch.setattr(ce._RunLeaseHeartbeat, "is_alive", lambda self: True)
        with factory() as db, pytest.raises(CollectorRunLeaseLostError):
            await run_collector_exclusively(db, _settings(), "xing", _ok)
        assert [r.status for r in _runs(factory)] == ["FAILED"]

    @pytest.mark.asyncio
    async def test_gmail_unset_rest_key_still_excludes_a_manual_run(self, factory):
        settings = _settings(gmail_username="", automation_scheduler_account_key="zed@example.com")
        with factory() as db:
            create_running_run(db, account_key="", holder="rest_automation:1")
        with factory() as db, pytest.raises(CollectorRunInProgressError):
            await run_collector_exclusively(db, settings, "xing", _ok)

    def test_digest_recognizes_unfinished_manual_rows_and_filters_before_the_limit(self, factory):
        from app.services.telegram_digest import build_digest_text

        with factory() as db:
            db.add(
                AutomationRunRecord(
                    account_key="me@example.com",
                    status="COMPLETED",
                    started_at=datetime.now(UTC) - timedelta(days=1),
                    finished_at=datetime.now(UTC) - timedelta(days=1),
                    lease_holder="automation_run:me@example.com:abc",
                    results_json=json.dumps({"bundesagentur": {"status": "ok", "counters": None}}),
                )
            )
            db.commit()
            for i in range(30):  # newer manual rows, some unfinished with empty results
                db.add(
                    AutomationRunRecord(
                        account_key="me@example.com",
                        status="RUNNING" if i == 0 else "COMPLETED",
                        started_at=datetime.now(UTC) + timedelta(seconds=i),
                        lease_holder=f"manual_collector:xing:{i}",
                        results_json="{}",
                    )
                )
            db.commit()
            text = build_digest_text(db, "me@example.com")
        assert "Latest run: #1 status=COMPLETED" in text


class TestAutomationCrossKey:
    """Task 22: a cycle claims ONE key, then verifies no live run holds the other actual key."""

    @pytest.mark.asyncio
    async def test_a_live_run_under_the_other_key_makes_the_cycle_yield(self, factory, monkeypatch):
        from app.services import automation
        from app.services.automation import AutomationRunAlreadyInProgressError

        settings = _settings(automation_scheduler_account_key="Zed@Example.com")
        called = []

        async def never(*a, **k):  # the collectors must not start
            called.append(1)
            return dict(STATS)

        monkeypatch.setattr(automation, "AUTOMATION_STEPS", ("bundesagentur", "xing"))
        with factory() as db:
            create_running_run(db, account_key="Zed@Example.com", holder="scheduler:1")
        with factory() as db, pytest.raises(AutomationRunAlreadyInProgressError, match="yielded"):
            await automation.run_automation_cycle(
                db, account_key="me@example.com", settings=settings
            )
        assert called == []
        by_key = {r.account_key: r for r in _runs(factory)}
        assert by_key["Zed@Example.com"].status == "RUNNING"  # the live run is untouched
        assert by_key["me@example.com"].status == "FAILED"
        assert "Yielded" in (by_key["me@example.com"].error_summary or "")

    @pytest.mark.asyncio
    async def test_an_expired_lease_under_the_other_key_does_not_block(self, factory, monkeypatch):
        from app.services import automation

        settings = _settings(automation_scheduler_account_key="zed@example.com")
        with factory() as db:
            run, _ = create_running_run(db, account_key="zed@example.com", holder="crashed:1")
            db.execute(
                update(AutomationRunRecord)
                .where(AutomationRunRecord.id == run.id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
            )
            db.commit()

        async def ok_step(*a, **k):
            return dict(STATS)

        monkeypatch.setattr(automation, "run_bundesagentur", ok_step)
        monkeypatch.setattr(automation, "run_xing", ok_step)
        with factory() as db:
            record = await automation.run_automation_cycle(
                db, account_key="me@example.com", settings=settings
            )
        assert record.status in {"COMPLETED", "PARTIAL"}

    @pytest.mark.asyncio
    async def test_a_single_identity_installation_never_yields(self, factory, monkeypatch):
        from app.services import automation

        async def ok_step(*a, **k):
            return dict(STATS)

        monkeypatch.setattr(automation, "run_bundesagentur", ok_step)
        monkeypatch.setattr(automation, "run_xing", ok_step)
        with factory() as db:
            record = await automation.run_automation_cycle(
                db, account_key="me@example.com", settings=_settings()
            )
        assert record.status in {"COMPLETED", "PARTIAL"}


class TestHoldCollectorSlots:
    def test_the_slots_are_held_during_the_body_and_released_completed_after(self, factory):
        settings = _settings(automation_scheduler_account_key="zed@example.com")
        with factory() as db:
            with ce.hold_collector_slots(db, settings, "ba_refresh") as hold:
                inside = {r.account_key: r.status for r in _runs(factory)}
                assert not hold.lease_lost()
                hold.stats.update(created=0, updated=3, failed=0)
        assert inside == {"me@example.com": "RUNNING", "zed@example.com": "RUNNING"}
        runs = _runs(factory)
        assert {r.status for r in runs} == {"COMPLETED"}
        assert all(json.loads(r.results_json)["manual:ba_refresh"]["status"] == "ok" for r in runs)

    def test_a_taken_slot_refuses_before_the_body_runs(self, factory):
        with factory() as db:
            create_running_run(db, account_key="me@example.com", holder="scheduler:1")
        ran = []
        with factory() as db, pytest.raises(CollectorRunInProgressError):
            with ce.hold_collector_slots(db, _settings(), "ba_refresh"):
                ran.append(1)
        assert ran == []

    def test_a_body_exception_finalizes_failed_and_frees_the_slot(self, factory):
        with factory() as db, pytest.raises(RuntimeError):
            with ce.hold_collector_slots(db, _settings(), "ba_refresh"):
                raise RuntimeError("boom")
        assert [r.status for r in _runs(factory)] == ["FAILED"]
        with factory() as db:  # the slot is free again
            with ce.hold_collector_slots(db, _settings(), "ba_refresh"):
                pass

    def test_an_expired_lease_at_the_end_is_reported_not_succeeded(self, factory):
        with factory() as db, pytest.raises(CollectorRunLeaseLostError):
            with ce.hold_collector_slots(db, _settings(), "ba_refresh"):
                with factory() as side:
                    side.execute(
                        update(AutomationRunRecord).values(
                            lease_expires_at=datetime.now(UTC) - timedelta(seconds=5)
                        )
                    )
                    side.commit()
        assert [r.status for r in _runs(factory)] != ["COMPLETED"]


class TestLiveConflictingRun:
    """`live_conflicting_run` never trusts a plain read of an expired-looking row."""

    def test_a_live_row_is_a_conflict_and_is_left_alone(self, factory):
        from app.db.automation_repository import live_conflicting_run

        with factory() as db:
            other, _ = create_running_run(db, account_key="zed@example.com", holder="h:1")
            mine, _ = create_running_run(db, account_key="me@example.com", holder="h:2")
            found = live_conflicting_run(
                db, ["zed@example.com", "me@example.com"], exclude_run_id=mine.id
            )
            assert found is not None and found.id == other.id
        assert {r.account_key: r.status for r in _runs(factory)} == {
            "zed@example.com": "RUNNING",
            "me@example.com": "RUNNING",
        }

    def test_an_expired_row_is_reconciled_by_cas_and_is_no_conflict(self, factory):
        from app.db.automation_repository import live_conflicting_run

        with factory() as db:
            dead, _ = create_running_run(db, account_key="zed@example.com", holder="crashed:1")
            dead_id = dead.id
            db.execute(
                update(AutomationRunRecord)
                .where(AutomationRunRecord.id == dead_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
            )
            db.commit()
            mine, _ = create_running_run(db, account_key="me@example.com", holder="h:2")
            assert live_conflicting_run(db, ["zed@example.com"], exclude_run_id=mine.id) is None
        assert {r.id: r.status for r in _runs(factory)}[dead_id] == "FAILED"

    def test_a_renewal_that_commits_first_wins_over_the_expired_looking_read(
        self, factory, monkeypatch
    ):
        """The row still LOOKS expired to the first read, but its holder renews before the
        reconciliation CAS runs: the CAS loses and the row is a conflict."""
        from app.db import automation_repository as repo

        with factory() as db:
            other, _ = create_running_run(db, account_key="zed@example.com", holder="h:1")
            other_id = other.id
            db.execute(
                update(AutomationRunRecord)
                .where(AutomationRunRecord.id == other_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            db.commit()
            mine, _ = create_running_run(db, account_key="me@example.com", holder="h:2")

            real = repo.reconcile_stale_run_to_failed

            def renewed_first(session, record):
                with factory() as side:  # the holder's renewal commits just before the CAS
                    side.execute(
                        update(AutomationRunRecord)
                        .where(AutomationRunRecord.id == other_id)
                        .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=60))
                    )
                    side.commit()
                return real(session, record)

            monkeypatch.setattr(repo, "reconcile_stale_run_to_failed", renewed_first)
            found = repo.live_conflicting_run(db, ["zed@example.com"], exclude_run_id=mine.id)
            assert found is not None and found.id == other_id
        assert {r.id: r.status for r in _runs(factory)}[other_id] == "RUNNING"


class TestInterruptedCycleKeepsExclusion:
    @pytest.mark.asyncio
    async def test_a_cancelled_cycle_extends_the_lease_and_stays_running(
        self, factory, monkeypatch
    ):
        from app.services import automation

        started = asyncio.Event()

        async def hangs(*a, **k):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(automation, "run_bundesagentur", hangs)
        monkeypatch.setattr(automation, "run_xing", hangs)

        async def cycle():
            with factory() as db:
                await automation.run_automation_cycle(
                    db, account_key="me@example.com", settings=_settings()
                )

        task = asyncio.create_task(cycle())
        await asyncio.wait_for(started.wait(), timeout=20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (run,) = _runs(factory)
        assert run.status == "RUNNING"  # fail closed: reconciled only after the lease expires
        horizon = datetime.now(UTC) + timedelta(seconds=ce.CANCELLED_LEASE_EXTENSION_SECONDS - 30)
        expires = run.lease_expires_at
        expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
        assert expires > horizon

    def test_a_renewal_never_shortens_the_expiry(self, factory):
        from app.db.automation_repository import renew_run_lease

        with factory() as db:
            run, _ = create_running_run(db, account_key="me@example.com", holder="h:1")
            assert renew_run_lease(db, run.id, holder="h:1", ttl_seconds=3600)
            long_expiry = db.get(AutomationRunRecord, run.id).lease_expires_at
            assert renew_run_lease(db, run.id, holder="h:1", ttl_seconds=1)  # a late, short renewal
            db.expire_all()
            assert db.get(AutomationRunRecord, run.id).lease_expires_at == long_expiry
            # a longer renewal still extends it
            assert renew_run_lease(db, run.id, holder="h:1", ttl_seconds=7200)
            db.expire_all()
            assert db.get(AutomationRunRecord, run.id).lease_expires_at > long_expiry

    @pytest.mark.asyncio
    async def test_a_renewal_slower_than_the_heartbeat_join_cannot_shorten_the_extension(
        self, factory, monkeypatch
    ):
        """The heartbeat's `stop()` joins for at most interval + 1 s. A renewal that takes
        longer survives it and commits AFTER the extension; being monotonic, it cannot undo it."""
        import time

        from app.services import automation

        real_renew = automation.renew_run_lease

        def very_slow_renewal(db, run_id, *, holder, ttl_seconds):
            if ttl_seconds < 2:
                time.sleep(1.4)  # longer than interval (0.02) + 1.0
            return real_renew(db, run_id, holder=holder, ttl_seconds=ttl_seconds)

        monkeypatch.setattr(automation, "renew_run_lease", very_slow_renewal)
        started = asyncio.Event()

        async def hangs(*a, **k):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(automation, "run_bundesagentur", hangs)
        monkeypatch.setattr(automation, "run_xing", hangs)

        async def cycle():
            with factory() as db:
                await automation.run_automation_cycle(
                    db,
                    account_key="me@example.com",
                    settings=_settings(),
                    lease_ttl_seconds=1.5,
                    heartbeat_interval_seconds=0.02,
                )

        task = asyncio.create_task(cycle())
        await asyncio.wait_for(started.wait(), timeout=20)
        await asyncio.sleep(0.1)  # a very slow renewal is now mid-flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.6)  # the straggler has landed
        (run,) = _runs(factory)
        expires = run.lease_expires_at
        expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
        assert expires > datetime.now(UTC) + timedelta(
            seconds=ce.CANCELLED_LEASE_EXTENSION_SECONDS - 30
        )

    @pytest.mark.asyncio
    async def test_a_late_heartbeat_renewal_cannot_shorten_the_extension(
        self, factory, monkeypatch
    ):
        """Regression: a renewal that is still IN FLIGHT when the cycle is interrupted would
        commit after the extension and shorten the expiry back to one TTL. Renewals are made
        slow here so one is certainly mid-flight at the cancellation; the extension must wait
        for the heartbeat to be stopped and drained."""
        import time

        from app.services import automation

        real_renew = automation.renew_run_lease

        def slow_heartbeat_renewal(db, run_id, *, holder, ttl_seconds):
            if ttl_seconds < 2:  # the heartbeat's own renewals, not the extension
                time.sleep(0.3)
            return real_renew(db, run_id, holder=holder, ttl_seconds=ttl_seconds)

        monkeypatch.setattr(automation, "renew_run_lease", slow_heartbeat_renewal)
        started = asyncio.Event()

        async def hangs(*a, **k):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(automation, "run_bundesagentur", hangs)
        monkeypatch.setattr(automation, "run_xing", hangs)

        async def cycle():
            with factory() as db:
                await automation.run_automation_cycle(
                    db,
                    account_key="me@example.com",
                    settings=_settings(),
                    lease_ttl_seconds=1.0,
                    heartbeat_interval_seconds=0.02,
                )

        task = asyncio.create_task(cycle())
        await asyncio.wait_for(started.wait(), timeout=20)
        await asyncio.sleep(0.1)  # a slow renewal is now mid-flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.6)  # any straggler would have landed by now
        (run,) = _runs(factory)
        expires = run.lease_expires_at
        expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
        assert expires > datetime.now(UTC) + timedelta(
            seconds=ce.CANCELLED_LEASE_EXTENSION_SECONDS - 30
        )
