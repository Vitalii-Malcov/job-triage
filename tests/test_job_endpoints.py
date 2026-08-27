import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord
from app.db.session import get_db
from app.main import app
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_job_endpoints.db"
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
    rate_limit_module._requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _seed_job(session_factory, **overrides) -> int:
    db = session_factory()
    now = overrides.pop("last_seen_at", datetime.now(UTC))
    data = {
        "fingerprint": overrides.pop("fingerprint", "fp-default"),
        "source": "xing",
        "title": "Python Developer",
        "company": "Example GmbH",
        "location": "Berlin",
        "url": "https://example.com/jobs/1",
        "description": "We build APIs.",
        "skills_json": json.dumps(["python", "fastapi"]),
        "data_confidence": 0.9,
        "skill_source": "description_extracted",
        "must_have_skills_json": json.dumps(["python"]),
        "nice_to_have_skills_json": json.dumps(["fastapi"]),
        "score": 80,
        "recommendation": "APPLY",
        "status": overrides.pop("status", "NEW"),
        "first_seen_at": overrides.pop("first_seen_at", now),
        "last_seen_at": now,
    }
    data.update(overrides)
    record = JobRecord(**data)
    db.add(record)
    db.commit()
    db.refresh(record)
    job_id = record.id
    db.close()
    return job_id


class TestListJobs:
    def test_list_jobs_returns_seeded_jobs(self, client):
        c, session_factory = client
        _seed_job(session_factory, fingerprint="fp-1")

        response = c.get("/api/v1/jobs", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["title"] == "Python Developer"
        assert body[0]["status"] == "NEW"

    def test_list_jobs_filters_by_status(self, client):
        c, session_factory = client
        _seed_job(session_factory, fingerprint="fp-new", status="NEW")
        _seed_job(session_factory, fingerprint="fp-applied", status="APPLIED")

        response = c.get("/api/v1/jobs", params={"status": "APPLIED"}, headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["status"] == "APPLIED"

    def test_list_jobs_pagination(self, client):
        c, session_factory = client
        base_time = datetime.now(UTC)
        job_ids = [
            _seed_job(
                session_factory,
                fingerprint=f"fp-{i}",
                last_seen_at=base_time + timedelta(seconds=i),
            )
            for i in range(5)
        ]

        response = c.get("/api/v1/jobs", params={"limit": 2, "offset": 1}, headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        # Newest (highest offset in job_ids) sorts first by last_seen_at desc;
        # offset=1 skips it, limit=2 returns the next two newest.
        assert [item["id"] for item in body] == [job_ids[3], job_ids[2]]

    def test_list_jobs_requires_api_key(self, client):
        c, _ = client
        response = c.get("/api/v1/jobs")
        assert response.status_code == 401

    def test_list_jobs_rate_limit_enforced(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(api_key=API_KEY, rate_limit_requests=1, rate_limit_window_seconds=60),
        )
        rate_limit_module._requests.clear()

        first = c.get("/api/v1/jobs", headers=_auth_headers())
        second = c.get("/api/v1/jobs", headers=_auth_headers())

        assert first.status_code == 200
        assert second.status_code == 429


class TestGetJob:
    def test_get_job_returns_full_detail(self, client):
        c, session_factory = client
        job_id = _seed_job(session_factory, fingerprint="fp-detail")

        response = c.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == job_id
        assert body["fingerprint"] == "fp-detail"
        assert body["skills"] == ["python", "fastapi"]
        assert body["data_confidence"] == 0.9
        assert body["skill_source"] == "description_extracted"
        assert body["must_have_skills"] == ["python"]
        assert body["nice_to_have_skills"] == ["fastapi"]

    def test_get_job_404_for_missing_id(self, client):
        c, _ = client
        response = c.get("/api/v1/jobs/999999", headers=_auth_headers())
        assert response.status_code == 404


class TestPatchJobStatus:
    def test_patch_status_success(self, client):
        c, session_factory = client
        job_id = _seed_job(session_factory, fingerprint="fp-patch", status="NEW")

        response = c.patch(
            f"/api/v1/jobs/{job_id}/status",
            json={"status": "APPLIED"},
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "APPLIED"

    def test_patch_status_invalid_transition_is_conflict(self, client):
        c, session_factory = client
        job_id = _seed_job(session_factory, fingerprint="fp-conflict", status="NEW")

        response = c.patch(
            f"/api/v1/jobs/{job_id}/status",
            json={"status": "INTERVIEW"},
            headers=_auth_headers(),
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "NEW" in detail
        assert "INTERVIEW" in detail

    def test_patch_status_404_for_missing_job(self, client):
        c, _ = client
        response = c.patch(
            "/api/v1/jobs/999999/status",
            json={"status": "APPLIED"},
            headers=_auth_headers(),
        )
        assert response.status_code == 404

    def test_patch_status_requires_api_key(self, client):
        c, session_factory = client
        job_id = _seed_job(session_factory, fingerprint="fp-noauth")

        response = c.patch(f"/api/v1/jobs/{job_id}/status", json={"status": "APPLIED"})

        assert response.status_code == 401

    def test_patch_status_rejects_unknown_status_value(self, client):
        c, session_factory = client
        job_id = _seed_job(session_factory, fingerprint="fp-badvalue")

        response = c.patch(
            f"/api/v1/jobs/{job_id}/status",
            json={"status": "NOT_A_REAL_STATUS"},
            headers=_auth_headers(),
        )

        assert response.status_code == 422
