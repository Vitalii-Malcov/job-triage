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
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_candidate_job_match_endpoints.db"
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
    rate_limit_module._match_requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._match_requests.clear()


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
        "description": "Python and Docker required.",
        "skills_json": json.dumps(["python", "docker"]),
        "data_confidence": 0.9,
        "must_have_skills_json": json.dumps(["python", "docker"]),
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


# --- auth --------------------------------------------------------------


def test_post_match_requires_auth(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(f"/api/v1/jobs/{job_id}/match")
    assert response.status_code == 401


def test_get_match_requires_auth(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.get(f"/api/v1/jobs/{job_id}/match")
    assert response.status_code == 401


# --- 404s ----------------------------------------------------------------


def test_post_match_job_missing_returns_404(client):
    test_client, _ = client
    response = test_client.post("/api/v1/jobs/999999/match", headers=_auth_headers())
    assert response.status_code == 404


def test_get_match_job_missing_returns_404(client):
    test_client, _ = client
    response = test_client.get("/api/v1/jobs/999999/match", headers=_auth_headers())
    assert response.status_code == 404


def test_get_match_no_analysis_yet_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.get(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    assert response.status_code == 404


# --- POST success / caching -------------------------------------------------


def test_post_match_valid_job_returns_200(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["algorithm_version"] == "v1"
    assert 0 <= body["overall_score"] <= 100


def test_get_match_after_post_returns_cached_result(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    post_response = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    get_response = test_client.get(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())

    assert get_response.status_code == 200
    assert get_response.json()["id"] == post_response.json()["id"]


def test_post_match_reuses_cache_without_force_recompute(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    first = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    second = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())

    assert first.json()["id"] == second.json()["id"]
    assert first.json()["created_at"] == second.json()["created_at"]


def test_post_match_force_recompute_with_unchanged_inputs_reuses_same_row(client):
    """force_recompute bypasses the cache-lookup shortcut and recomputes,
    but matching is deterministic — with nothing about the job or profile
    changed, the recomputed result collides with the same cache identity
    and the DB UNIQUE constraint's dedup safety net reloads the existing
    row rather than inserting a byte-identical duplicate (section 34).
    """
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    first = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    second = test_client.post(
        f"/api/v1/jobs/{job_id}/match", headers=_auth_headers(), json={"force_recompute": True}
    )

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_job_content_change_produces_a_new_match_row(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    first = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())

    db = session_factory()
    record = db.get(JobRecord, job_id)
    record.description = "Completely different vacancy text now."
    db.commit()
    db.close()

    second = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]


def test_empty_candidate_profile_does_not_fail_match(client):
    """Section 33: a sparse/empty profile must still return 200 with
    low/neutral coverage and warnings, never a failure."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert any("Missing required skill" in w for w in body["warnings"])
    assert any("no confirmed facts" in w for w in body["warnings"])


def test_get_match_never_computes(client, monkeypatch):
    """GET must be a pure cache read (section 23) — it must not call the
    matching algorithm even when no cached row exists yet for this
    profile version (it should 404 instead, per the two 404 tests above).
    This test additionally asserts compute_match is never invoked in the
    GET path by monkeypatching it to raise if called.
    """
    test_client, session_factory = client
    job_id = _seed_job(session_factory)

    def _boom(*args, **kwargs):
        raise AssertionError("GET must never compute a match")

    monkeypatch.setattr("app.api.routes.compute_match", _boom)
    response = test_client.get(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    assert response.status_code == 404


def test_response_does_not_expose_internal_orm_fields(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    body = response.json()

    assert "job_snapshot_fingerprint" not in body
    assert "analysis_json" not in body
