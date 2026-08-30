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
    db_path = tmp_path / "test_candidate_cv_draft_endpoints.db"
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
    rate_limit_module._cv_draft_requests.clear()
    rate_limit_module._match_requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._cv_draft_requests.clear()
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
        "description": "Python required.",
        "skills_json": json.dumps(["python"]),
        "data_confidence": 0.9,
        "must_have_skills_json": json.dumps(["python"]),
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


def _set_python_skill(test_client) -> None:
    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    r = test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={
            "expected_profile_version": version,
            "first_name": "Anna",
            "skills": [{"name": "Python"}],
        },
    )
    assert r.status_code == 200, r.text


def _compute_match(test_client, job_id: int) -> int:
    r = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers(), json={})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# --- auth --------------------------------------------------------------


def test_post_cv_draft_requires_auth(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(f"/api/v1/jobs/{job_id}/cv-draft", json={"match_id": 1})
    assert response.status_code == 401


def test_get_cv_draft_for_job_requires_auth(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.get(f"/api/v1/jobs/{job_id}/cv-draft")
    assert response.status_code == 401


def test_get_cv_draft_by_id_requires_auth(client):
    test_client, _ = client
    response = test_client.get("/api/v1/cv-drafts/1")
    assert response.status_code == 401


# --- 404s ----------------------------------------------------------------


def test_post_cv_draft_missing_job_returns_404(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/jobs/999999/cv-draft", headers=_auth_headers(), json={"match_id": 1}
    )
    assert response.status_code == 404


def test_post_cv_draft_missing_match_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": 999999}
    )
    assert response.status_code == 404


def test_get_cv_draft_for_job_missing_job_returns_404(client):
    test_client, _ = client
    response = test_client.get("/api/v1/jobs/999999/cv-draft", headers=_auth_headers())
    assert response.status_code == 404


def test_get_cv_draft_for_job_no_draft_yet_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.get(f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers())
    assert response.status_code == 404


def test_get_cv_draft_by_id_missing_returns_404(client):
    test_client, _ = client
    response = test_client.get("/api/v1/cv-drafts/999999", headers=_auth_headers())
    assert response.status_code == 404


# --- POST success / caching -------------------------------------------------


def test_post_cv_draft_valid_returns_200(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["match_id"] == match_id
    assert body["cv_adapter_version"] == "v1"
    assert body["status"] == "DRAFT"


def test_get_latest_after_post_returns_same_draft(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)

    post_response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    get_response = test_client.get(f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers())
    assert get_response.status_code == 200
    assert get_response.json()["id"] == post_response.json()["id"]


def test_get_by_id_returns_exact_snapshot(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)

    post_response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    draft_id = post_response.json()["id"]
    get_response = test_client.get(f"/api/v1/cv-drafts/{draft_id}", headers=_auth_headers())
    assert get_response.status_code == 200
    assert get_response.json()["id"] == draft_id


def test_top_level_provenance_round_trips_through_post_and_get_by_id(client):
    """M-01 section 14: title/summary/name provenance must be identical
    after a real POST -> DB persist -> GET by draft_id round trip through
    the actual HTTP API, not just at the repository layer."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)

    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    r = test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={
            "expected_profile_version": version,
            "first_name": "Anna",
            "location_city": "Example City",
            "professional_title": "Junior Python Developer",
            "professional_summary": "Backend-focused developer.",
            "skills": [{"name": "Python"}],
        },
    )
    assert r.status_code == 200, r.text
    profile_id = r.json()["id"]
    profile_version = r.json()["profile_version"]

    match_id = _compute_match(test_client, job_id)
    post_response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert post_response.status_code == 200, post_response.text
    posted = post_response.json()
    draft_id = posted["id"]

    get_response = test_client.get(f"/api/v1/cv-drafts/{draft_id}", headers=_auth_headers())
    assert get_response.status_code == 200
    fetched = get_response.json()

    for body in (posted, fetched):
        title = body["header"]["professional_title"]
        assert title["value"] == "Junior Python Developer"
        assert title["source_entity"] == "candidate_profile"
        assert title["source_id"] == profile_id
        assert title["source_field"] == "professional_title"
        assert title["profile_version"] == profile_version

        summary = body["professional_summary"]
        assert summary["value"] == "Backend-focused developer."
        assert summary["source_field"] == "professional_summary"
        assert summary["profile_version"] == profile_version

        name = body["header"]["first_name"]
        assert name["value"] == "Anna"
        assert name["source_field"] == "first_name"

    assert posted["header"]["professional_title"] == fetched["header"]["professional_title"]
    assert posted["professional_summary"] == fetched["professional_summary"]


def test_untrusted_top_level_fact_round_trips_as_null(client):
    """M-01 section 15: a top-level fact with untrusted provenance
    (INFERRED+CONFIRMED) must render as null with no provenance object,
    both immediately after POST and after a GET-by-id round trip."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)

    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    r = test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={
            "expected_profile_version": version,
            "professional_title": "Senior Architect",
            "field_trust": {
                "professional_title": {"source": "INFERRED", "confidence": "CONFIRMED"}
            },
            "skills": [{"name": "Python"}],
        },
    )
    assert r.status_code == 200, r.text

    match_id = _compute_match(test_client, job_id)
    post_response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert post_response.status_code == 200, post_response.text
    posted = post_response.json()
    assert posted["header"]["professional_title"] is None
    assert "Senior Architect" not in post_response.text

    draft_id = posted["id"]
    get_response = test_client.get(f"/api/v1/cv-drafts/{draft_id}", headers=_auth_headers())
    assert get_response.json()["header"]["professional_title"] is None
    assert "Senior Architect" not in get_response.text


def test_post_reuses_cache_without_force_recompute(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)

    first = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    second = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert first.json()["id"] == second.json()["id"]


def test_get_cv_draft_never_computes(client, monkeypatch):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)

    def _boom(*args, **kwargs):
        raise AssertionError("GET must never compute a CV draft")

    monkeypatch.setattr("app.api.routes.compute_cv_draft", _boom)
    response = test_client.get(f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers())
    assert response.status_code == 404


# --- wrong match / stale profile / stale job -------------------------------


def test_wrong_match_for_job_returns_422(client):
    test_client, session_factory = client
    job1_id = _seed_job(session_factory, fingerprint="fp-1", url="https://x.com/1")
    job2_id = _seed_job(session_factory, fingerprint="fp-2", url="https://x.com/2")
    _set_python_skill(test_client)
    match1_id = _compute_match(test_client, job1_id)

    response = test_client.post(
        f"/api/v1/jobs/{job2_id}/cv-draft", headers=_auth_headers(), json={"match_id": match1_id}
    )
    assert response.status_code == 422


def test_stale_profile_returns_409_and_creates_no_draft(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)

    # Bump the profile version again after the match was computed.
    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    r = test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={"expected_profile_version": version, "last_name": "Example"},
    )
    assert r.status_code == 200

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "match_profile_version" in detail
    assert "current_profile_version" in detail
    assert detail["match_profile_version"] != detail["current_profile_version"]

    get_response = test_client.get(f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers())
    assert get_response.status_code == 404


def test_stale_job_returns_409_and_creates_no_draft(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)

    db = session_factory()
    record = db.get(JobRecord, job_id)
    record.description = "Completely different vacancy text now."
    db.commit()
    db.close()

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert response.status_code == 409
    assert "job" in response.json()["detail"].lower()

    get_response = test_client.get(f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers())
    assert get_response.status_code == 404


def test_stale_job_error_does_not_leak_job_content(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)

    db = session_factory()
    record = db.get(JobRecord, job_id)
    record.description = "VeryUniqueDescriptionMarkerXyz123"
    db.commit()
    db.close()

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert "VeryUniqueDescriptionMarkerXyz123" not in response.text


# --- missing match_id in body ----------------------------------------------


def test_missing_match_id_returns_422(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={})
    assert response.status_code == 422


# --- network safety --------------------------------------------------------


def test_module_never_imports_an_http_client():
    import ast
    from pathlib import Path

    for module_name in ("cv_adapter",):
        path = Path("app/agents") / f"{module_name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module.split(".")[0])
        assert "httpx" not in imported_names
        assert "requests" not in imported_names
        assert "socket" not in imported_names
        assert "urllib" not in imported_names
