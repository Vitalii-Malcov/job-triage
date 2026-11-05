"""Review 2.4: blocking service work runs off the web event loop
(app/services/offload.py) and endpoints stay responsive meanwhile."""

import asyncio
import threading
import time

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.base import CollectorError
from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.job import JobScore
from app.security import rate_limit as rate_limit_module
from app.services import offload
from app.services.offload import run_off_loop

API_KEY = "offload-test-key"
HEADERS = {"X-API-Key": API_KEY}


class TestRunOffLoop:
    @pytest.mark.asyncio
    async def test_runs_on_another_thread_and_loop_and_returns_the_value(self):
        caller = (threading.get_ident(), asyncio.get_running_loop())

        async def work():
            return threading.get_ident(), asyncio.get_running_loop()

        thread, loop = await run_off_loop(work)
        assert thread != caller[0] and loop is not caller[1]

    @pytest.mark.asyncio
    async def test_exceptions_propagate_unchanged(self):
        class Upstream(CollectorError):
            pass

        async def work():
            raise Upstream("boom")

        with pytest.raises(Upstream, match="boom"):
            await run_off_loop(work)

    @pytest.mark.asyncio
    async def test_the_event_loop_stays_free_while_the_worker_blocks(self):
        entered = threading.Event()

        async def work():
            entered.set()
            time.sleep(0.6)  # synchronous blocking, like a sync DB call
            return "done"

        task = asyncio.create_task(run_off_loop(work))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        ticks = 0
        started = time.monotonic()
        while not task.done():
            await asyncio.sleep(0.02)
            ticks += 1
        assert ticks >= 10  # the loop kept running while the worker was blocked
        assert time.monotonic() - started >= 0.3
        assert task.result() == "done"

    def test_uses_a_dedicated_bounded_executor(self):
        assert offload.OFFLOAD_MAX_WORKERS == 8
        assert offload._EXECUTOR._max_workers == 8
        assert offload._EXECUTOR._thread_name_prefix == "offload"


class TestCancellation:
    @pytest.mark.asyncio
    async def test_a_cancelled_caller_waits_for_the_worker_before_cancellation_surfaces(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        async def work():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            finished.set()
            return "late"

        task = asyncio.create_task(run_off_loop(work))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.2)
        # cancellation has NOT been delivered while the worker still uses the session
        assert not task.done()
        task.cancel()  # repeated cancellation is absorbed too
        await asyncio.sleep(0.1)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()  # the worker had completed before we were released

    @pytest.mark.asyncio
    async def test_worker_failure_after_cancellation_is_retrieved_not_leaked(self, recwarn):
        entered, release = threading.Event(), threading.Event()

        async def work():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            raise RuntimeError("failed after the caller was cancelled")

        task = asyncio.create_task(run_off_loop(work))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        import gc

        gc.collect()
        assert not [w for w in recwarn if "never retrieved" in str(w.message)]


@pytest.fixture()
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'offload.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    settings = Settings(
        _env_file=None,
        api_key=API_KEY,
        rate_limit_requests=1000,
        xing_mailbox_username="x@example.com",
        xing_mailbox_app_password="pw",
        bundesagentur_api_key="ba",
        gmail_username="me@example.com",
    )
    app.dependency_overrides[get_db] = override
    for target in ("app.security.auth", "app.security.rate_limit", "app.api.routers.common"):
        monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
    for bucket in (
        rate_limit_module._requests,
        rate_limit_module._collector_requests,
        rate_limit_module._xing_requests,
        rate_limit_module._automation_run_requests,
    ):
        bucket.clear()
    yield
    app.dependency_overrides.clear()
    engine.dispose()


async def _responsive_while(slow_request, entered: threading.Event) -> float:
    """Fire `slow_request`, wait until its worker is blocking, then time /health."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow = asyncio.create_task(slow_request(client))
        deadline = time.monotonic() + 20
        while not entered.is_set():
            assert time.monotonic() < deadline, "the slow endpoint never reached its worker"
            await asyncio.sleep(0.01)
        started = time.monotonic()
        health = await client.get("/api/v1/health")
        elapsed = time.monotonic() - started
        assert health.status_code == 200
        assert not slow.done(), "the slow request must still be running"
        response = await slow
        return elapsed, response


class TestEndpointsStayResponsive:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("collector", ["xing", "bundesagentur"])
    async def test_collector_endpoints(self, api, monkeypatch, collector):
        entered = threading.Event()

        async def blocking_runner(db, settings, **kwargs):
            entered.set()
            time.sleep(1.2)  # a sync DB-heavy stretch
            return {
                "fetched": 0,
                "created": 0,
                "updated": 0,
                "skipped_invalid": 0,
                "failed": 0,
            }

        monkeypatch.setattr(f"app.api.routers.collectors.run_{collector}", blocking_runner)
        elapsed, response = await _responsive_while(
            lambda c: c.post(f"/api/v1/collectors/{collector}/run", headers=HEADERS), entered
        )
        assert elapsed < 0.6, f"/health took {elapsed:.2f}s while a collector run blocked"
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_automation_run_endpoint(self, api, monkeypatch):
        from app.services.automation import AutomationRunAlreadyInProgressError

        entered = threading.Event()

        async def blocking_cycle(db, **kwargs):
            entered.set()
            time.sleep(1.2)
            raise AutomationRunAlreadyInProgressError("busy")

        monkeypatch.setattr("app.api.routers.automation.run_automation_cycle", blocking_cycle)
        elapsed, response = await _responsive_while(
            lambda c: c.post("/api/v1/automation/runs", headers=HEADERS), entered
        )
        assert elapsed < 0.6
        assert response.status_code == 409  # HTTP mapping unchanged

    @pytest.mark.asyncio
    async def test_score_endpoint(self, api, monkeypatch):
        from types import SimpleNamespace

        entered = threading.Event()

        def blocking_score(db, skills, job):
            entered.set()
            time.sleep(1.2)
            result = JobScore(score=10, recommendation="SKIP")
            return SimpleNamespace(id=1), result, True

        monkeypatch.setattr("app.api.routers.jobs.score_and_persist", blocking_score)
        body = {
            "source": "manual",
            "title": "Role",
            "company": "Co",
            "location": "Berlin",
            "url": "https://example.com/jobs/1",
        }
        elapsed, response = await _responsive_while(
            lambda c: c.post("/api/v1/jobs/score", json=body, headers=HEADERS), entered
        )
        assert elapsed < 0.6
        assert response.status_code == 200 and response.json()["score"] == 10

    @pytest.mark.asyncio
    async def test_gmail_sync_endpoint(self, api, monkeypatch):
        from app.models.gmail import GmailSyncResult

        entered = threading.Event()

        async def blocking_sync(db, settings):
            entered.set()
            time.sleep(1.2)
            return GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.api.routers.gmail.run_gmail_sync_service", blocking_sync)
        rate_limit_module._gmail_requests.clear()
        elapsed, response = await _responsive_while(
            lambda c: c.post("/api/v1/gmail/sync", headers=HEADERS), entered
        )
        assert elapsed < 0.6
        assert response.status_code == 200
