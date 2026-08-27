from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.security import rate_limit as rate_limit_module
from app.security.auth import require_api_key
from app.security.rate_limit import enforce_rate_limit


def _fake_request(host: str = "1.2.3.4"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """Each test starts with a clean in-memory rate-limit state."""
    rate_limit_module._requests.clear()
    yield
    rate_limit_module._requests.clear()


class TestRequireApiKey:
    def test_missing_header_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.auth.get_settings",
            lambda: Settings(api_key="secret-key"),
        )
        with pytest.raises(HTTPException) as exc:
            require_api_key(x_api_key="")
        assert exc.value.status_code == 401

    def test_wrong_key_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.auth.get_settings",
            lambda: Settings(api_key="secret-key"),
        )
        with pytest.raises(HTTPException) as exc:
            require_api_key(x_api_key="wrong-key")
        assert exc.value.status_code == 401

    def test_correct_key_is_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.auth.get_settings",
            lambda: Settings(api_key="secret-key"),
        )
        # Should not raise.
        require_api_key(x_api_key="secret-key")

    def test_unset_api_key_fails_closed(self, monkeypatch):
        """If API_KEY is not configured, every request must be rejected
        rather than silently accepted."""
        monkeypatch.setattr(
            "app.security.auth.get_settings",
            lambda: Settings(api_key=""),
        )
        with pytest.raises(HTTPException) as exc:
            require_api_key(x_api_key="")
        assert exc.value.status_code == 401


class TestEnforceRateLimit:
    def test_allows_requests_under_the_limit(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(rate_limit_requests=3, rate_limit_window_seconds=60),
        )
        request = _fake_request()
        for _ in range(3):
            enforce_rate_limit(request)  # should not raise

    def test_blocks_requests_over_the_limit(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(rate_limit_requests=2, rate_limit_window_seconds=60),
        )
        request = _fake_request()
        enforce_rate_limit(request)
        enforce_rate_limit(request)
        with pytest.raises(HTTPException) as exc:
            enforce_rate_limit(request)
        assert exc.value.status_code == 429

    def test_limits_are_tracked_independently_per_client(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(rate_limit_requests=1, rate_limit_window_seconds=60),
        )
        enforce_rate_limit(_fake_request(host="1.1.1.1"))
        # A different client host must not be affected by the first one.
        enforce_rate_limit(_fake_request(host="2.2.2.2"))

    def test_missing_client_falls_back_to_unknown_bucket(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(rate_limit_requests=1, rate_limit_window_seconds=60),
        )
        request = SimpleNamespace(client=None)
        enforce_rate_limit(request)
        with pytest.raises(HTTPException):
            enforce_rate_limit(request)


def test_get_settings_still_cached_singleton():
    # Sanity check that we didn't break the real dependency wiring.
    assert get_settings() is get_settings()
