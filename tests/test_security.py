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


class TestRateLimiterBucketEviction:
    """HARD-001 (adversarial hardening r1): a distinct host key must not
    stay allocated in `_RateLimiter.buckets` forever once its own bucket
    has fully expired and the limiter has accumulated enough distinct
    hosts to trigger a sweep. See docs/ADVERSARIAL_HARDENING_REPORT.md.
    """

    def test_expired_host_keys_are_evicted_once_threshold_is_crossed(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(rate_limit_requests=1000, rate_limit_window_seconds=1),
        )
        limiter = rate_limit_module._generic_limiter
        assert limiter._SWEEP_THRESHOLD == 512

        fake_now = [1_000_000.0]
        monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: fake_now[0])

        # One request each from more distinct hosts than the sweep
        # threshold -- every one of these buckets is about to be stale.
        for i in range(limiter._SWEEP_THRESHOLD + 1):
            enforce_rate_limit(_fake_request(host=f"10.0.0.{i}"))
        assert len(limiter.buckets) == limiter._SWEEP_THRESHOLD + 1

        # Advance time well past the 1-second window, then make exactly
        # one more request (from a host not counted above) -- crossing
        # the threshold triggers the sweep as a side effect of THIS call.
        fake_now[0] += 10.0
        enforce_rate_limit(_fake_request(host="10.0.0.new"))

        # Every previously-stale host key must be gone; only the one
        # active (just-appended) key from this call remains.
        assert len(limiter.buckets) == 1
        assert "10.0.0.new" in limiter.buckets
        assert "10.0.0.0" not in limiter.buckets

    def test_eviction_never_removes_the_calling_host_own_fresh_entry(self, monkeypatch):
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(rate_limit_requests=1000, rate_limit_window_seconds=1),
        )
        limiter = rate_limit_module._generic_limiter
        fake_now = [2_000_000.0]
        monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: fake_now[0])

        for i in range(limiter._SWEEP_THRESHOLD + 1):
            enforce_rate_limit(_fake_request(host=f"172.16.0.{i}"))
        fake_now[0] += 10.0
        # Same host that triggers the sweep also makes this call --
        # its own just-appended entry must survive the sweep it triggers.
        enforce_rate_limit(_fake_request(host="172.16.0.0"))
        assert "172.16.0.0" in limiter.buckets
        assert len(limiter.buckets) == 1

    def test_below_threshold_no_sweep_occurs_stale_keys_persist(self, monkeypatch):
        """Confirms the fix is bounded/amortized, not a per-call scan --
        below the threshold, a stale key is left alone (matches the
        documented low-priority-given-private-deployment tradeoff in
        docs/PRODUCTION_READINESS_AUDIT.md AUD-005)."""
        monkeypatch.setattr(
            "app.security.rate_limit.get_settings",
            lambda: Settings(rate_limit_requests=1000, rate_limit_window_seconds=1),
        )
        limiter = rate_limit_module._generic_limiter
        fake_now = [3_000_000.0]
        monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: fake_now[0])

        enforce_rate_limit(_fake_request(host="192.168.0.1"))
        fake_now[0] += 10.0
        enforce_rate_limit(_fake_request(host="192.168.0.2"))
        # Only 2 distinct hosts -- far below _SWEEP_THRESHOLD, so no
        # sweep runs and the now-stale first host's key is still present.
        assert "192.168.0.1" in limiter.buckets
        assert len(limiter.buckets) == 2


def test_get_settings_still_cached_singleton():
    # Sanity check that we didn't break the real dependency wiring.
    assert get_settings() is get_settings()
