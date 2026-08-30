import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord
from app.db.session import get_db
from app.main import app
from app.providers.base import ProviderNotConfiguredError
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_company_research_endpoints.db"
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
        api_key=API_KEY, rate_limit_requests=1000, rate_limit_window_seconds=60
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._company_research_requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._company_research_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _seed_job(session_factory, **overrides) -> int:
    db = session_factory()
    now = datetime.now(UTC)
    data = {
        "fingerprint": overrides.pop("fingerprint", "fp-default"),
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "Example GmbH",
        "location": "Berlin",
        "url": "https://careers.example.com/jobs/1",
        "description": "We use Python and Docker.",
        "skills_json": json.dumps(["python", "docker"]),
        "data_confidence": 0.9,
        "must_have_skills_json": "[]",
        "nice_to_have_skills_json": "[]",
        "score": 80,
        "recommendation": "APPLY",
        "status": "NEW",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    data.update(overrides)
    record = JobRecord(**data)
    db.add(record)
    db.commit()
    job_id = record.id
    db.close()
    return job_id


class _FakeService:
    """Stand-in for CompanyResearchService, installed via
    `monkeypatch.setattr("app.api.routes.CompanyResearchService", ...)` so a
    test can control exactly what get_or_run does without touching the real
    provider/DB logic already covered by tests/test_company_research_service.py.
    """

    def __init__(self, *, error: Exception | None = None):
        self._error = error

    async def get_or_run(self, db, job, settings, *, force_refresh=False):
        raise self._error


class TestPostResearch:
    def test_requires_api_key(self, client):
        client_, _ = client
        response = client_.post("/api/v1/jobs/1/research")
        assert response.status_code == 401

    def test_404_for_unknown_job(self, client):
        client_, _ = client
        response = client_.post("/api/v1/jobs/999/research", headers=_auth_headers())
        assert response.status_code == 404

    def test_success_reports_refresh_attempted_and_succeeded(self, client):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["refresh_attempted"] is True
        assert body["refresh_succeeded"] is True
        assert body["served_stale"] is False
        assert body["error"] is None
        assert body["research"]["company_name"] == "Example GmbH"
        assert body["research"]["research_status"] == "PARTIAL"
        assert body["research"]["provider_name"] == "job_data"

    def test_second_call_reuses_cache_and_reports_no_refresh_attempted(self, client):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        first = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())
        second = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert first.json()["research"]["id"] == second.json()["research"]["id"]
        assert second.json()["refresh_attempted"] is False
        assert second.json()["refresh_succeeded"] is True

    def test_force_refresh_still_succeeds(self, client):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())
        response = client_.post(
            f"/api/v1/jobs/{job_id}/research",
            headers=_auth_headers(),
            json={"force_refresh": True},
        )

        assert response.status_code == 200
        assert response.json()["refresh_attempted"] is True

    def test_blank_company_name_returns_422(self, client):
        client_, session_factory = client
        job_id = _seed_job(session_factory, company="   ")

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 422

    def test_ambiguous_identity_returns_409(self, client):
        """FR-M-01: two distinct known-domain companies already on file
        share this job's normalized company name — POST must fail closed
        with 409, never silently research (or return) an arbitrary one of
        them.
        """
        client_, session_factory = client
        job_id = _seed_job(session_factory, company="Acme GmbH")

        from app.db.repositories import upsert_company_research
        from app.models.company_research import CompanyResearchData, Evidence

        def _data(**overrides):
            fields = {
                "company_name": "Acme GmbH",
                "provider_name": "job_data",
                "research_status": "PARTIAL",
                "evidence": [Evidence(type="FACT", claim="test", source_url="https://example.com")],
            }
            fields.update(overrides)
            return CompanyResearchData(**fields)

        db = session_factory()
        upsert_company_research(
            db, _data(), normalized_domain="acme.de", normalized_company_name="acme gmbh"
        )
        upsert_company_research(
            db, _data(), normalized_domain="acme.com", normalized_company_name="acme gmbh"
        )
        db.close()

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 409

    def test_sole_known_domain_fresh_cache_does_not_call_provider(self, client, monkeypatch):
        """FR-M-03 Test D: a job whose company resolves to a single already
        known-domain record (POST-visible symptom of the bug) must hit
        cache — 200, refresh_attempted=False, refresh_succeeded=True,
        refresh_superseded=False — not spuriously call the provider and
        report a false "superseded by a newer concurrent refresh".
        """
        client_, session_factory = client
        job_id = _seed_job(session_factory, company="Acme GmbH")

        from app.db.repositories import upsert_company_research
        from app.models.company_research import CompanyResearchData, Evidence

        def _data(**overrides):
            fields = {
                "company_name": "Acme GmbH",
                "provider_name": "job_data",
                "research_status": "PARTIAL",
                "evidence": [Evidence(type="FACT", claim="test", source_url="https://example.com")],
            }
            fields.update(overrides)
            return CompanyResearchData(**fields)

        db = session_factory()
        seeded, _ = upsert_company_research(
            db, _data(), normalized_domain="acme.de", normalized_company_name="acme gmbh"
        )
        seeded_id = seeded.id
        db.close()

        def _explode(*args, **kwargs):
            raise AssertionError("fresh cache hit must never call the provider")

        import app.providers.job_data_provider as provider_module

        monkeypatch.setattr(provider_module.JobDataCompanyResearchProvider, "research", _explode)

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["refresh_attempted"] is False
        assert body["refresh_succeeded"] is True
        assert body["refresh_superseded"] is False
        assert body["served_stale"] is False
        assert body["error"] is None
        assert body["research"]["id"] == seeded_id

    def test_provider_not_configured_returns_503(self, client, monkeypatch):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        monkeypatch.setattr(
            "app.api.routes.CompanyResearchService",
            lambda *a, **k: _FakeService(error=ProviderNotConfiguredError("needs an API key")),
        )

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 503

    def test_total_failure_with_no_usable_data_returns_502(self, client, monkeypatch):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        from app.models.company_research import CompanyResearchRunResponse

        class _FailingService:
            async def get_or_run(self, db, job, settings, *, force_refresh=False):
                return CompanyResearchRunResponse(
                    research=None,
                    refresh_attempted=True,
                    refresh_succeeded=False,
                    served_stale=False,
                    error="provider exploded",
                )

        monkeypatch.setattr(
            "app.api.routes.CompanyResearchService", lambda *a, **k: _FailingService()
        )

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 502
        assert "provider exploded" in response.json()["detail"]

    def test_repeated_total_failure_keeps_returning_502_not_200(self, client, monkeypatch):
        """RR-M-02: a second consecutive total-failure call must not
        silently become a 200 just because a diagnostic FAILED row now
        exists from the first failure — that row is not usable research
        content (see CompanyResearchService._is_usable_research).
        """
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        from app.models.company_research import CompanyResearchRunResponse

        class _AlwaysFailingService:
            async def get_or_run(self, db, job, settings, *, force_refresh=False):
                return CompanyResearchRunResponse(
                    research=None,
                    refresh_attempted=True,
                    refresh_succeeded=False,
                    refresh_superseded=False,
                    served_stale=False,
                    error="provider exploded",
                )

        monkeypatch.setattr(
            "app.api.routes.CompanyResearchService", lambda *a, **k: _AlwaysFailingService()
        )

        first = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())
        second = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert first.status_code == 502
        assert second.status_code == 502

    def test_superseded_refresh_returns_200_with_superseded_flag(self, client, monkeypatch):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        from app.models.company_research import CompanyResearchResponse, CompanyResearchRunResponse

        now = datetime.now(UTC)
        winning_research = CompanyResearchResponse(
            id=1,
            company_name="Example GmbH",
            provider_name="job_data",
            research_status="PARTIAL",
            confidence=0.4,
            researched_at=now,
            last_attempt_at=now,
            last_attempt_status="SUCCESS",
            last_error=None,
            created_at=now,
            updated_at=now,
        )

        class _SupersededService:
            async def get_or_run(self, db, job, settings, *, force_refresh=False):
                return CompanyResearchRunResponse(
                    research=winning_research,
                    refresh_attempted=True,
                    refresh_succeeded=False,
                    refresh_superseded=True,
                    served_stale=False,
                    error="Refresh result was superseded by a newer concurrent refresh.",
                )

        monkeypatch.setattr(
            "app.api.routes.CompanyResearchService", lambda *a, **k: _SupersededService()
        )

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["refresh_superseded"] is True
        assert body["refresh_succeeded"] is False
        assert body["served_stale"] is False
        assert body["research"]["company_name"] == "Example GmbH"

    def test_refresh_failed_with_prior_result_returns_200_served_stale(self, client, monkeypatch):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        from app.models.company_research import CompanyResearchResponse, CompanyResearchRunResponse

        now = datetime.now(UTC)
        stale_research = CompanyResearchResponse(
            id=1,
            company_name="Example GmbH",
            provider_name="job_data",
            research_status="PARTIAL",
            confidence=0.3,
            researched_at=now,
            last_attempt_at=now,
            last_attempt_status="FAILED",
            last_error="transient failure",
            created_at=now,
            updated_at=now,
        )

        class _StaleService:
            async def get_or_run(self, db, job, settings, *, force_refresh=False):
                return CompanyResearchRunResponse(
                    research=stale_research,
                    refresh_attempted=True,
                    refresh_succeeded=False,
                    served_stale=True,
                    error="transient failure",
                )

        monkeypatch.setattr(
            "app.api.routes.CompanyResearchService", lambda *a, **k: _StaleService()
        )

        response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["served_stale"] is True
        assert body["refresh_succeeded"] is False
        assert body["research"]["company_name"] == "Example GmbH"

    def test_rate_limit_enforced(self, client, monkeypatch):
        client_, session_factory = client
        job_id = _seed_job(session_factory)
        monkeypatch.setattr("app.security.rate_limit.COMPANY_RESEARCH_RATE_LIMIT_REQUESTS", 1)
        rate_limit_module._company_research_requests.clear()

        first = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())
        second = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert first.status_code == 200
        assert second.status_code == 429


class TestGetResearch:
    def test_requires_api_key(self, client):
        client_, _ = client
        response = client_.get("/api/v1/jobs/1/research")
        assert response.status_code == 401

    def test_404_for_unknown_job(self, client):
        client_, _ = client
        response = client_.get("/api/v1/jobs/999/research", headers=_auth_headers())
        assert response.status_code == 404

    def test_404_when_not_yet_researched(self, client):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        response = client_.get(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 404

    def test_ambiguous_identity_returns_409_not_an_arbitrary_company(self, client):
        """FR-M-01: GET is a pure cache read but must not silently pick one
        of two distinct known-domain companies sharing this job's
        normalized company name.
        """
        client_, session_factory = client
        job_id = _seed_job(session_factory, company="Acme GmbH")

        from app.db.repositories import upsert_company_research
        from app.models.company_research import CompanyResearchData, Evidence

        def _data(**overrides):
            fields = {
                "company_name": "Acme GmbH",
                "provider_name": "job_data",
                "research_status": "PARTIAL",
                "evidence": [Evidence(type="FACT", claim="test", source_url="https://example.com")],
            }
            fields.update(overrides)
            return CompanyResearchData(**fields)

        db = session_factory()
        upsert_company_research(
            db, _data(), normalized_domain="acme.de", normalized_company_name="acme gmbh"
        )
        upsert_company_research(
            db, _data(), normalized_domain="acme.com", normalized_company_name="acme gmbh"
        )
        db.close()

        response = client_.get(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 409

    def test_sole_known_domain_returns_200_not_404(self, client):
        """FR-M-03 Test C: GET must resolve a job's company to its sole
        already-known-domain research record — not 404, which the old
        exact "name:<x>"-only lookup would have produced.
        """
        client_, session_factory = client
        job_id = _seed_job(session_factory, company="Acme GmbH")

        from app.db.repositories import upsert_company_research
        from app.models.company_research import CompanyResearchData, Evidence

        def _data(**overrides):
            fields = {
                "company_name": "Acme GmbH",
                "provider_name": "job_data",
                "research_status": "PARTIAL",
                "evidence": [Evidence(type="FACT", claim="test", source_url="https://example.com")],
            }
            fields.update(overrides)
            return CompanyResearchData(**fields)

        db = session_factory()
        seeded, _ = upsert_company_research(
            db, _data(), normalized_domain="acme.de", normalized_company_name="acme gmbh"
        )
        seeded_id = seeded.id
        db.close()

        response = client_.get(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert response.status_code == 200
        assert response.json()["id"] == seeded_id

    def test_returns_cached_result_without_triggering_a_new_run(self, client, monkeypatch):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        post_response = client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())
        assert post_response.status_code == 200

        def _explode(*args, **kwargs):
            raise AssertionError("GET must never call get_or_run / trigger a provider run")

        import app.services.company_research as service_module

        monkeypatch.setattr(service_module.CompanyResearchService, "get_or_run", _explode)

        get_response = client_.get(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        assert get_response.status_code == 200
        assert get_response.json()["id"] == post_response.json()["research"]["id"]

    def test_shows_attempt_metadata(self, client):
        client_, session_factory = client
        job_id = _seed_job(session_factory)

        client_.post(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())
        response = client_.get(f"/api/v1/jobs/{job_id}/research", headers=_auth_headers())

        body = response.json()
        assert body["last_attempt_status"] == "SUCCESS"
        assert body["last_attempt_at"] is not None
        assert body["last_error"] is None
