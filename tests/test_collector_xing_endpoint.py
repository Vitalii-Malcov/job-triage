import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.xing_email import XingConnectionError, XingEmailBatch
from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.job import Job, JobScore
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


class FakeJobScorer:
    """Stand-in for JobScorer that returns a fixed JobScore per job title.

    Notification routing (score/recommendation -> send_job) is what these
    tests exercise, not the real scoring heuristics — those are covered by
    tests/test_job_scorer.py.
    """

    def __init__(self, scores_by_title: dict[str, JobScore]) -> None:
        self._scores_by_title = scores_by_title

    def score(self, job: Job) -> JobScore:
        return self._scores_by_title[job.title]


def _job_score(**overrides) -> JobScore:
    data = {
        "score": 90,
        "recommendation": "APPLY",
        "data_confidence": 0.9,
    }
    data.update(overrides)
    return JobScore(**data)


class FakeTelegramNotifier:
    """Records send_job calls; queued results let a test script per-call
    success/failure/exception without touching real Telegram HTTP calls."""

    def __init__(self, results: list[bool | Exception] | None = None) -> None:
        self.calls: list[tuple[Job, JobScore]] = []
        self._results = list(results) if results else None

    async def send_job(self, job: Job, score: JobScore) -> bool:
        self.calls.append((job, score))
        if self._results:
            outcome = self._results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return True


class FakeCollector:
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

    async def fetch_message_batches(self, since=None) -> list[XingEmailBatch]:
        if self._error is not None:
            raise self._error
        return [XingEmailBatch(message_id=self._message_id, jobs=tuple(self._jobs))]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_collector_xing_endpoint.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    fake_settings = Settings(
        api_key=API_KEY,
        rate_limit_requests=1000,
        rate_limit_window_seconds=60,
        xing_mailbox_username="user@example.com",
        xing_mailbox_app_password="app-password",
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._xing_requests.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._xing_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _sample_job(**overrides) -> Job:
    data = {
        "source": "xing",
        "title": "Junior Informatiker (m/w/d)",
        "company": "Institut für Kommunikations- und Prüfungsforschung gGmbH",
        "location": "Heidelberg",
        "url": "https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1",
        "description": "",
        "skills": [],
    }
    data.update(overrides)
    return Job(**data)


class TestRunXingCollector:
    def test_requires_api_key_auth(self, client):
        response = client.post("/api/v1/collectors/xing/run")
        assert response.status_code == 401

    def test_missing_mailbox_config_returns_503(self, client, monkeypatch):
        unconfigured = Settings(
            api_key=API_KEY,
            rate_limit_requests=1000,
            xing_mailbox_username="",
            xing_mailbox_app_password="",
        )
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)

        response = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert response.status_code == 503

    def test_missing_mailbox_config_returns_503_for_whitespace_only_password(
        self, client, monkeypatch
    ):
        unconfigured = Settings(
            api_key=API_KEY,
            rate_limit_requests=1000,
            xing_mailbox_username="user@example.com",
            xing_mailbox_app_password="   ",
        )
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)

        response = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert response.status_code == 503

    def test_successful_run_reports_created_count(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.XingEmailCollector",
            lambda **kwargs: FakeCollector(jobs=[_sample_job()]),
        )

        response = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert response.status_code == 200
        assert response.json() == {
            "fetched": 1,
            "created": 1,
            "updated": 0,
            "skipped_invalid": 0,
            "failed": 0,
        }

    def test_second_run_deduplicates_via_fingerprint(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.XingEmailCollector",
            lambda **kwargs: FakeCollector(jobs=[_sample_job()]),
        )

        first = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())
        second = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert first.json() == {
            "fetched": 1,
            "created": 1,
            "updated": 0,
            "skipped_invalid": 0,
            "failed": 0,
        }
        assert second.json() == {
            "fetched": 1,
            "created": 0,
            "updated": 1,
            "skipped_invalid": 0,
            "failed": 0,
        }

    def test_second_run_deduplicates_even_with_different_tracking_url(self, client, monkeypatch):
        # Same real posting, different per-recipient tracking URL across two
        # separate digest emails — this is the exact scenario the xing
        # fingerprint fields (no url) exist to handle. See
        # app/db/repositories.py's _FINGERPRINT_FIELDS_BY_SOURCE.
        calls = iter(
            [
                [_sample_job(url="https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1")],
                [_sample_job(url="https://www.xing.com/m/BBBBBBBBBBBBBBBBBBBB2")],
            ]
        )
        monkeypatch.setattr(
            "app.api.routes.XingEmailCollector",
            lambda **kwargs: FakeCollector(jobs=next(calls)),
        )

        first = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())
        second = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert first.json()["created"] == 1
        assert second.json() == {
            "fetched": 1,
            "created": 0,
            "updated": 1,
            "skipped_invalid": 0,
            "failed": 0,
        }

    def test_upstream_failure_returns_502(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.XingEmailCollector",
            lambda **kwargs: FakeCollector(error=XingConnectionError("boom")),
        )

        response = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert response.status_code == 502

    def test_xing_rate_limit_is_stricter_than_general_limit(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.XingEmailCollector",
            lambda **kwargs: FakeCollector(jobs=[_sample_job()]),
        )
        monkeypatch.setattr("app.security.rate_limit.XING_RATE_LIMIT_REQUESTS", 1)
        rate_limit_module._xing_requests.clear()

        first = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())
        second = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert first.status_code == 200
        assert second.status_code == 429

    def test_partial_failure_reports_failed_count_and_does_not_lose_already_committed_jobs(
        self, client, monkeypatch
    ):
        jobs = [
            _sample_job(title="Job One", url="https://www.xing.com/m/1"),
            _sample_job(title="Job Two", url="https://www.xing.com/m/2"),
            _sample_job(title="Job Three", url="https://www.xing.com/m/3"),
        ]
        monkeypatch.setattr(
            "app.api.routes.XingEmailCollector",
            lambda **kwargs: FakeCollector(jobs=jobs),
        )

        import app.api.routes as routes_module

        original_upsert_job = routes_module.upsert_job
        call_count = {"n": 0}

        def flaky_upsert_job(db, job, score):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated persistence failure")
            return original_upsert_job(db, job, score)

        monkeypatch.setattr("app.api.routes.upsert_job", flaky_upsert_job)

        acknowledged: set[str] = set()
        monkeypatch.setattr(
            "app.api.routes.mark_message_processed",
            lambda db, source, message_id: acknowledged.add(message_id),
        )

        first = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert first.status_code == 200
        assert first.json() == {
            "fetched": 3,
            "created": 2,
            "updated": 0,
            "skipped_invalid": 0,
            "failed": 1,
        }
        assert acknowledged == set()

        # The message is retried because it was not acknowledged. Jobs One
        # and Three are deduplicated; the previously failed Job Two is now
        # created. Only after all three succeed is the Message-ID marked.
        second = client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

        assert second.status_code == 200
        assert second.json() == {
            "fetched": 3,
            "created": 1,
            "updated": 2,
            "skipped_invalid": 0,
            "failed": 0,
        }
        assert acknowledged == {"<fake-digest@mail.xing.com>"}

        list_response = client.get("/api/v1/jobs", headers=_auth_headers())
        titles = {item["title"] for item in list_response.json()}
        assert titles == {"Job One", "Job Two", "Job Three"}


class TestXingCollectorNotifications:
    def _run(self, client, monkeypatch, jobs, scores_by_title, notifier):
        monkeypatch.setattr(
            "app.api.routes.XingEmailCollector",
            lambda **kwargs: FakeCollector(jobs=jobs),
        )
        monkeypatch.setattr(
            "app.api.routes.JobScorer",
            lambda profile_skills: FakeJobScorer(scores_by_title),
        )
        monkeypatch.setattr("app.api.routes.TelegramNotifier", lambda **kwargs: notifier)
        return client.post("/api/v1/collectors/xing/run", headers=_auth_headers())

    def test_sends_notification_for_apply_job_above_threshold(self, client, monkeypatch):
        job = _sample_job(title="Senior Python Dev")
        notifier = FakeTelegramNotifier()

        response = self._run(
            client,
            monkeypatch,
            jobs=[job],
            scores_by_title={"Senior Python Dev": _job_score(score=90, recommendation="APPLY")},
            notifier=notifier,
        )

        assert response.status_code == 200
        assert len(notifier.calls) == 1
        sent_job, sent_score = notifier.calls[0]
        assert sent_job.title == "Senior Python Dev"
        assert sent_score.score == 90

    @pytest.mark.parametrize("recommendation", ["MAYBE", "SKIP", "NEEDS_ENRICHMENT"])
    def test_does_not_notify_for_non_apply_recommendations(
        self, client, monkeypatch, recommendation
    ):
        job = _sample_job(title="Some Job")
        notifier = FakeTelegramNotifier()

        response = self._run(
            client,
            monkeypatch,
            jobs=[job],
            scores_by_title={"Some Job": _job_score(score=90, recommendation=recommendation)},
            notifier=notifier,
        )

        assert response.status_code == 200
        assert notifier.calls == []

    def test_does_not_notify_for_apply_below_threshold(self, client, monkeypatch):
        stricter_settings = Settings(
            api_key=API_KEY,
            rate_limit_requests=1000,
            rate_limit_window_seconds=60,
            xing_mailbox_username="user@example.com",
            xing_mailbox_app_password="app-password",
            min_job_score_to_notify=95,
        )
        monkeypatch.setattr("app.api.routes.get_settings", lambda: stricter_settings)

        job = _sample_job(title="Borderline Job")
        notifier = FakeTelegramNotifier()

        response = self._run(
            client,
            monkeypatch,
            jobs=[job],
            scores_by_title={"Borderline Job": _job_score(score=85, recommendation="APPLY")},
            notifier=notifier,
        )

        assert response.status_code == 200
        assert notifier.calls == []

    def test_multiple_apply_jobs_all_notified_with_pause_between_sends(self, client, monkeypatch):
        jobs = [
            _sample_job(title=f"Job {i}", url=f"https://www.xing.com/m/notify-{i}")
            for i in range(3)
        ]
        scores = {f"Job {i}": _job_score(score=90, recommendation="APPLY") for i in range(3)}
        notifier = FakeTelegramNotifier()

        sleep_calls: list[float] = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr("app.api.routes.asyncio.sleep", fake_sleep)

        response = self._run(
            client, monkeypatch, jobs=jobs, scores_by_title=scores, notifier=notifier
        )

        assert response.status_code == 200
        assert len(notifier.calls) == 3
        # 3 notifications -> 2 pauses between consecutive sends, none before
        # the first and none after the last.
        assert sleep_calls == [1, 1]

    def test_single_apply_job_does_not_sleep(self, client, monkeypatch):
        job = _sample_job(title="Only Job")
        notifier = FakeTelegramNotifier()

        sleep_calls: list[float] = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr("app.api.routes.asyncio.sleep", fake_sleep)

        response = self._run(
            client,
            monkeypatch,
            jobs=[job],
            scores_by_title={"Only Job": _job_score(score=90, recommendation="APPLY")},
            notifier=notifier,
        )

        assert response.status_code == 200
        assert len(notifier.calls) == 1
        assert sleep_calls == []

    def test_notification_failure_does_not_affect_counts_or_other_jobs(self, client, monkeypatch):
        jobs = [
            _sample_job(title=f"Job {i}", url=f"https://www.xing.com/m/fail-{i}") for i in range(3)
        ]
        scores = {f"Job {i}": _job_score(score=90, recommendation="APPLY") for i in range(3)}
        # Job 0 fails cleanly (False), Job 1 raises, Job 2 succeeds — none of
        # this should influence created/updated/failed counts below, nor the
        # message acknowledgment (all three persisted successfully).
        notifier = FakeTelegramNotifier(results=[False, RuntimeError("boom"), True])

        response = self._run(
            client, monkeypatch, jobs=jobs, scores_by_title=scores, notifier=notifier
        )

        assert response.status_code == 200
        assert response.json() == {
            "fetched": 3,
            "created": 3,
            "updated": 0,
            "skipped_invalid": 0,
            "failed": 0,
        }
        assert len(notifier.calls) == 3
