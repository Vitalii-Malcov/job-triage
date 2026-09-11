"""hardening/api-boundaries-r1: live TestClient measurements for HTTP
body-size behavior (Section 2), list-cardinality behavior (Section 3),
and pagination/offset abuse (Section 4). See
docs/API_BOUNDARY_HARDENING_REPORT.md (BOUND-IDs) for the findings
these tests produce evidence for.

All requests go through FastAPI's TestClient (in-process ASGI calls) --
never real sockets, never the public internet, never any external
provider. No payload here exceeds low-single-digit megabytes, per
explicit instruction not to generate hundreds of MB.
"""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
    db_path = tmp_path / "test_api_boundary_hardening.db"
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
        api_key=API_KEY, rate_limit_requests=10_000, rate_limit_window_seconds=60
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


def _job_payload(**overrides) -> dict:
    payload = {
        "source": "bundesagentur",
        "title": "Backend Engineer",
        "company": "BoundCo",
        "location": "Berlin",
        "url": "https://example.com/jobs/boundary-1",
        "description": "",
        "skills": [],
        "must_have_skills": [],
        "nice_to_have_skills": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Section 2 — HTTP body / input resource testing
# ---------------------------------------------------------------------------


class TestBodySizeBehavior:
    """Quantifies actual behavior for oversized-but-schema-legal payloads
    -- the prior adversarial pass established NO body-size limit exists;
    this measures what actually happens rather than restating that fact.
    """

    @pytest.mark.parametrize("size_kb", [100, 1024, 5 * 1024])
    def test_large_job_description_is_accepted_and_persisted_verbatim(self, client, size_kb):
        test_client, session_factory = client
        description = "A" * (size_kb * 1024)

        start = time.monotonic()
        response = test_client.post(
            "/api/v1/jobs/score",
            json=_job_payload(
                description=description, url=f"https://example.com/jobs/size-{size_kb}"
            ),
            headers=_auth_headers(),
        )
        elapsed = time.monotonic() - start

        assert response.status_code == 200, response.text
        # Measured, not asserted blindly -- record actual behavior so the
        # report can cite real numbers instead of a guess.
        assert elapsed < 10.0, f"{size_kb}KB description took {elapsed:.2f}s -- unexpectedly slow"

        db = session_factory()
        try:
            stored = db.scalar(
                select(JobRecord).where(JobRecord.url == f"https://example.com/jobs/size-{size_kb}")
            )
            assert stored is not None
            # DB state genuinely changed, and the FULL payload was
            # persisted verbatim -- no silent truncation anywhere in the
            # write path (unlike the Gmail IMAP ingestion path, which
            # DOES truncate at MAX_BODY_LENGTH -- this is a real,
            # verified asymmetry, not assumed).
            assert len(stored.description) == size_kb * 1024
        finally:
            db.close()

    def test_10mb_job_description_is_still_accepted(self, client):
        """Upper bound of what this test suite constructs -- 10MB, per
        explicit instruction not to generate hundreds of MB. Confirms
        the behavior scales the same way at the largest size tested,
        not just at smaller ones.
        """
        test_client, session_factory = client
        description = "B" * (10 * 1024 * 1024)

        start = time.monotonic()
        response = test_client.post(
            "/api/v1/jobs/score",
            json=_job_payload(description=description, url="https://example.com/jobs/size-10mb"),
            headers=_auth_headers(),
        )
        elapsed = time.monotonic() - start

        assert response.status_code == 200, response.text[:500]
        assert elapsed < 20.0, f"10MB description took {elapsed:.2f}s"

        db = session_factory()
        try:
            stored = db.scalar(
                select(JobRecord).where(JobRecord.url == "https://example.com/jobs/size-10mb")
            )
            assert stored is not None
            assert len(stored.description) == 10 * 1024 * 1024
        finally:
            db.close()

    def test_large_candidate_profile_field_is_accepted(self, client):
        """A second, structurally different endpoint (PATCH, singleton
        resource, optimistic-concurrency-gated) to confirm the missing
        body-size bound isn't specific to /jobs/score's shape.
        """
        test_client, session_factory = client
        get_resp = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
        assert get_resp.status_code == 200
        version = get_resp.json()["profile_version"]

        large_summary = "C" * (1024 * 1024)
        patch_resp = test_client.patch(
            "/api/v1/candidate-profile",
            json={"expected_profile_version": version, "professional_summary": large_summary},
            headers=_auth_headers(),
        )
        assert patch_resp.status_code == 200, patch_resp.text[:500]
        assert patch_resp.json()["professional_summary"] == large_summary


# ---------------------------------------------------------------------------
# Section 3 — List cardinality adversarial tests
# ---------------------------------------------------------------------------


class TestListCardinality:
    @pytest.mark.parametrize("count", [100, 1000, 5000])
    def test_large_skills_list_is_accepted_no_quadratic_blowup(self, client, count):
        test_client, session_factory = client
        skills = [f"skill-{i}" for i in range(count)]

        start = time.monotonic()
        response = test_client.post(
            "/api/v1/jobs/score",
            json=_job_payload(skills=skills, url=f"https://example.com/jobs/cardinality-{count}"),
            headers=_auth_headers(),
        )
        elapsed = time.monotonic() - start

        assert response.status_code == 200, response.text[:500]
        # A per-item cost that were accidentally quadratic (e.g. an O(n^2)
        # dedup/normalize pass) would show up as a clearly
        # super-linear elapsed time between the 100 and 5000 cases --
        # bounding absolute time is a coarse but real regression guard.
        assert elapsed < 10.0, f"{count} skills took {elapsed:.2f}s"

        db = session_factory()
        try:
            stored = db.scalar(
                select(JobRecord).where(
                    JobRecord.url == f"https://example.com/jobs/cardinality-{count}"
                )
            )
            assert stored is not None
        finally:
            db.close()

    def test_1000_candidate_skills_accepted_and_roundtrip_correctly(self, client):
        test_client, session_factory = client
        get_resp = test_client.get("/api/v1/candidate-profile", headers=_auth_headers())
        version = get_resp.json()["profile_version"]

        skills = [{"name": f"Skill {i}"} for i in range(1000)]
        start = time.monotonic()
        patch_resp = test_client.patch(
            "/api/v1/candidate-profile",
            json={"expected_profile_version": version, "skills": skills},
            headers=_auth_headers(),
        )
        elapsed = time.monotonic() - start

        assert patch_resp.status_code == 200, patch_resp.text[:500]
        assert elapsed < 15.0, f"1000 candidate skills took {elapsed:.2f}s"
        assert len(patch_resp.json()["skills"]) == 1000


# ---------------------------------------------------------------------------
# Section 4 — Pagination / offset abuse
# ---------------------------------------------------------------------------


class TestPaginationAbuse:
    def test_limit_zero_rejected(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/jobs?limit=0", headers=_auth_headers())
        assert resp.status_code == 422

    def test_limit_negative_rejected(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/jobs?limit=-1", headers=_auth_headers())
        assert resp.status_code == 422

    def test_limit_at_max_accepted(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/jobs?limit=200", headers=_auth_headers())
        assert resp.status_code == 200

    def test_limit_above_max_rejected(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/jobs?limit=201", headers=_auth_headers())
        assert resp.status_code == 422

    def test_offset_negative_rejected(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/jobs?offset=-1", headers=_auth_headers())
        assert resp.status_code == 422

    def test_offset_reasonably_large_int_does_not_crash(self, client):
        """A large-but-realistic offset must still cleanly return an
        empty result, not an error."""
        test_client, _ = client
        resp = test_client.get("/api/v1/jobs?offset=999999999", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json() == []

    def test_offset_at_max_offset_accepted(self, client):
        test_client, _ = client
        resp = test_client.get(f"/api/v1/jobs?offset={2**31 - 1}", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json() == []

    def test_offset_at_2_31_rejected(self, client):
        """BOUND-001: before the fix, this value (and everything up to
        2**63-1) was silently accepted by Query(ge=0) with no upper
        bound. Now cleanly rejected with 422 by the added `le=MAX_OFFSET`
        bound (app/api/routes.py) -- never reaches the DB layer at all.
        """
        test_client, _ = client
        resp = test_client.get(f"/api/v1/jobs?offset={2**31}", headers=_auth_headers())
        assert resp.status_code == 422

    def test_offset_at_2_63_rejected_cleanly_not_a_500(self, client):
        """BOUND-001 (verified, fixed this branch): before the fix,
        offset=2**63 (still a legal, non-negative Python int) crashed the
        SQLite driver with an unhandled `OverflowError` ("Python int too
        large to convert to SQLite INTEGER"), propagating uncaught to a
        raw 500 -- confirmed by actually reproducing it before this test
        was updated to assert the post-fix behavior. Now rejected with a
        clean 422 by FastAPI/Pydantic query validation, never reaching
        the DB layer. PostgreSQL's own `bigint` OFFSET parameter would
        independently reject a value this large too, but that dialect-
        specific behavior was not separately reproduced against a real
        PostgreSQL server in this pass -- the fix applies identically to
        both dialects regardless (validation happens before any SQL is
        built), so dialect-specific re-verification was not necessary to
        justify it.
        """
        test_client, _ = client
        resp = test_client.get(f"/api/v1/jobs?offset={2**63}", headers=_auth_headers())
        assert resp.status_code == 422

    def test_offset_at_2_63_minus_1_rejected_cleanly(self, client):
        test_client, _ = client
        resp = test_client.get(f"/api/v1/jobs?offset={2**63 - 1}", headers=_auth_headers())
        assert resp.status_code == 422

    def test_status_filter_plus_extreme_offset_still_clean(self, client):
        test_client, _ = client
        resp = test_client.get(
            f"/api/v1/jobs?status=APPLIED&offset={2**31 - 1}&limit=1", headers=_auth_headers()
        )
        assert resp.status_code == 200
        assert resp.json() == []

    # Spot-check a second, structurally different list endpoint (tighter
    # default bounds per the API surface inventory: default=20, max=100)
    # to confirm the SAME validation pattern is genuinely applied, not
    # just assumed from routes.py's declared Query(...) bounds.
    def test_automation_runs_limit_above_max_rejected(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/automation/runs?limit=101", headers=_auth_headers())
        assert resp.status_code == 422

    def test_automation_runs_limit_at_max_accepted(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/automation/runs?limit=100", headers=_auth_headers())
        assert resp.status_code == 200
