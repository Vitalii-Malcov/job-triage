"""Failed API-key attempts are throttled per client before the key is compared.

Review finding 1.1: `require_api_key` runs first on every route, so a wrong
key used to return 401 forever. Failures now fill a shared per-client budget
(`rate_limit.AUTH_FAILURE_LIMIT` in `AUTH_FAILURE_WINDOW_SECONDS`); a client
that exhausted it gets 429 on every route -- even with the right key -- until
the window slides. Trade-off (accepted): an operator behind the same NAT/proxy
identity as an attacker is locked out with them.
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
    rate_limit_module._auth_failure_requests.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._auth_failure_requests.clear()


class TestFailedAuthIsThrottled:
    def test_wrong_keys_hit_429_after_the_failure_limit(self, client):
        limit = rate_limit_module.AUTH_FAILURE_LIMIT
        for _ in range(limit):
            assert client.get("/api/v1/jobs", headers={"X-API-Key": "wrong"}).status_code == 401
        resp = client.get("/api/v1/jobs", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 429
        assert "wrong" not in resp.text

    def test_correct_key_is_refused_while_locked_out(self, client):
        for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT):
            client.get("/api/v1/jobs", headers={"X-API-Key": "wrong"})
        resp = client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY})
        assert resp.status_code == 429

    def test_budget_is_shared_across_endpoints(self, client):
        limit = rate_limit_module.AUTH_FAILURE_LIMIT
        for i in range(limit):
            path = "/api/v1/jobs" if i % 2 else "/api/v1/jobs/1"
            assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/api/v1/jobs/2", headers={"X-API-Key": "wrong"}).status_code == 429

    def test_successful_requests_never_consume_the_failure_budget(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(
                api_key=API_KEY, rate_limit_requests=1000, rate_limit_window_seconds=60
            ),
        )
        for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT * 3):
            assert client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY}).status_code == 200

    def test_failures_expire_with_the_window(self, client, monkeypatch):
        for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT):
            client.get("/api/v1/jobs", headers={"X-API-Key": "wrong"})
        real = rate_limit_module.time.monotonic
        monkeypatch.setattr(
            rate_limit_module.time,
            "monotonic",
            lambda: real() + rate_limit_module.AUTH_FAILURE_WINDOW_SECONDS + 1,
        )
        assert client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY}).status_code == 200

    def test_valid_key_requests_are_still_rate_limited_normally(self, client):
        for _ in range(3):
            assert client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY}).status_code == 200
        assert client.get("/api/v1/jobs", headers={"X-API-Key": API_KEY}).status_code == 429

    def test_non_ascii_key_is_a_401_not_a_500(self, client):
        resp = client.get("/api/v1/jobs", headers={"X-API-Key": "kéy".encode("latin-1")})
        assert resp.status_code == 401

    def test_ui_login_failures_share_the_budget(self, client, monkeypatch):
        fake = Settings(api_key=API_KEY, ui_enabled=True)
        monkeypatch.setattr("app.ui.routes.get_settings", lambda: fake)
        monkeypatch.setattr("app.ui.auth.get_settings", lambda: fake)
        for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT):
            client.get("/api/v1/jobs", headers={"X-API-Key": "wrong"})
        resp = client.post("/ui/login", data={"api_key": API_KEY})
        assert resp.status_code == 429


class TestForgedCookiesAndAtomicity:
    def test_forged_ui_cookies_are_charged_to_the_budget(self, client, monkeypatch):
        fake = Settings(api_key=API_KEY, ui_enabled=True)
        monkeypatch.setattr("app.ui.routes.get_settings", lambda: fake)
        monkeypatch.setattr("app.ui.auth.get_settings", lambda: fake)
        from app.ui.auth import COOKIE_NAME, issue_session_token

        limit = rate_limit_module.AUTH_FAILURE_LIMIT
        for i in range(limit):
            forged = issue_session_token(f"guess-{i}", hours=1)
            client.cookies.set(COOKIE_NAME, forged, path="/ui")
            r = client.get("/ui/", follow_redirects=False)
            assert r.status_code == 303
        # Locked out: even a cookie signed with the REAL key is refused.
        client.cookies.set(COOKIE_NAME, issue_session_token(API_KEY, hours=1), path="/ui")
        assert client.get("/ui/", follow_redirects=False).status_code == 429

    def test_valid_cookie_and_cookieless_requests_do_not_consume_budget(self, monkeypatch):
        from types import SimpleNamespace

        from app.ui.auth import (
            COOKIE_NAME,
            NotLoggedInError,
            issue_session_token,
            require_ui_session,
        )

        fake = Settings(api_key=API_KEY, ui_enabled=True)
        monkeypatch.setattr("app.ui.auth.get_settings", lambda: fake)
        peer = SimpleNamespace(host="8.8.8.8")
        anon = SimpleNamespace(client=peer, headers={}, cookies={})
        good = SimpleNamespace(
            client=peer,
            headers={},
            cookies={COOKIE_NAME: issue_session_token(API_KEY, hours=1)},
        )
        for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT * 3):
            with pytest.raises(NotLoggedInError):
                require_ui_session(anon)
            require_ui_session(good)
        assert not rate_limit_module._auth_failure_requests

    def test_concurrent_wrong_keys_never_exceed_the_limit(self):
        import threading
        from types import SimpleNamespace

        from fastapi import HTTPException

        from app.security.auth import require_api_key

        request = SimpleNamespace(client=SimpleNamespace(host="9.9.9.9"), headers={})
        statuses: list[int] = []
        guard = threading.Lock()

        def attempt():
            try:
                require_api_key(request, x_api_key="wrong")
            except HTTPException as exc:
                with guard:
                    statuses.append(exc.status_code)

        threads = [threading.Thread(target=attempt) for _ in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert statuses.count(401) == rate_limit_module.AUTH_FAILURE_LIMIT
        assert statuses.count(429) == 60 - rate_limit_module.AUTH_FAILURE_LIMIT

    def test_distinct_identities_are_capped(self):
        from types import SimpleNamespace

        limiter = rate_limit_module._auth_failure_limiter
        cap = limiter._MAX_KEYS
        for i in range(cap + 50):
            req = SimpleNamespace(
                client=SimpleNamespace(host=f"10.{i // 256 % 256}.{i % 256}.1"), headers={}
            )
            rate_limit_module.begin_auth_attempt(req)
        assert len(limiter.buckets) <= cap


class TestLockoutRetentionAndExactRefund:
    @staticmethod
    def _req(host):
        from types import SimpleNamespace

        return SimpleNamespace(client=SimpleNamespace(host=host), headers={})

    def test_locked_identity_survives_address_churn(self):
        from fastapi import HTTPException

        limiter = rate_limit_module._auth_failure_limiter
        victim = self._req("1.1.1.1")
        for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT):
            rate_limit_module.begin_auth_attempt(victim)
        for i in range(limiter._MAX_KEYS + 100):
            rate_limit_module.begin_auth_attempt(
                self._req(f"10.{i // 65536}.{i // 256 % 256}.{i % 256}")
            )
        with pytest.raises(HTTPException) as exc:
            rate_limit_module.begin_auth_attempt(victim)
        assert exc.value.status_code == 429

    def test_table_full_of_locked_identities_refuses_newcomers(self, monkeypatch):
        from fastapi import HTTPException

        limiter = rate_limit_module._auth_failure_limiter
        monkeypatch.setattr(limiter, "_MAX_KEYS", 3)
        for h in ("2.0.0.1", "2.0.0.2", "2.0.0.3"):
            for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT):
                rate_limit_module.begin_auth_attempt(self._req(h))
        with pytest.raises(HTTPException) as exc:
            rate_limit_module.begin_auth_attempt(self._req("2.0.0.4"))
        assert exc.value.status_code == 429

    def test_refund_removes_its_own_reservation_not_a_later_failure(self):
        req = self._req("3.3.3.3")
        ok_token = rate_limit_module.begin_auth_attempt(req)
        for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT - 1):
            rate_limit_module.begin_auth_attempt(req)  # failures after the success
        rate_limit_module.end_auth_attempt_ok(req, ok_token)
        bucket = rate_limit_module._auth_failure_requests["3.3.3.3"]
        assert ok_token not in bucket
        assert len(bucket) == rate_limit_module.AUTH_FAILURE_LIMIT - 1


def test_ipv6_clients_are_tracked_per_64():
    from types import SimpleNamespace

    from fastapi import HTTPException

    def req(host):
        return SimpleNamespace(client=SimpleNamespace(host=host), headers={})

    for i in range(rate_limit_module.AUTH_FAILURE_LIMIT):
        rate_limit_module.begin_auth_attempt(req(f"2001:db8:1:2::{i + 1:x}"))
    with pytest.raises(HTTPException):
        rate_limit_module.begin_auth_attempt(req("2001:db8:1:2:aaaa:bbbb:cccc:dddd"))
    # a different /64 is unaffected
    rate_limit_module.begin_auth_attempt(req("2001:db8:1:3::1"))
