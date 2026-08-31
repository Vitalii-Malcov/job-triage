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
from app.models.bewerbung import BewerbungEvidencePacket
from app.providers.bewerbung.base import (
    BewerbungProvider,
    BewerbungProviderError,
    BewerbungProviderNotConfiguredError,
)
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


class _FakeProvider(BewerbungProvider):
    """Test-only provider — deliberately configurable to return malformed
    or adversarial plan payloads, to prove the schema/claim-resolution
    boundary rejects them regardless of what the provider itself decided
    to do. Never authors final prose (that isn't even a legal return type
    anymore) — only ever a raw, untrusted mapping."""

    name = "fake"

    def __init__(self, plan: dict | None = None, error: Exception | None = None):
        self._plan = plan
        self._error = error

    async def generate_plan(self, evidence: BewerbungEvidencePacket) -> dict:
        if self._error is not None:
            raise self._error
        assert self._plan is not None
        return self._plan


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bewerbung_endpoints.db"
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
    for bucket in ("_requests", "_cv_draft_requests", "_match_requests", "_bewerbung_requests"):
        getattr(rate_limit_module, bucket).clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    for bucket in ("_requests", "_cv_draft_requests", "_match_requests", "_bewerbung_requests"):
        getattr(rate_limit_module, bucket).clear()


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
            "last_name": "Example",
            "skills": [{"name": "Python"}],
        },
    )
    assert r.status_code == 200, r.text


def _compute_match(test_client, job_id: int) -> int:
    r = test_client.post(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers(), json={})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _create_cv_draft(test_client, job_id: int, match_id: int) -> int:
    r = test_client.post(
        f"/api/v1/jobs/{job_id}/cv-draft", headers=_auth_headers(), json={"match_id": match_id}
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _setup_ready_cv_draft(test_client, session_factory, job_id: int) -> int:
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)
    return _create_cv_draft(test_client, job_id, match_id)


def _patch_default_provider(monkeypatch, provider: BewerbungProvider) -> None:
    monkeypatch.setattr("app.services.bewerbung.DeterministicBewerbungProvider", lambda: provider)


_VALID_PLAN = {
    "opening_style": "ROLE_INTEREST",
    "paragraphs": [{"kind": "GENERIC", "claim_ids": []}],
    "closing_style": "INTERVIEW_INTEREST",
}


# --- auth --------------------------------------------------------------


def test_post_bewerbung_draft_requires_auth(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(f"/api/v1/jobs/{job_id}/bewerbung-draft", json={"cv_draft_id": 1})
    assert response.status_code == 401


def test_get_bewerbung_draft_for_job_requires_auth(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.get(f"/api/v1/jobs/{job_id}/bewerbung-draft")
    assert response.status_code == 401


def test_get_bewerbung_draft_by_id_requires_auth(client):
    test_client, _ = client
    response = test_client.get("/api/v1/bewerbung-drafts/1")
    assert response.status_code == 401


# --- 404s ----------------------------------------------------------------


def test_post_bewerbung_draft_missing_job_returns_404(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/jobs/999999/bewerbung-draft", headers=_auth_headers(), json={"cv_draft_id": 1}
    )
    assert response.status_code == 404


def test_post_bewerbung_draft_missing_cv_draft_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": 999999},
    )
    assert response.status_code == 404


def test_get_bewerbung_draft_for_job_missing_job_returns_404(client):
    test_client, _ = client
    response = test_client.get("/api/v1/jobs/999999/bewerbung-draft", headers=_auth_headers())
    assert response.status_code == 404


def test_get_bewerbung_draft_for_job_none_yet_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.get(f"/api/v1/jobs/{job_id}/bewerbung-draft", headers=_auth_headers())
    assert response.status_code == 404


def test_get_bewerbung_draft_by_id_missing_returns_404(client):
    test_client, _ = client
    response = test_client.get("/api/v1/bewerbung-drafts/999999", headers=_auth_headers())
    assert response.status_code == 404


# --- missing cv_draft_id in body -------------------------------------------


def test_missing_cv_draft_id_returns_422(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft", headers=_auth_headers(), json={}
    )
    assert response.status_code == 422


# --- wrong cv_draft / stale profile / stale job -----------------------------


def test_wrong_cv_draft_for_job_returns_422(client):
    test_client, session_factory = client
    job1_id = _seed_job(session_factory, fingerprint="fp-1", url="https://x.com/1")
    job2_id = _seed_job(session_factory, fingerprint="fp-2", url="https://x.com/2")
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job1_id)

    response = test_client.post(
        f"/api/v1/jobs/{job2_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 422


def test_stale_profile_returns_409_and_creates_no_draft(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    r = test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={"expected_profile_version": version, "location_city": "Munich"},
    )
    assert r.status_code == 200

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "cv_draft_profile_version" in detail
    assert "current_profile_version" in detail

    get_response = test_client.get(
        f"/api/v1/jobs/{job_id}/bewerbung-draft", headers=_auth_headers()
    )
    assert get_response.status_code == 404


def test_stale_job_returns_409_and_creates_no_draft_and_no_leak(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    db = session_factory()
    record = db.get(JobRecord, job_id)
    record.description = "VeryUniqueDescriptionMarkerXyz123"
    db.commit()
    db.close()

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 409
    assert "VeryUniqueDescriptionMarkerXyz123" not in response.text

    get_response = test_client.get(
        f"/api/v1/jobs/{job_id}/bewerbung-draft", headers=_auth_headers()
    )
    assert get_response.status_code == 404


# --- POST success ------------------------------------------------------


def test_post_bewerbung_draft_valid_returns_200(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == job_id
    assert body["cv_draft_id"] == cv_draft_id
    assert body["status"] == "DRAFT"
    assert body["language"] == "de"
    assert body["provider"] == "deterministic"
    assert body["salutation"] == "Sehr geehrte Damen und Herren,"
    assert body["subject"] == "Bewerbung als Python Developer"
    # Traceability: the plan that produced this draft is persisted, small,
    # and free of any provider-authored prose.
    assert set(body["plan"].keys()) == {"opening_style", "paragraphs", "closing_style"}


def test_get_latest_after_post_returns_same_draft(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    post_response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    get_response = test_client.get(
        f"/api/v1/jobs/{job_id}/bewerbung-draft", headers=_auth_headers()
    )
    assert get_response.status_code == 200
    assert get_response.json()["id"] == post_response.json()["id"]


def test_get_by_id_returns_exact_snapshot(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    post_response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    draft_id = post_response.json()["id"]
    get_response = test_client.get(f"/api/v1/bewerbung-drafts/{draft_id}", headers=_auth_headers())
    assert get_response.status_code == 200
    assert get_response.json()["id"] == draft_id


def test_two_posts_create_two_distinct_immutable_drafts(client):
    """spec section 35/49: unlike match/cv-draft, every successful POST
    creates a NEW row, even with identical pinned inputs."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    first = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    second = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"]


def test_get_bewerbung_draft_never_generates(client, monkeypatch):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)

    def _boom(*args, **kwargs):
        raise AssertionError("GET must never generate a Bewerbung draft")

    monkeypatch.setattr("app.services.bewerbung.BewerbungService.generate", _boom)
    response = test_client.get(f"/api/v1/jobs/{job_id}/bewerbung-draft", headers=_auth_headers())
    assert response.status_code == 404


# --- provider failure / not-configured --------------------------------------


def test_provider_not_configured_returns_503(client, monkeypatch):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    _patch_default_provider(
        monkeypatch, _FakeProvider(error=BewerbungProviderNotConfiguredError("not configured"))
    )
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 503


def test_provider_failure_returns_502_and_creates_no_draft(client, monkeypatch):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    _patch_default_provider(monkeypatch, _FakeProvider(error=BewerbungProviderError("boom")))
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 502

    get_response = test_client.get(
        f"/api/v1/jobs/{job_id}/bewerbung-draft", headers=_auth_headers()
    )
    assert get_response.status_code == 404


# --- malicious provider matrix (blocker-fix regression) ---------------------


def _count_bewerbung_rows(session_factory) -> int:
    from sqlalchemy import func, select

    from app.db.models import BewerbungDraftRecord

    db = session_factory()
    try:
        return db.scalar(select(func.count()).select_from(BewerbungDraftRecord))
    finally:
        db.close()


def test_provider_free_text_field_is_rejected_schema_extra_forbid(client, monkeypatch):
    """spec test 29: a provider trying to smuggle arbitrary prose via an
    unexpected field must fail schema validation — extra="forbid"."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    bad_plan = dict(_VALID_PLAN, free_text="Ich verfüge über AWS-Erfahrung.")
    _patch_default_provider(monkeypatch, _FakeProvider(plan=bad_plan))

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["codes"] == ["SCHEMA_INVALID"]
    assert "AWS" not in response.text
    assert _count_bewerbung_rows(session_factory) == 0


def test_valid_claim_id_plus_fabricated_claim_id_is_rejected_wholesale(client, monkeypatch):
    """spec test 30: a legitimate Python id alongside a fabricated
    AWS-like id must reject the entire plan — no partial rendering."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    match_response = test_client.get(f"/api/v1/jobs/{job_id}/match", headers=_auth_headers())
    assert match_response.status_code == 200

    bad_plan = {
        "opening_style": "ROLE_INTEREST",
        "paragraphs": [
            {
                "kind": "EVIDENCE",
                "claim_ids": ["candidate_skill:1", "candidate_skill:fabricated-aws"],
            }
        ],
        "closing_style": "INTERVIEW_INTEREST",
    }
    _patch_default_provider(monkeypatch, _FakeProvider(plan=bad_plan))

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["codes"][0].startswith("UNKNOWN_CLAIM_ID")
    assert "AWS" not in response.text
    assert _count_bewerbung_rows(session_factory) == 0


def test_unresolvable_plan_creates_zero_rows(client, monkeypatch):
    """spec section 44/54: rejected generation must never persist a
    partial row."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    _patch_default_provider(
        monkeypatch,
        _FakeProvider(
            plan={
                "opening_style": "ROLE_INTEREST",
                "paragraphs": [{"kind": "EVIDENCE", "claim_ids": ["does-not-exist:1"]}],
                "closing_style": "INTERVIEW_INTEREST",
            }
        ),
    )
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 422
    assert _count_bewerbung_rows(session_factory) == 0


# --- human approval boundary / no side effects (spec section 51/59) --------


def test_generation_never_mutates_application_status(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id = _setup_ready_cv_draft(test_client, session_factory, job_id)

    before = test_client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers())
    assert before.status_code == 200
    status_before = before.json()["status"]

    response = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert response.status_code == 200

    after = test_client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers())
    assert after.json()["status"] == status_before
