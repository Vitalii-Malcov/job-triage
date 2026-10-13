"""Stage 13A-2: trusted-proxy client identity for rate limiting
(app.security.client_identity) -- explicit one-hop trust, fail closed.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.core.config import Settings
from app.security import rate_limit as rate_limit_module
from app.security.client_identity import resolve_client_identity
from app.security.rate_limit import enforce_rate_limit

PROXY_V4 = "172.17.0.1"
PROXY_V6 = "fd00::1"
TRUSTED = frozenset({PROXY_V4, PROXY_V6})


def _request(peer: str | None, headers: list[tuple[str, str]] = ()) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": (peer, 50000) if peer is not None else None,
    }
    return Request(scope)


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None, rate_limit_requests=1, rate_limit_window_seconds=60, **overrides
    )


class TestUntrustedPeer:
    @pytest.mark.parametrize(
        "header",
        [
            ("X-Real-IP", "203.0.113.9"),
            ("X-Forwarded-For", "203.0.113.9"),
            ("Forwarded", "for=203.0.113.9"),
        ],
    )
    def test_forwarding_headers_are_ignored(self, header):
        request = _request("198.51.100.7", [header])
        assert resolve_client_identity(request, TRUSTED) == "198.51.100.7"

    def test_default_empty_trust_never_believes_a_header(self):
        request = _request(PROXY_V4, [("X-Real-IP", "203.0.113.9")])
        assert resolve_client_identity(request, _settings().trusted_proxy_ip_set) == PROXY_V4

    def test_missing_client_is_unknown(self):
        assert resolve_client_identity(_request(None), TRUSTED) == "unknown"

    def test_non_ip_peer_is_kept_verbatim(self):
        assert resolve_client_identity(_request("testclient"), TRUSTED) == "testclient"


class TestTrustedPeer:
    @pytest.mark.parametrize(
        ("peer", "forwarded", "expected"),
        [
            (PROXY_V4, "203.0.113.9", "203.0.113.9"),
            (PROXY_V4, "2001:db8::5", "2001:db8::5"),
            (PROXY_V6, "203.0.113.9", "203.0.113.9"),
            (PROXY_V4, "2001:DB8:0:0::5", "2001:db8::5"),  # canonicalized
            (PROXY_V4, "::ffff:203.0.113.9", "203.0.113.9"),  # IPv4-mapped folded
            ("::ffff:172.17.0.1", "203.0.113.9", "203.0.113.9"),  # mapped peer matches
        ],
    )
    def test_single_valid_ip_is_used(self, peer, forwarded, expected):
        request = _request(peer, [("X-Real-IP", forwarded)])
        assert resolve_client_identity(request, TRUSTED) == expected

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param([], id="missing"),
            pytest.param([("X-Real-IP", "")], id="empty"),
            pytest.param([("X-Real-IP", "not-an-ip")], id="malformed"),
            pytest.param([("X-Real-IP", "203.0.113.9, 198.51.100.1")], id="comma-list"),
            pytest.param([("X-Real-IP", "203.0.113.9 198.51.100.1")], id="space-list"),
            pytest.param([("X-Real-IP", " 203.0.113.9")], id="padded"),
            pytest.param([("X-Real-IP", "203.0.113.9:443")], id="with-port"),
            pytest.param([("X-Real-IP", "[2001:db8::5]")], id="bracketed-v6"),
            pytest.param([("X-Real-IP", "fe80::1%eth0")], id="v6-zone"),
            pytest.param([("X-Real-IP", "203.0.113.0/24")], id="cidr"),
            pytest.param(
                [("X-Real-IP", "203.0.113.9"), ("X-Real-IP", "198.51.100.1")], id="repeated"
            ),
            pytest.param([("X-Forwarded-For", "203.0.113.9")], id="other-header-only"),
        ],
    )
    def test_anything_but_one_bare_ip_falls_back_to_the_peer(self, headers):
        assert resolve_client_identity(_request(PROXY_V4, headers), TRUSTED) == PROXY_V4


class TestTrustedProxySetting:
    def test_default_is_empty(self):
        assert _settings().trusted_proxy_ip_set == frozenset()

    def test_entries_are_normalized_and_deduplicated(self):
        settings = _settings(trusted_proxy_ips=" 172.17.0.1, FD00::1 ,172.17.0.1,")
        assert settings.trusted_proxy_ips == "172.17.0.1,fd00::1"
        assert settings.trusted_proxy_ip_set == TRUSTED

    @pytest.mark.parametrize(
        "value", ["172.17.0.0/16", "localhost", "fe80::1%eth0", "1.2.3.4 5.6.7.8", "*"]
    )
    def test_anything_but_literal_ips_is_rejected(self, value):
        with pytest.raises(ValidationError):
            _settings(trusted_proxy_ips=value)


class TestRateLimitBucketsUseTheResolvedIdentity:
    @pytest.fixture(autouse=True)
    def _clean_buckets(self):
        rate_limit_module._requests.clear()
        yield
        rate_limit_module._requests.clear()

    def _use(self, monkeypatch, settings: Settings) -> None:
        monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)

    def test_clients_behind_a_trusted_proxy_get_separate_buckets(self, monkeypatch):
        self._use(monkeypatch, _settings(trusted_proxy_ips=PROXY_V4))
        enforce_rate_limit(_request(PROXY_V4, [("X-Real-IP", "203.0.113.9")]))
        enforce_rate_limit(_request(PROXY_V4, [("X-Real-IP", "203.0.113.10")]))
        assert set(rate_limit_module._requests) == {"203.0.113.9", "203.0.113.10"}
        with pytest.raises(HTTPException) as exc:
            enforce_rate_limit(_request(PROXY_V4, [("X-Real-IP", "203.0.113.9")]))
        assert exc.value.status_code == 429

    def test_untrusted_peer_cannot_escape_its_bucket_by_spoofing(self, monkeypatch):
        self._use(monkeypatch, _settings(trusted_proxy_ips=PROXY_V4))
        enforce_rate_limit(_request("198.51.100.7", [("X-Real-IP", "203.0.113.1")]))
        with pytest.raises(HTTPException):
            enforce_rate_limit(_request("198.51.100.7", [("X-Real-IP", "203.0.113.2")]))
        assert set(rate_limit_module._requests) == {"198.51.100.7"}

    def test_default_config_keys_on_the_peer_even_with_headers(self, monkeypatch):
        self._use(monkeypatch, _settings())
        enforce_rate_limit(_request(PROXY_V4, [("X-Real-IP", "203.0.113.9")]))
        assert set(rate_limit_module._requests) == {PROXY_V4}

    def test_malformed_header_from_trusted_proxy_shares_the_proxy_bucket(self, monkeypatch):
        self._use(monkeypatch, _settings(trusted_proxy_ips=PROXY_V4))
        enforce_rate_limit(_request(PROXY_V4, [("X-Real-IP", "203.0.113.9, 1.1.1.1")]))
        assert set(rate_limit_module._requests) == {PROXY_V4}
