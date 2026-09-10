"""API tests for Stage 8A's orchestrator endpoints: POST /automation/runs,
GET /automation/runs/{id}, GET /automation/runs.

Mirrors tests/test_collector_endpoint.py and
tests/test_collector_xing_endpoint.py's fake-collector approach (no real
network I/O anywhere) — this suite additionally covers run-level
coordination: per-step isolation (PARTIAL/FAILED), account scoping,
concurrent-duplicate-run rejection, and that GET never has a side
effect.
"""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.bundesagentur import BundesagenturAPIError
from app.collectors.xing_email import XingConnectionError, XingEmailBatch
from app.core.config import Settings
from app.db.automation_repository import create_running_run
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.job import Job
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"
ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"


class FakeBundesagenturCollector:
    def __init__(
        self, jobs: list[Job] | None = None, error: Exception | None = None, delay: float = 0.0
    ) -> None:
        self._jobs = jobs or []
        self._error = error
        self._delay = delay
        self.skipped_invalid_count = 0

    async def fetch(self, since=None) -> list[Job]:
        if self._delay:
            # asyncio.sleep, never time.sleep: this must actually yield
            # control back to the event loop so two concurrent requests
            # can genuinely interleave (see
            # TestConcurrentDuplicateRunProtection) — a blocking sleep
            # would serialize them regardless of threading.
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._jobs

    async def fetch_detail(self, source_reference: str) -> str | None:
        return None


class FakeXingCollector:
    def __init__(
        self,
        jobs: list[Job] | None = None,
        error: Exception | None = None,
        message_id: str = "<fake-digest@mail.xing.com>",
    ) -> None:
        self._jobs = jobs or []
        self._error = error
        self._message_id = message_id
        self.skipped_invalid_count = 0
        self.deadline_exceeded = False
        # Codex gate follow-up (Astra R4A MEDIUM, starvation): run_xing
        # always reads these after awaiting fetch_message_batches() to
        # persist the scan watermark -- see
        # app.db.xing_scan_progress_repository. None is a legitimate
        # value here (this fake never determines a real UIDVALIDITY) and
        # simply makes run_xing skip the watermark-advance step.
        self.uid_validity: int | None = None
        self.confirmed_uids: list[int] = []

    async def fetch_message_batches(self, since=None) -> list[XingEmailBatch]:
        if self._error is not None:
            raise self._error
        return [XingEmailBatch(message_id=self._message_id, jobs=tuple(self._jobs), uid=1)]


def _ba_job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "Example GmbH",
        "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1184867112-S",
        "description": "",
        "source_reference": "10000-1184867112-S",
        "skills": ["python"],
    }
    data.update(overrides)
    return Job(**data)


def _xing_job(**overrides) -> Job:
    data = {
        "source": "xing",
        "title": "Junior Informatiker (m/w/d)",
        "company": "Institut fur Kommunikationsforschung gGmbH",
        "location": "Heidelberg",
        "url": "https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1",
        "description": "",
        "skills": [],
    }
    data.update(overrides)
    return Job(**data)


def _settings_for(account: str, **overrides) -> Settings:
    data = dict(
        api_key=API_KEY,
        rate_limit_requests=1000,
        rate_limit_window_seconds=60,
        gmail_username=account,
        bundesagentur_api_key="upstream-key",
        xing_mailbox_username="xing-user@example.com",
        xing_mailbox_app_password="app-password",
    )
    data.update(overrides)
    return Settings(**data)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_automation_endpoints.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    fake_settings = _settings_for(ACCOUNT)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._automation_run_requests.clear()
    rate_limit_module._collector_requests.clear()
    rate_limit_module._xing_requests.clear()

    # Default happy-path fakes — individual tests override these via
    # monkeypatch as needed.
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector",
        lambda **kwargs: FakeBundesagenturCollector(),
    )
    monkeypatch.setattr(
        "app.services.collector_runner.XingEmailCollector",
        lambda **kwargs: FakeXingCollector(),
    )

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._automation_run_requests.clear()
    rate_limit_module._collector_requests.clear()
    rate_limit_module._xing_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


class TestSuccessfulCycle:
    def test_both_collectors_succeed_marks_completed(self, client, monkeypatch):
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs=[_ba_job()]),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(jobs=[_xing_job()]),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["account_key"] == ACCOUNT
        assert body["error_summary"] is None
        assert body["results"]["bundesagentur"]["status"] == "ok"
        assert body["results"]["bundesagentur"]["counters"]["created"] == 1
        assert body["results"]["xing"]["status"] == "ok"
        assert body["results"]["xing"]["counters"]["created"] == 1
        assert body["finished_at"] is not None

    def test_requires_api_key_auth(self, client):
        test_client, _session_factory = client
        response = test_client.post("/api/v1/automation/runs")
        assert response.status_code == 401


class TestPartialAndFailedOutcomes:
    def test_one_collector_fails_other_succeeds_is_partial(self, client, monkeypatch):
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs=[_ba_job()]),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(error=XingConnectionError("boom")),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "PARTIAL"
        assert body["results"]["bundesagentur"]["status"] == "ok"
        assert body["results"]["bundesagentur"]["counters"]["created"] == 1
        assert body["results"]["xing"]["status"] == "failed"
        assert body["results"]["xing"]["error_type"] == "XingConnectionError"
        assert "xing" in body["error_summary"]
        assert "bundesagentur" not in body["error_summary"]

    def test_bundesagentur_not_configured_xing_succeeds_is_partial(self, client, monkeypatch):
        test_client, _session_factory = client
        unconfigured = _settings_for(ACCOUNT, bundesagentur_api_key="")
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(jobs=[_xing_job()]),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "PARTIAL"
        assert body["results"]["bundesagentur"]["status"] == "not_configured"
        assert body["results"]["xing"]["status"] == "ok"

    def test_both_collectors_fail_is_failed(self, client, monkeypatch):
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(error=BundesagenturAPIError("boom")),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(error=XingConnectionError("boom")),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "FAILED"
        assert body["results"]["bundesagentur"]["status"] == "failed"
        assert body["results"]["xing"]["status"] == "failed"
        assert "bundesagentur" in body["error_summary"]
        assert "xing" in body["error_summary"]

    def test_both_not_configured_is_failed(self, client, monkeypatch):
        test_client, _session_factory = client
        unconfigured = _settings_for(
            ACCOUNT,
            bundesagentur_api_key="",
            xing_mailbox_username="",
            xing_mailbox_app_password="",
        )
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "FAILED"
        assert body["results"]["bundesagentur"]["status"] == "not_configured"
        assert body["results"]["xing"]["status"] == "not_configured"


class TestItemLevelBusinessOutcome:
    """AUD-009 (Astra R2): `run_bundesagentur`/`run_xing` isolate PER-JOB
    persist failures internally (their own try/except, never propagating
    — see collector_runner.py) and report them via `counters["failed"]`,
    so a collector step function returning WITHOUT raising only means it
    didn't crash, not that any actual work succeeded. The step's own
    reported `status` (and therefore the run's overall status) must
    reflect the real business outcome of the attempted items, not just
    "the call didn't raise".
    """

    def test_all_fetched_items_failing_to_persist_is_run_failed_not_completed(
        self, client, monkeypatch
    ):
        from app.services import collector_runner as collector_runner_module

        def _always_fail(db, profile, job):
            raise ValueError("simulated persist failure")

        monkeypatch.setattr(collector_runner_module, "score_and_persist", _always_fail)

        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs=[_ba_job()]),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(jobs=[_xing_job()]),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        # Before the AUD-009 fix, both steps reported "ok" here (neither
        # run_bundesagentur nor run_xing itself raised) and the run would
        # have wrongly reported COMPLETED even though every single
        # fetched job failed to persist.
        assert body["results"]["bundesagentur"]["status"] == "failed"
        assert body["results"]["bundesagentur"]["counters"]["failed"] == 1
        assert body["results"]["bundesagentur"]["counters"]["created"] == 0
        assert body["results"]["xing"]["status"] == "failed"
        assert body["results"]["xing"]["counters"]["failed"] == 1
        assert body["status"] == "FAILED"

    def test_some_items_succeed_some_fail_is_step_partial_and_run_partial(
        self, client, monkeypatch
    ):
        from app.services import collector_runner as collector_runner_module

        real_score_and_persist = collector_runner_module.score_and_persist

        def _fail_for_marked_job(db, profile, job):
            if str(job.url).endswith("/FAIL"):
                raise ValueError("simulated persist failure")
            return real_score_and_persist(db, profile, job)

        monkeypatch.setattr(collector_runner_module, "score_and_persist", _fail_for_marked_job)

        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(
                jobs=[
                    _ba_job(
                        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/OK",
                        source_reference="10000-1184867112-A",
                    ),
                    _ba_job(
                        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/FAIL",
                        source_reference="10000-1184867112-B",
                    ),
                ]
            ),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(jobs=[_xing_job()]),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        assert body["results"]["bundesagentur"]["status"] == "partial"
        assert body["results"]["bundesagentur"]["counters"]["created"] == 1
        assert body["results"]["bundesagentur"]["counters"]["failed"] == 1
        assert body["results"]["xing"]["status"] == "ok"
        assert body["status"] == "PARTIAL"

    def test_zero_fetched_items_is_step_ok_not_failed(self, client, monkeypatch):
        """No-op / no-eligible-items must never be misreported as a
        business failure -- zero attempted items is a legitimate "ok".
        """
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs=[]),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(jobs=[]),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 201
        body = response.json()
        assert body["results"]["bundesagentur"]["status"] == "ok"
        assert body["results"]["bundesagentur"]["counters"]["failed"] == 0
        assert body["results"]["xing"]["status"] == "ok"
        assert body["status"] == "COMPLETED"


class TestErrorSummarySanitization:
    def test_error_summary_never_leaks_raw_exception_text(self, client, monkeypatch):
        """GMAIL-003-style sanitization: only type(exc).__name__ may ever
        appear — never the exception's own message, which could carry a
        server-echoed/sensitive detail."""
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(
                error=BundesagenturAPIError("secret-upstream-detail")
            ),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(error=XingConnectionError("secret-upstream-detail")),
        )

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert "secret-upstream-detail" not in response.text


class TestDedupRemainsCorrect:
    def test_second_run_deduplicates_bundesagentur_job_via_fingerprint(self, client, monkeypatch):
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs=[_ba_job()]),
        )

        first = test_client.post("/api/v1/automation/runs", headers=_auth_headers())
        second = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["results"]["bundesagentur"]["counters"] == {
            "fetched": 1,
            "created": 1,
            "updated": 0,
            "skipped_invalid": 0,
            "failed": 0,
        }
        assert second.json()["results"]["bundesagentur"]["counters"] == {
            "fetched": 1,
            "created": 0,
            "updated": 1,
            "skipped_invalid": 0,
            "failed": 0,
        }

        jobs = test_client.get("/api/v1/jobs", headers=_auth_headers()).json()
        assert len(jobs) == 1


class TestAccountIsolation:
    def test_runs_are_scoped_to_the_current_account(self, client, monkeypatch):
        test_client, _session_factory = client
        monkeypatch.setattr("app.api.routes.get_settings", lambda: _settings_for(ACCOUNT))
        first_run = test_client.post("/api/v1/automation/runs", headers=_auth_headers()).json()

        monkeypatch.setattr("app.api.routes.get_settings", lambda: _settings_for(OTHER_ACCOUNT))
        second_run = test_client.post("/api/v1/automation/runs", headers=_auth_headers()).json()

        # Account B cannot read account A's run by id.
        cross_account_get = test_client.get(
            f"/api/v1/automation/runs/{first_run['id']}", headers=_auth_headers()
        )
        assert cross_account_get.status_code == 404

        # Account B's list never includes account A's run.
        listed = test_client.get("/api/v1/automation/runs", headers=_auth_headers()).json()
        ids = {run["id"] for run in listed}
        assert second_run["id"] in ids
        assert first_run["id"] not in ids

        # Switching back to account A, its own run is visible again.
        monkeypatch.setattr("app.api.routes.get_settings", lambda: _settings_for(ACCOUNT))
        own_get = test_client.get(
            f"/api/v1/automation/runs/{first_run['id']}", headers=_auth_headers()
        )
        assert own_get.status_code == 200
        assert own_get.json()["account_key"] == ACCOUNT


class TestConcurrentDuplicateRunProtection:
    def test_second_request_while_one_is_running_fails_closed(self, client, monkeypatch):
        test_client, session_factory = client
        db = session_factory()
        try:
            create_running_run(db, account_key=ACCOUNT, holder="pre-existing-holder")
        finally:
            db.close()

        response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert response.status_code == 409

    def test_two_real_concurrent_requests_only_one_wins(self, client, monkeypatch):
        """A genuine two-thread race against the same TestClient/DB file —
        not a simulated same-session ordering — proving the DB-enforced
        partial unique index (uq_automation_runs_one_running_per_account),
        not just application-level luck, is what serializes this."""
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs=[_ba_job()], delay=0.5),
        )

        results: list[int] = []
        barrier = threading.Barrier(2)

        def _post():
            barrier.wait(timeout=5)
            resp = test_client.post("/api/v1/automation/runs", headers=_auth_headers())
            results.append(resp.status_code)

        threads = [threading.Thread(target=_post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert sorted(results) == [201, 409]


class TestGetEndpointsHaveNoSideEffects:
    def test_get_list_never_touches_collectors(self, client, monkeypatch):
        test_client, _session_factory = client

        def _boom(**kwargs):
            raise AssertionError("GET must never construct a collector")

        monkeypatch.setattr("app.services.collector_runner.BundesagenturCollector", _boom)
        monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _boom)

        first = test_client.get("/api/v1/automation/runs", headers=_auth_headers())
        second = test_client.get("/api/v1/automation/runs", headers=_auth_headers())

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json() == []

    def test_repeated_get_by_id_returns_identical_unchanged_state(self, client, monkeypatch):
        test_client, _session_factory = client
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs=[_ba_job()]),
        )
        created = test_client.post("/api/v1/automation/runs", headers=_auth_headers()).json()

        def _boom(**kwargs):
            raise AssertionError("GET must never construct a collector")

        monkeypatch.setattr("app.services.collector_runner.BundesagenturCollector", _boom)
        monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _boom)

        first = test_client.get(f"/api/v1/automation/runs/{created['id']}", headers=_auth_headers())
        second = test_client.get(
            f"/api/v1/automation/runs/{created['id']}", headers=_auth_headers()
        )

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json() == created

    def test_get_unknown_run_returns_404(self, client):
        test_client, _session_factory = client
        response = test_client.get("/api/v1/automation/runs/999999", headers=_auth_headers())
        assert response.status_code == 404

    def test_rate_limit_applies_to_post(self, client, monkeypatch):
        test_client, _session_factory = client
        monkeypatch.setattr("app.security.rate_limit.AUTOMATION_RUN_RATE_LIMIT_REQUESTS", 1)
        rate_limit_module._automation_run_requests.clear()

        first = test_client.post("/api/v1/automation/runs", headers=_auth_headers())
        second = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert first.status_code == 201
        assert second.status_code == 429


class TestSanitizedStepFailureLogging:
    """S8A-004 (Codex re-review, sanitized logging): an UNEXPECTED
    (non-CollectorError) step failure must never leak the raw exception
    text into logs or the persisted/returned result -- only the step name
    and type(exc).__name__ are ever recorded. Before this fix,
    app.services.automation used logger.exception(...), which attaches
    exc_info=True and logs the full traceback INCLUDING the exception's
    own str(exc) message.
    """

    def test_secret_bearing_exception_never_appears_in_logs_or_response(
        self, client, monkeypatch, caplog
    ):
        test_client, _session_factory = client

        def _boom(db, settings, *, touched_jobs=None, is_lease_lost=None):
            raise RuntimeError("secret-upstream-detail-should-never-leak")

        # A raw callable, not a CollectorError subclass -- exercises the
        # "truly unexpected failure" branch in app.services.automation._run_step.
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _boom)
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: FakeXingCollector(jobs=[_xing_job()]),
        )

        with caplog.at_level("DEBUG"):
            response = test_client.post("/api/v1/automation/runs", headers=_auth_headers())

        assert "secret-upstream-detail-should-never-leak" not in response.text
        assert "secret-upstream-detail-should-never-leak" not in caplog.text

        body = response.json()
        assert body["results"]["bundesagentur"]["status"] == "failed"
        assert body["results"]["bundesagentur"]["error_type"] == "RuntimeError"
        assert body["results"]["bundesagentur"]["counters"] is None
        assert "RuntimeError" in body["error_summary"]
        assert "secret-upstream-detail-should-never-leak" not in body["error_summary"]

        # Persisted state (not just the immediate HTTP response) is
        # equally sanitized.
        run_id = body["id"]
        fetched = test_client.get(f"/api/v1/automation/runs/{run_id}", headers=_auth_headers())
        assert "secret-upstream-detail-should-never-leak" not in fetched.text
