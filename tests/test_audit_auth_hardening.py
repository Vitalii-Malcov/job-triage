"""Audit SEC-02 (placeholder secrets) and SEC-03 (failed-auth budget lock-out)."""

from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.core.startup_diagnostics import startup_safety_fields
from app.security import auth as auth_module
from app.security import rate_limit as rate_limit_module
from app.security.key_strength import (
    MIN_STRONG_KEY_CHARS,
    is_placeholder_secret,
    is_strong_secret,
    secret_strength,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRONG = "k" * 8 + "Zq3-" * 6 + "end0"  # 36 chars, not a placeholder
assert len(STRONG) >= MIN_STRONG_KEY_CHARS and not is_placeholder_secret(STRONG)


# ---- SEC-02 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["replace-with-a-long-random-secret", "Replace-Me", "changeme", "your-key-here", "<secret>"],
)
@pytest.mark.parametrize("field", ["api_key", "ui_session_secret", "postgres_password"])
def test_template_placeholders_are_refused(field, value):
    with pytest.raises(ValidationError) as excinfo:
        Settings(**{field: value})
    message = str(excinfo.value)
    assert field in message
    assert value not in message  # the value itself is never echoed


@pytest.mark.parametrize("value", ["", STRONG, "k", "test-key", "upstream-key"])
def test_empty_real_and_test_keys_are_accepted(value):
    assert Settings(api_key=value).api_key.get_secret_value() == value


def test_env_templates_ship_empty_secrets():
    for name in (".env.example", ".env.stage10.example"):
        lines = (PROJECT_ROOT / name).read_text(encoding="utf-8").splitlines()
        assert "API_KEY=" in lines, name
        assert not any(line.startswith("API_KEY=") and line != "API_KEY=" for line in lines)
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "POSTGRES_PASSWORD=" in example


def test_the_unmodified_template_values_keep_the_api_closed_not_open(monkeypatch):
    monkeypatch.setattr("app.security.auth.get_settings", lambda: Settings(api_key=""))
    with pytest.raises(HTTPException) as excinfo:
        _call_auth("anything", "198.51.100.1")
    assert excinfo.value.status_code == 401


def test_strength_classes():
    assert secret_strength("") == "unset"
    assert secret_strength("replace-with-x") == "placeholder"
    assert secret_strength("short") == "weak"
    assert secret_strength(STRONG) == "strong"
    assert is_strong_secret(STRONG) and not is_strong_secret("short")


def test_startup_evidence_reports_strength_class_only():
    fields = startup_safety_fields(Settings(api_key=STRONG))
    assert fields["api_key_strength"] == "strong"
    assert STRONG not in " ".join(fields.values())


# ---- SEC-03 -----------------------------------------------------------------


class _Request:
    def __init__(self, ip: str):
        self.client = type("C", (), {"host": ip})()
        self.headers = {}
        self.scope = {"type": "http"}
        self.cookies = {}


def _call_auth(key: str, ip: str) -> None:
    auth_module.require_api_key(_Request(ip), key)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fresh_budget():
    rate_limit_module._auth_failure_requests.clear()
    yield
    rate_limit_module._auth_failure_requests.clear()


def _use_key(monkeypatch, key: str) -> None:
    settings = Settings(api_key=key)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)


def _burn_budget(ip: str) -> None:
    for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as excinfo:
            _call_auth("wrong", ip)
        assert excinfo.value.status_code == 401


def test_strong_correct_key_is_not_locked_out_by_failures_from_the_same_address(monkeypatch):
    _use_key(monkeypatch, STRONG)
    _burn_budget("198.51.100.7")
    _call_auth(STRONG, "198.51.100.7")  # no exception: a valid key always passes


def test_wrong_keys_are_still_throttled_after_the_budget_is_spent(monkeypatch):
    _use_key(monkeypatch, STRONG)
    _burn_budget("198.51.100.7")
    with pytest.raises(HTTPException) as excinfo:
        _call_auth("wrong", "198.51.100.7")
    assert excinfo.value.status_code == 429


def test_other_clients_keep_their_own_budget(monkeypatch):
    _use_key(monkeypatch, STRONG)
    _burn_budget("198.51.100.7")
    with pytest.raises(HTTPException) as excinfo:
        _call_auth("wrong", "203.0.113.9")
    assert excinfo.value.status_code == 401  # a fresh client is not yet limited


def test_valid_strong_key_does_not_consume_budget(monkeypatch):
    _use_key(monkeypatch, STRONG)
    for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT * 3):
        _call_auth(STRONG, "198.51.100.7")
    with pytest.raises(HTTPException) as excinfo:
        _call_auth("wrong", "198.51.100.7")
    assert excinfo.value.status_code == 401  # budget untouched by the valid calls


def test_weak_key_keeps_the_conservative_lockout_before_comparison(monkeypatch):
    _use_key(monkeypatch, "test-key")
    _burn_budget("198.51.100.7")
    with pytest.raises(HTTPException) as excinfo:
        _call_auth("test-key", "198.51.100.7")  # correct, but the key is too weak to trust
    assert excinfo.value.status_code == 429


def test_weak_key_success_refunds_its_slot(monkeypatch):
    _use_key(monkeypatch, "test-key")
    for _ in range(rate_limit_module.AUTH_FAILURE_LIMIT * 2):
        _call_auth("test-key", "198.51.100.7")


def test_same_budget_behind_a_proxy_that_hides_clients(monkeypatch):
    """All clients look like one address (no TRUSTED_PROXY_IPS): attackers burn the
    shared budget, the operator with the strong key still gets in."""
    _use_key(monkeypatch, STRONG)
    _burn_budget("172.18.0.1")
    _call_auth(STRONG, "172.18.0.1")
