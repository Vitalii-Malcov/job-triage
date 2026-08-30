import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_candidate_profile_endpoints.db"
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

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


class TestGetCandidateProfile:
    def test_requires_api_key(self, client):
        response = client.get("/api/v1/candidate-profile")
        assert response.status_code == 401

    def test_not_yet_initialized_returns_200_with_empty_profile(self, client):
        """No 404 state: the singleton is created empty on first access."""
        response = client.get("/api/v1/candidate-profile", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["profile_version"] == 1
        assert body["first_name"] is None
        assert body["skills"] == []
        assert body["experiences"] == []
        assert body["field_trust"] == {}
        assert body["job_preferences"]["remote_preference"] == "UNKNOWN"

    def test_get_reflects_prior_patches(self, client):
        client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "first_name": "Anna"},
        )
        response = client.get("/api/v1/candidate-profile", headers=_auth_headers())

        assert response.json()["first_name"] == "Anna"
        assert response.json()["profile_version"] == 2


class TestPatchCandidateProfile:
    def test_requires_api_key(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            json={"expected_profile_version": 1, "first_name": "Anna"},
        )
        assert response.status_code == 401

    def test_patch_scalar_field(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "professional_title": "Junior Python Developer"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["professional_title"] == "Junior Python Developer"
        assert body["profile_version"] == 2
        assert body["field_trust"]["professional_title"] == {
            "source": "MANUAL_ENTRY",
            "confidence": "CONFIRMED",
        }

    def test_patch_does_not_erase_unrelated_fields(self, client):
        client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "skills": [{"name": "Python"}],
                "education": [{"institution": "TU Berlin"}],
                "projects": [{"name": "Job Triage"}],
                "languages": [{"language": "German", "level": "B2"}],
            },
        )

        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 2, "professional_title": "Junior Python Developer"},
        )

        body = response.json()
        assert body["professional_title"] == "Junior Python Developer"
        assert [s["name"] for s in body["skills"]] == ["Python"]
        assert [e["institution"] for e in body["education"]] == ["TU Berlin"]
        assert [p["name"] for p in body["projects"]] == ["Job Triage"]
        assert [lang["language"] for lang in body["languages"]] == ["German"]

    def test_patch_replaces_list_field_wholesale(self, client):
        client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "skills": [{"name": "Python"}, {"name": "SQL"}]},
        )
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 2, "skills": [{"name": "Docker"}]},
        )

        assert [s["name"] for s in response.json()["skills"]] == ["Docker"]

    def test_invalid_skill_blank_name_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "skills": [{"name": "   "}]},
        )
        assert response.status_code == 422

    def test_duplicate_skill_in_payload_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "skills": [{"name": "Python"}, {"name": "python"}],
            },
        )
        assert response.status_code == 422

    def test_invalid_experience_dates_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "experiences": [
                    {
                        "company": "Acme",
                        "job_title": "Dev",
                        "start_date": "2022-01-01",
                        "end_date": "2020-01-01",
                    }
                ],
            },
        )
        assert response.status_code == 422

    def test_current_experience_with_end_date_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "experiences": [
                    {
                        "company": "Acme",
                        "job_title": "Dev",
                        "is_current": True,
                        "end_date": "2020-01-01",
                    }
                ],
            },
        )
        assert response.status_code == 422

    def test_invalid_language_level_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "languages": [{"language": "German", "level": "Z9"}],
            },
        )
        assert response.status_code == 422

    def test_invalid_url_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "projects": [{"name": "X", "repository_url": "not-a-url"}],
            },
        )
        assert response.status_code == 422

    def test_unknown_extra_field_is_ignored(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "not_a_real_field": "ignored by default pydantic extra=ignore",
            },
        )
        # Unknown keys are ignored (default Pydantic config); a valid
        # expected_profile_version with no recognized profile fields is
        # simply an empty-body PATCH — no mutation, version unchanged.
        assert response.status_code == 200
        assert response.json()["profile_version"] == 1

    def test_completely_invalid_json_type_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "skills": "not-a-list"},
        )
        assert response.status_code == 422

    def test_job_preferences_patch(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "job_preferences": {
                    "minimum_salary": 45000,
                    "salary_currency": "EUR",
                    "remote_preference": "HYBRID",
                    "relocation": False,
                },
            },
        )
        assert response.status_code == 200
        prefs = response.json()["job_preferences"]
        assert prefs["minimum_salary"] == 45000
        assert prefs["remote_preference"] == "HYBRID"
        assert prefs["relocation"] is False

    # --- CP-M-02: field_trust ------------------------------------------

    def test_field_trust_explicit_override_round_trips(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "professional_summary": "Presumably backend-focused.",
                "field_trust": {
                    "professional_summary": {"source": "INFERRED", "confidence": "UNCONFIRMED"}
                },
            },
        )
        assert response.status_code == 200
        trust = response.json()["field_trust"]["professional_summary"]
        assert trust == {"source": "INFERRED", "confidence": "UNCONFIRMED"}

    def test_field_trust_for_field_not_in_payload_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={
                "expected_profile_version": 1,
                "professional_title": "Dev",
                "field_trust": {"career_goal": {"source": "INFERRED", "confidence": "UNCONFIRMED"}},
            },
        )
        assert response.status_code == 422

    # --- CP-M-03: optimistic concurrency --------------------------------

    def test_missing_expected_profile_version_returns_422(self, client):
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"professional_title": "Junior Python Developer"},
        )
        assert response.status_code == 422

    def test_correct_expected_profile_version_returns_200(self, client):
        get_response = client.get("/api/v1/candidate-profile", headers=_auth_headers())
        version = get_response.json()["profile_version"]

        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": version, "first_name": "Anna"},
        )
        assert response.status_code == 200
        assert response.json()["profile_version"] == version + 1

    def test_stale_expected_profile_version_returns_409(self, client):
        client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "first_name": "Anna"},
        )

        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "last_name": "Muster"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["current_profile_version"] == 2

        # The failed stale write must not have applied.
        get_response = client.get("/api/v1/candidate-profile", headers=_auth_headers())
        assert get_response.json()["last_name"] is None
        assert get_response.json()["first_name"] == "Anna"

    def test_conflict_response_never_leaks_profile_content(self, client):
        client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "first_name": "VeryUniqueNameXyz123"},
        )
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "last_name": "Anything"},
        )
        assert response.status_code == 409
        assert "VeryUniqueNameXyz123" not in response.text


class TestSecurity:
    def test_full_profile_never_logged(self, client, caplog):
        """Stage 6A section 21: candidate details must never reach logs."""
        with caplog.at_level(logging.DEBUG):
            client.patch(
                "/api/v1/candidate-profile",
                headers=_auth_headers(),
                json={
                    "expected_profile_version": 1,
                    "first_name": "Zbigniew",
                    "last_name": "Uncommonlastname",
                    "skills": [{"name": "VeryUniqueSkillNameXyz123"}],
                },
            )

        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert "Zbigniew" not in log_text
        assert "Uncommonlastname" not in log_text
        assert "VeryUniqueSkillNameXyz123" not in log_text

    def test_module_never_imports_an_http_client(self):
        """No external networking anywhere in Stage 6A (section 25)."""
        import inspect

        import app.db.candidate_profile_repository as repo_module

        source = inspect.getsource(repo_module)
        for forbidden in ("httpx", "requests", "aiohttp", "urllib.request", "http.client"):
            assert forbidden not in source

    def test_no_application_status_side_effect(self, client):
        """Patching the candidate profile must never touch job/application
        state (section 23's human-approval invariant) — there is no job_id
        anywhere in this API's request/response shape at all.
        """
        response = client.patch(
            "/api/v1/candidate-profile",
            headers=_auth_headers(),
            json={"expected_profile_version": 1, "first_name": "Anna"},
        )
        assert "job_id" not in response.json()
        assert "status" not in response.json()
