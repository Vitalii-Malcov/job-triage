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
    db_path = tmp_path / "test_review_package_endpoints.db"
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
    buckets = (
        "_requests",
        "_cv_draft_requests",
        "_match_requests",
        "_bewerbung_requests",
        "_review_write_requests",
    )
    for bucket in buckets:
        getattr(rate_limit_module, bucket).clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    for bucket in buckets:
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


def _create_bewerbung_draft(test_client, job_id: int, cv_draft_id: int) -> int:
    r = test_client.post(
        f"/api/v1/jobs/{job_id}/bewerbung-draft",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ready_pair(test_client, session_factory, job_id: int) -> tuple[int, int]:
    _set_python_skill(test_client)
    match_id = _compute_match(test_client, job_id)
    cv_draft_id = _create_cv_draft(test_client, job_id, match_id)
    bewerbung_draft_id = _create_bewerbung_draft(test_client, job_id, cv_draft_id)
    return cv_draft_id, bewerbung_draft_id


def _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id):
    return test_client.post(
        f"/api/v1/jobs/{job_id}/review-package",
        headers=_auth_headers(),
        json={"cv_draft_id": cv_draft_id, "bewerbung_draft_id": bewerbung_draft_id},
    )


def _count_reviews(session_factory) -> int:
    from sqlalchemy import func, select

    from app.db.models import ApplicationPackageReviewRecord

    db = session_factory()
    try:
        return db.scalar(select(func.count()).select_from(ApplicationPackageReviewRecord))
    finally:
        db.close()


def _count_candidate_profiles(session_factory) -> int:
    from sqlalchemy import func, select

    from app.db.models import CandidateProfileRecord

    db = session_factory()
    try:
        return db.scalar(select(func.count()).select_from(CandidateProfileRecord))
    finally:
        db.close()


def _delete_candidate_profile(session_factory) -> None:
    from app.db.models import CandidateProfileRecord

    db = session_factory()
    db.query(CandidateProfileRecord).delete()
    db.commit()
    db.close()


def _delete_job(session_factory, job_id: int) -> None:
    db = session_factory()
    db.query(JobRecord).filter(JobRecord.id == job_id).delete()
    db.commit()
    db.close()


# --- auth ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path_suffix",
    [
        ("post", "/review-package"),
        ("get", "/review-package"),
    ],
)
def test_job_scoped_endpoints_require_auth(client, method, path_suffix):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    call = getattr(test_client, method)
    kwargs = {"json": {"cv_draft_id": 1, "bewerbung_draft_id": 1}} if method == "post" else {}
    response = call(f"/api/v1/jobs/{job_id}{path_suffix}", **kwargs)
    assert response.status_code == 401


def test_review_package_by_id_requires_auth(client):
    test_client, _ = client
    assert test_client.get("/api/v1/review-packages/1").status_code == 401
    assert (
        test_client.patch(
            "/api/v1/review-packages/1", json={"expected_review_version": 1}
        ).status_code
        == 401
    )
    assert (
        test_client.post(
            "/api/v1/review-packages/1/approve", json={"expected_review_version": 1}
        ).status_code
        == 401
    )
    assert (
        test_client.post(
            "/api/v1/review-packages/1/reject", json={"expected_review_version": 1}
        ).status_code
        == 401
    )


def test_approved_package_requires_auth(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    assert test_client.get(f"/api/v1/jobs/{job_id}/approved-package").status_code == 401


# --- create: 404s / 422s / 409 ----------------------------------------------


def test_create_valid_review_returns_pending(client):
    """spec test 55."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    response = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PENDING_REVIEW"
    assert body["review_version"] == 1
    assert body["has_manual_overrides"] is False
    assert body["verification_state"] == "EVIDENCE_BOUND"
    assert body["job_id"] == job_id
    assert body["cv_draft_id"] == cv_draft_id
    assert body["bewerbung_draft_id"] == bewerbung_draft_id


def test_create_missing_job_returns_404(client):
    test_client, _ = client
    response = _create_review(test_client, 999999, 1, 1)
    assert response.status_code == 404


def test_create_missing_cv_draft_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = _create_review(test_client, job_id, 999999, 1)
    assert response.status_code == 404


def test_create_missing_bewerbung_draft_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, _ = _ready_pair(test_client, session_factory, job_id)
    response = _create_review(test_client, job_id, cv_draft_id, 999999)
    assert response.status_code == 404


def test_create_source_mismatch_returns_422_zero_rows(client, monkeypatch):
    """spec test 56, constructed directly at the service layer: a
    Bewerbung draft whose own cv_draft_id disagrees with the CV draft
    passed to review-package creation."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    db = session_factory()
    from app.db.models import BewerbungDraftRecord

    bewerbung_record = db.get(BewerbungDraftRecord, bewerbung_draft_id)
    bewerbung_record.cv_draft_id = cv_draft_id + 999
    db.commit()
    db.close()

    response = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    assert response.status_code == 422
    assert "cv_draft_id" in response.json()["detail"]["mismatched_fields"]
    assert _count_reviews(session_factory) == 0


def test_create_wrong_job_cv_returns_422(client):
    """spec test 57: CV draft belongs to a different job than the URL."""
    test_client, session_factory = client
    job1_id = _seed_job(session_factory, fingerprint="fp-1", url="https://x.com/1")
    job2_id = _seed_job(session_factory, fingerprint="fp-2", url="https://x.com/2")
    cv1_draft_id, bewerbung1_id = _ready_pair(test_client, session_factory, job1_id)

    response = _create_review(test_client, job2_id, cv1_draft_id, bewerbung1_id)
    assert response.status_code == 422
    assert _count_reviews(session_factory) == 0


def test_create_stale_profile_returns_409_zero_rows(client):
    """spec test 58."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    r = test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={"expected_profile_version": version, "location_city": "Munich"},
    )
    assert r.status_code == 200

    response = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    assert response.status_code == 409
    assert _count_reviews(session_factory) == 0


def test_create_stale_job_returns_409_zero_rows(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    db = session_factory()
    record = db.get(JobRecord, job_id)
    record.description = "Completely different vacancy text now."
    db.commit()
    db.close()

    response = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    assert response.status_code == 409
    assert _count_reviews(session_factory) == 0


def test_create_duplicate_review_pairs_are_allowed(client):
    """spec section 36: no dedup — each POST is an independent attempt."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    first = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    second = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"]


# --- GET (never generates/mutates) ------------------------------------------


def test_get_review_for_job_missing_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    response = test_client.get(f"/api/v1/jobs/{job_id}/review-package", headers=_auth_headers())
    assert response.status_code == 404


def test_get_review_by_id_missing_returns_404(client):
    test_client, _ = client
    response = test_client.get("/api/v1/review-packages/999999", headers=_auth_headers())
    assert response.status_code == 404


def test_get_review_for_job_returns_latest(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)

    response = test_client.get(f"/api/v1/jobs/{job_id}/review-package", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json()["id"] == created.json()["id"]


def test_get_never_generates_or_creates(client, monkeypatch):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)

    def _boom(*args, **kwargs):
        raise AssertionError("GET must never create/generate a review package")

    monkeypatch.setattr("app.services.review_package.ReviewPackageService.create", _boom)
    response = test_client.get(f"/api/v1/jobs/{job_id}/review-package", headers=_auth_headers())
    assert response.status_code == 404


# --- PATCH: creates revision, requires PENDING, version CAS -----------------


def test_patch_edit_creates_new_revision_and_flags_overrides(client):
    """spec test 59."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    response = test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={
            "expected_review_version": 1,
            "bewerbung_changes": {"opening": "Edited opening line."},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_version"] == 2
    assert body["has_manual_overrides"] is True
    assert body["verification_state"] == "HUMAN_OVERRIDDEN"
    assert body["reviewed_bewerbung"]["opening"]["value"] == "Edited opening line."
    assert body["reviewed_bewerbung"]["opening"]["origin"] == "USER_EDIT"
    assert "bewerbung.opening" in body["manual_override_paths"]


def test_patch_stale_version_returns_409_no_lost_update(client):
    """spec test 60."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    first = test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={"expected_review_version": 1, "cv_changes": {"professional_summary": "A"}},
    )
    assert first.status_code == 200

    second = test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={"expected_review_version": 1, "cv_changes": {"professional_summary": "B"}},
    )
    assert second.status_code == 409

    final = test_client.get(
        f"/api/v1/review-packages/{created['id']}", headers=_auth_headers()
    ).json()
    assert final["review_version"] == 2
    assert final["reviewed_cv"]["professional_summary"]["value"] == "A"


def test_patch_decided_review_returns_409(client):
    """spec test 61."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    approve = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert approve.status_code == 200

    response = test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={"expected_review_version": 1, "cv_changes": {"professional_summary": "X"}},
    )
    assert response.status_code == 409


def test_patch_out_of_range_paragraph_index_returns_422(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    response = test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={
            "expected_review_version": 1,
            "bewerbung_changes": {"body_paragraphs": [{"index": 999, "text": "x"}]},
        },
    )
    assert response.status_code == 422


# --- approve: clean, manual-override ack, staleness, version CAS -----------


def test_approve_clean_no_edits(client):
    """spec test 62."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "APPROVED"
    assert body["decided_at"] is not None
    assert body["approved_revision_id"] == created["current_revision_id"]


def test_approve_manual_without_ack_returns_422(client):
    """spec test 63."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={
            "expected_review_version": 1,
            "bewerbung_changes": {
                "body_paragraphs": [{"index": 0, "text": "Ich habe AWS-Erfahrung."}]
            },
        },
    )

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 2, "acknowledge_manual_overrides": False},
    )
    assert response.status_code == 422

    still_pending = test_client.get(
        f"/api/v1/review-packages/{created['id']}", headers=_auth_headers()
    ).json()
    assert still_pending["status"] == "PENDING_REVIEW"


def test_approve_manual_with_ack_succeeds(client):
    """spec test 64: manual edit acknowledged with explicit ack=true."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={
            "expected_review_version": 1,
            "bewerbung_changes": {
                "body_paragraphs": [{"index": 0, "text": "Ich habe AWS-Erfahrung."}]
            },
        },
    )

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 2, "acknowledge_manual_overrides": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "APPROVED"
    assert body["verification_state"] == "HUMAN_OVERRIDDEN"
    assert body["has_manual_overrides"] is True


def test_approve_stale_review_version_returns_409(client):
    """spec test 65."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={"expected_review_version": 1, "cv_changes": {"professional_summary": "A"}},
    )
    test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={"expected_review_version": 2, "cv_changes": {"professional_summary": "B"}},
    )
    # Current version is now 3; approve expecting stale 2.
    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 2, "acknowledge_manual_overrides": True},
    )
    assert response.status_code == 409


def test_approve_after_profile_change_returns_409(client):
    """spec test 66."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={"expected_profile_version": version, "location_city": "Munich"},
    )

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert response.status_code == 409

    still_pending = test_client.get(
        f"/api/v1/review-packages/{created['id']}", headers=_auth_headers()
    ).json()
    assert still_pending["status"] == "PENDING_REVIEW"


def test_approve_after_job_change_returns_409(client):
    """spec test 67."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    db = session_factory()
    record = db.get(JobRecord, job_id)
    record.description = "Completely different vacancy text now."
    db.commit()
    db.close()

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert response.status_code == 409


# --- reject ------------------------------------------------------------------


def test_reject_transitions_and_sets_decided_at(client):
    """spec test 68."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/reject",
        headers=_auth_headers(),
        json={"expected_review_version": 1, "decision_note": "not good enough"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["decided_at"] is not None

    job_after = test_client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers()).json()
    assert job_after["status"] == "NEW"


# --- double decision / race --------------------------------------------------


def test_double_decision_approve_then_reject_returns_409(client):
    """spec test 69."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    approve = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert approve.status_code == 200

    reject = test_client.post(
        f"/api/v1/review-packages/{created['id']}/reject",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert reject.status_code == 409


def test_double_decision_reject_then_approve_returns_409(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    reject = test_client.post(
        f"/api/v1/review-packages/{created['id']}/reject",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert reject.status_code == 200

    approve = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert approve.status_code == 409


# --- original drafts remain immutable ---------------------------------------


def test_review_edits_never_mutate_original_drafts(client):
    """spec test 41/71."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    db = session_factory()
    from app.db.models import BewerbungDraftRecord, CandidateCVDraftRecord

    cv_json_before = db.get(CandidateCVDraftRecord, cv_draft_id).draft_json
    bewerbung_json_before = db.get(BewerbungDraftRecord, bewerbung_draft_id).draft_json
    db.close()

    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={
            "expected_review_version": 1,
            "cv_changes": {"professional_title": "Completely different title"},
            "bewerbung_changes": {"subject": "Completely different subject"},
        },
    )

    db = session_factory()
    cv_json_after = db.get(CandidateCVDraftRecord, cv_draft_id).draft_json
    bewerbung_json_after = db.get(BewerbungDraftRecord, bewerbung_draft_id).draft_json
    db.close()

    assert cv_json_before == cv_json_after
    assert bewerbung_json_before == bewerbung_json_after

    cv_response = test_client.get(f"/api/v1/cv-drafts/{cv_draft_id}", headers=_auth_headers())
    assert "Completely different title" not in cv_response.text
    bewerbung_response = test_client.get(
        f"/api/v1/bewerbung-drafts/{bewerbung_draft_id}", headers=_auth_headers()
    )
    assert "Completely different subject" not in bewerbung_response.text


# --- approved package endpoint -----------------------------------------------


def test_approved_package_read_after_approval(client):
    """spec test 72."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )

    response = test_client.get(f"/api/v1/jobs/{job_id}/approved-package", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert response.json()["status"] == "APPROVED"


def test_no_approved_package_when_only_pending_returns_404(client):
    """spec test 73."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)

    response = test_client.get(f"/api/v1/jobs/{job_id}/approved-package", headers=_auth_headers())
    assert response.status_code == 404


def test_no_approved_package_when_only_rejected_returns_404(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.post(
        f"/api/v1/review-packages/{created['id']}/reject",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )

    response = test_client.get(f"/api/v1/jobs/{job_id}/approved-package", headers=_auth_headers())
    assert response.status_code == 404


def test_approved_package_never_auto_approves(client, monkeypatch):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)

    def _boom(*args, **kwargs):
        raise AssertionError("GET approved-package must never approve anything")

    monkeypatch.setattr("app.services.review_package.ReviewPackageService.approve", _boom)
    response = test_client.get(f"/api/v1/jobs/{job_id}/approved-package", headers=_auth_headers())
    assert response.status_code == 404


# --- application status untouched -------------------------------------------


def test_review_lifecycle_never_mutates_application_status(client):
    """spec test 75."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    before = test_client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers()).json()

    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={"expected_review_version": 1, "cv_changes": {"professional_summary": "Edited."}},
    )
    test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 2},
    )

    after = test_client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers()).json()
    assert after["status"] == before["status"]


# --- no external action -----------------------------------------------------


def test_no_external_action_during_review_lifecycle(client, monkeypatch):
    """spec test 76: nothing in the review lifecycle may call a
    Bewerbung provider, Telegram, or any notifier."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    def _boom(*args, **kwargs):
        raise AssertionError("Review lifecycle must never call a Bewerbung provider")

    monkeypatch.setattr(
        "app.providers.bewerbung.deterministic.DeterministicBewerbungProvider.generate_plan",
        _boom,
    )

    async def _telegram_boom(*args, **kwargs):
        raise AssertionError("Review lifecycle must never send a Telegram message")

    monkeypatch.setattr("app.services.telegram.TelegramNotifier.send_job", _telegram_boom)

    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.patch(
        f"/api/v1/review-packages/{created['id']}",
        headers=_auth_headers(),
        json={"expected_review_version": 1, "cv_changes": {"professional_summary": "Edited."}},
    )
    approve = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 2, "acknowledge_manual_overrides": True},
    )
    assert approve.status_code == 200


def test_get_approved_package_pins_exact_revision_not_latest_pending(client):
    """spec test 32/72: the approved-package endpoint must never fall
    back to a job's latest (possibly newer, unrelated) PENDING review."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    first = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    test_client.post(
        f"/api/v1/review-packages/{first['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    # A second, independent PENDING review for the same job/pair must not
    # be returned as "the approved package".
    second = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()
    assert second["status"] == "PENDING_REVIEW"

    response = test_client.get(f"/api/v1/jobs/{job_id}/approved-package", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json()["id"] == first["id"]
    assert response.json()["status"] == "APPROVED"


# --- blocker fix: approval must fail closed when current authorities are missing ---


def test_approve_when_profile_missing_returns_409_and_never_recreates_it(client):
    """spec section 2/9/10/26: a deleted Candidate Profile must never be
    silently recreated to pass the version check — approval must fail
    closed instead."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    _delete_candidate_profile(session_factory)
    assert _count_candidate_profiles(session_factory) == 0

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert response.status_code == 409
    # Critical: the failed approval must not have created a replacement
    # profile row merely by looking it up.
    assert _count_candidate_profiles(session_factory) == 0

    still_pending = test_client.get(
        f"/api/v1/review-packages/{created['id']}", headers=_auth_headers()
    ).json()
    assert still_pending["status"] == "PENDING_REVIEW"
    assert still_pending["approved_revision_id"] is None
    assert still_pending["decided_at"] is None
    assert still_pending["review_version"] == 1


def test_approve_when_job_missing_returns_409(client):
    """spec section 1/11/12/26."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    _delete_job(session_factory, job_id)

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert response.status_code == 409

    still_pending = test_client.get(
        f"/api/v1/review-packages/{created['id']}", headers=_auth_headers()
    ).json()
    assert still_pending["status"] == "PENDING_REVIEW"
    assert still_pending["approved_revision_id"] is None
    assert still_pending["decided_at"] is None


def test_create_when_profile_missing_fails_closed_zero_rows(client):
    """spec section 15/16: creation must apply the same fail-closed
    authority rule — LOOKUP != CREATE."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    _delete_candidate_profile(session_factory)
    assert _count_candidate_profiles(session_factory) == 0

    response = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    assert response.status_code == 409
    assert _count_candidate_profiles(session_factory) == 0
    assert _count_reviews(session_factory) == 0


def test_create_when_job_missing_returns_404_zero_rows(client):
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)

    _delete_job(session_factory, job_id)

    response = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id)
    assert response.status_code == 404
    assert _count_reviews(session_factory) == 0


def test_get_review_still_readable_after_job_and_profile_deletion(client):
    """spec section 13: historical audit reads survive upstream deletion
    even though approval does not."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    _delete_candidate_profile(session_factory)
    _delete_job(session_factory, job_id)

    response = test_client.get(f"/api/v1/review-packages/{created['id']}", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert response.json()["status"] == "PENDING_REVIEW"


def test_reject_allowed_when_profile_and_job_missing(client):
    """spec section 14: rejecting stale/unverifiable material remains
    safe and does not require current-source freshness."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    _delete_candidate_profile(session_factory)
    _delete_job(session_factory, job_id)

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/reject",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"


def test_approve_still_checks_profile_version_after_authority_confirmed_present(client):
    """Regression guard: the missing-authority fix must not have broken
    the existing 'exists but changed version' staleness check."""
    test_client, session_factory = client
    job_id = _seed_job(session_factory)
    cv_draft_id, bewerbung_draft_id = _ready_pair(test_client, session_factory, job_id)
    created = _create_review(test_client, job_id, cv_draft_id, bewerbung_draft_id).json()

    r = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
    version = r.json()["profile_version"]
    test_client.patch(
        "/api/v1/candidate-profile",
        headers=_auth_headers(),
        json={"expected_profile_version": version, "location_city": "Munich"},
    )

    response = test_client.post(
        f"/api/v1/review-packages/{created['id']}/approve",
        headers=_auth_headers(),
        json={"expected_review_version": 1},
    )
    assert response.status_code == 409
    # The profile itself must still exist and be untouched by Stage 6E.
    assert _count_candidate_profiles(session_factory) == 1
