import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.bundesagentur import BundesagenturAPIError
from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.job import Job
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


class FakeCollector:
    def __init__(self, jobs: list[Job] | None = None, error: Exception | None = None) -> None:
        self._jobs = jobs or []
        self._error = error
        self.skipped_invalid_count = 0

    async def fetch(self, since=None) -> list[Job]:
        if self._error is not None:
            raise self._error
        return self._jobs


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
