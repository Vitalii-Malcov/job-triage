import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.bundesagentur import BundesagenturAPIError
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
    tests/test_job_scorer.py. Keying results by title decouples the
    notification tests from the extraction/confidence pipeline that runs
    ahead of scoring in _run_bundesagentur.
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
        details: dict[str, str | None] | None = None,
    ) -> None:
        self._jobs = jobs or []
        self._error = error
        self._details = details or {}
        self.detail_calls: list[str] = []
        self.skipped_invalid_count = 0

    async def fetch(self, since=None) -> list[Job]:
        if self._error is not None:
            raise self._error
        return self._jobs

    async def fetch_detail(self, referenznummer: str) -> str | None:
        self.detail_calls.append(referenznummer)
        return self._details.get(referenznummer)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_collector_endpoint.db"
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
        bundesagentur_api_key="upstream-key",
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._collector_requests.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._collector_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _sample_job(**overrides) -> Job:
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


class TestRunBundesagenturCollector:
    def test_requires_api_key_auth(self, client):
        response = client.post("/api/v1/collectors/bundesagentur/run")
        assert response.status_code == 401

    def test_missing_upstream_api_key_returns_503(self, client, monkeypatch):
        unconfigured = Settings(api_key=API_KEY, rate_limit_requests=1000, bundesagentur_api_key="")
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)

        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

        assert response.status_code == 503

    def test_missing_upstream_api_key_returns_503_for_whitespace_only_key(
        self, client, monkeypatch
    ):
        unconfigured = Settings(
            api_key=API_KEY, rate_limit_requests=1000, bundesagentur_api_key="   "
        )
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)

        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

        assert response.status_code == 503

    def test_successful_run_reports_created_count(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: FakeCollector(jobs=[_sample_job()]),
        )

        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

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
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: FakeCollector(jobs=[_sample_job()]),
        )

        first = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())
        second = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

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

    def test_upstream_failure_returns_502(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: FakeCollector(error=BundesagenturAPIError("boom")),
        )

        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

        assert response.status_code == 502

    def test_collector_rate_limit_is_stricter_than_general_limit(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: FakeCollector(jobs=[_sample_job()]),
        )
        monkeypatch.setattr("app.security.rate_limit.COLLECTOR_RATE_LIMIT_REQUESTS", 1)
        rate_limit_module._collector_requests.clear()

        first = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())
        second = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

        assert first.status_code == 200
        assert second.status_code == 429

    def test_partial_failure_reports_failed_count_and_does_not_lose_already_committed_jobs(
        self, client, monkeypatch
    ):
        jobs = [
            _sample_job(
                title="Job One",
                url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1",
            ),
            _sample_job(
                title="Job Two",
                url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-2",
            ),
            _sample_job(
                title="Job Three",
                url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-3",
            ),
        ]
        monkeypatch.setattr(
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: FakeCollector(jobs=jobs),
        )

        # Fail only the 2nd persistence call via the real upsert_job (not a
        # full mock of _score_and_persist), so jobs 1 and 3 go through the
        # actual DB session. This proves the session recovers via
        # db.rollback() after job 2's failure rather than being left in an
        # unusable state that would also break job 3.
        import app.api.routes as routes_module

        original_upsert_job = routes_module.upsert_job
        call_count = {"n": 0}

        def flaky_upsert_job(db, job, score):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated persistence failure")
            return original_upsert_job(db, job, score)

        monkeypatch.setattr("app.api.routes.upsert_job", flaky_upsert_job)

        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

        assert response.status_code == 200
        assert response.json() == {
            "fetched": 3,
            "created": 2,
            "updated": 0,
            "skipped_invalid": 0,
            "failed": 1,
        }

        list_response = client.get("/api/v1/jobs", headers=_auth_headers())
        titles = {item["title"] for item in list_response.json()}
        assert titles == {"Job One", "Job Three"}

    def test_enrichment_extraction_and_confidence_gate_run_before_scoring(
        self, client, monkeypatch
    ):
        rich_ref = "10000-rich-S"
        missing_ref = "10000-missing-S"
        rich_description = (
            "Erfahrung mit Python, FastAPI, Flask, MySQL, MongoDB, Git und pytest. " * 35
        )
        jobs = [
            _sample_job(
                title="Backend Developer",
                url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-rich-S",
                source_reference=rich_ref,
                skills=[],
            ),
            _sample_job(
                title="Python Developer",
                url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-missing-S",
                source_reference=missing_ref,
                skills=[],
            ),
        ]
        fake_collector = FakeCollector(
            jobs=jobs,
            details={rich_ref: rich_description, missing_ref: None},
        )
        monkeypatch.setattr(
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: fake_collector,
        )

        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

        assert response.status_code == 200
        assert response.json()["created"] == 2
        listed = client.get("/api/v1/jobs", headers=_auth_headers()).json()
        by_title = {item["title"]: item for item in listed}
        assert by_title["Backend Developer"]["recommendation"] == "APPLY"
        assert by_title["Python Developer"]["recommendation"] == "NEEDS_ENRICHMENT"

        rich_detail = client.get(
            f"/api/v1/jobs/{by_title['Backend Developer']['id']}",
            headers=_auth_headers(),
        ).json()
        assert rich_detail["description"] == rich_description
        assert rich_detail["skills"] == [
            "fastapi",
            "flask",
            "git",
            "mongodb",
            "mysql",
            "pytest",
            "python",
        ]

    def test_second_run_reuses_persisted_detail_instead_of_refetching(self, client, monkeypatch):
        referenznummer = "10000-cached-S"
        description = "Python Flask PostgreSQL Git Linux. " * 70
        fake_collector = FakeCollector(
            jobs=[
                _sample_job(
                    url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-cached-S",
                    source_reference=referenznummer,
                    skills=[],
                )
            ],
            details={referenznummer: description},
        )
        monkeypatch.setattr(
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: fake_collector,
        )

        first = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())
        second = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

        assert first.status_code == 200
        assert second.status_code == 200
        assert fake_collector.detail_calls == [referenznummer]


class TestBundesagenturCollectorNotifications:
    def _run(self, client, monkeypatch, jobs, scores_by_title, notifier):
        monkeypatch.setattr(
            "app.api.routes.BundesagenturCollector",
            lambda **kwargs: FakeCollector(jobs=jobs),
        )
        monkeypatch.setattr(
            "app.api.routes.JobScorer",
            lambda profile_skills: FakeJobScorer(scores_by_title),
        )
        monkeypatch.setattr("app.api.routes.TelegramNotifier", lambda **kwargs: notifier)
        return client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

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
            bundesagentur_api_key="upstream-key",
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
            _sample_job(
                title=f"Job {i}",
                url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-notify-{i}",
            )
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
            _sample_job(
                title=f"Job {i}",
                url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-fail-{i}",
            )
            for i in range(3)
        ]
        scores = {f"Job {i}": _job_score(score=90, recommendation="APPLY") for i in range(3)}
        # Job 0 fails cleanly (False), Job 1 raises, Job 2 succeeds — none of
        # this should influence created/updated/failed counts below.
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
