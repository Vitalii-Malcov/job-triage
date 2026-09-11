"""hardening/api-boundaries-r1, Section 5: demonstrates the CURRENT
behavior of auth-before-rate-limit ordering (every route's
`dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)]`
declares auth first, uniformly, across all ~50 routes). See
docs/API_BOUNDARY_HARDENING_REPORT.md BOUND-002 for the full
architecture analysis (A/B/C) and why no reordering is implemented in
this branch.
"""

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
    db_path = tmp_path / "test_auth_rate_limit_ordering.db"
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

    # A tight rate limit so the test can cross it deterministically in a
    # small number of requests.
    fake_settings = Settings(api_key=API_KEY, rate_limit_requests=3, rate_limit_window_seconds=60)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


class TestInvalidKeyRequestsDoNotConsumeRateLimitQuota:
    def test_unlimited_invalid_key_attempts_never_429(self, client):
        """BOUND-002 (documented, not fixed): auth runs before the rate
        limiter in every route's dependency list, so a wrong API key
        short-circuits with 401 before ever reaching
        enforce_rate_limit -- confirmed here by sending 10 invalid-key
        requests (more than the configured limit of 3) and observing
        every single one still returns 401, never 429. An attacker
        brute-forcing the API key is not throttled by this mechanism at
        all.
        """
        for _ in range(10):
            resp = client.get("/api/v1/jobs", headers={"X-API-Key": "wrong-key"})
            assert resp.status_code == 401

    def test_valid_key_requests_are_still_rate_limited_normally(self, client):
        """Control: confirms the rate limiter itself still works for
        authenticated traffic -- the gap is specific to unauthenticated
        (wrong-key) requests, not a general rate-limiter failure.
        """
        for _ in range(3):
            resp = client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY})
            assert resp.status_code == 200
        fourth = client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY})
        assert fourth.status_code == 429

    def test_invalid_and_valid_key_attempts_from_same_host_do_not_share_a_budget(self, client):
        """Confirms the CURRENT (auth-first) design's one genuine
        advantage: an attacker spamming wrong keys from the same source
        host as the legitimate operator (e.g. both behind one reverse
        proxy / NAT -- the documented AUD-003 scenario) does NOT erode
        the operator's own valid-key request budget, because invalid
        attempts never touch the rate limiter's bucket at all. This is
        exactly the property a naive "swap the dependency order"
        response-draft-send.py-style fix could accidentally destroy --
        see BOUND-002's Option B analysis.
        """
        for _ in range(20):
            resp = client.get("/api/v1/jobs", headers={"X-API-Key": "wrong-key"})
            assert resp.status_code == 401

        # The legitimate operator's own budget (3 requests) is untouched
        # by the 20 failed attempts above.
        for _ in range(3):
            resp = client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY})
            assert resp.status_code == 200
