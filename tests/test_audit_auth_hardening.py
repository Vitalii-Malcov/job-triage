"""Audit SEC-02 (placeholder secrets) and SEC-03 (failed-auth budget lock-out)."""

from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.core.key_strength import (
    MIN_STRONG_KEY_CHARS,
    is_placeholder_secret,
    is_strong_secret,
    secret_strength,
)
from app.core.startup_diagnostics import startup_safety_fields
from app.security import auth as auth_module
from app.security import rate_limit as rate_limit_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRONG = "Q7xT2mV9bK4nR8sW1cY6dF3gH5jL0pAz"  # 32 mixed characters, not a placeholder
assert len(STRONG) >= MIN_STRONG_KEY_CHARS and not is_placeholder_secret(STRONG)


# ---- SEC-02 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "replace-with-a-long-random-secret",
        "Replace-Me",
        "changeme",
        "your-key-here",
        "<secret>",
        "change_me_please",
    ],
)
@pytest.mark.parametrize("field", ["api_key", "ui_session_secret", "postgres_password"])
def test_template_placeholders_are_refused(field, value):
    with pytest.raises(ValidationError) as excinfo:
        Settings(**{field: value})
    message = str(excinfo.value)
    assert field in message
    assert value not in message  # the value itself is never echoed


@pytest.mark.parametrize(
    "value",
    [
        "",
        STRONG,
        "k",
        "test-key",
        "upstream-key",
        # legitimate random-looking secrets that merely START like a template word
        "todoQ7xT2mV9bK4nR8sW1cY6dF3gH5jL0pA",
        "xxxQ7xT2mV9bK4nR8sW1cY6dF3gH5jL0pAaa",
        "exampleQ7xT2mV9bK4nR8sW1cY6dF3gH5jL0p",
        "replaceQ7xT2mV9bK4nR8sW1cY6dF3gH5jL0p",
        "<Q7xT2mV9bK4nR8sW1cY6dF3gH5jL0pA!x",
    ],
)
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
        self.path_params = {}
        self.query_params = {}


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


def test_low_diversity_value_is_not_strong():
    assert not is_strong_secret("a" * 40)
    assert not is_strong_secret("ab" * 20)


# ---- SEC-03 on the UI paths -------------------------------------------------


@pytest.fixture()
def ui_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from app.db.base import Base
    from app.main import app
    from app.ui.readonly import build_readonly_engine, get_ui_db, readonly_sessionmaker

    url = f"sqlite:///{(tmp_path / 'ui.db').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    ro = build_readonly_engine(url)
    factory = readonly_sessionmaker(ro)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.rollback()
            db.close()

    def make(api_key):
        settings = Settings(
            _env_file=None,
            api_key=api_key,
            telegram_bot_token="",
            ui_enabled=True,
            ui_cookie_secure=True,
            gmail_username="",
        )
        for target in ("app.ui.auth", "app.ui.routes", "app.security.rate_limit"):
            monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
        rate_limit_module._ui_login_limiter.buckets.clear()
        app.dependency_overrides[get_ui_db] = override
        return TestClient(app, base_url="https://testserver")

    yield make
    app.dependency_overrides.pop(get_ui_db, None)
    ro.dispose()


def _wrong_logins(client, count):
    for _ in range(count):
        response = client.post("/ui/login", data={"api_key": "wrong"}, follow_redirects=False)
        assert response.status_code in (401, 429)


def test_ui_strong_key_login_and_session_survive_other_clients_failures(ui_client):
    client = ui_client(STRONG)
    _wrong_logins(client, rate_limit_module.AUTH_FAILURE_LIMIT + 5)  # budget + login quota spent
    blocked = client.post("/ui/login", data={"api_key": "wrong"}, follow_redirects=False)
    assert blocked.status_code == 429
    ok = client.post("/ui/login", data={"api_key": STRONG}, follow_redirects=False)
    assert ok.status_code == 303, ok.text
    page = client.get("/ui/", follow_redirects=False)
    assert page.status_code == 200, page.status_code  # the valid session is not refused either


def test_ui_weak_key_keeps_the_conservative_lockout(ui_client):
    client = ui_client("weak-ui-key")
    _wrong_logins(client, rate_limit_module.AUTH_FAILURE_LIMIT + 5)
    refused = client.post("/ui/login", data={"api_key": "weak-ui-key"}, follow_redirects=False)
    assert refused.status_code == 429


def test_ui_forged_cookies_are_still_charged_and_throttled(ui_client):
    client = ui_client(STRONG)
    client.cookies.set("jobtriage_ui", "forged.cookie.value", path="/ui")
    codes = [client.get("/ui/", follow_redirects=False).status_code for _ in range(14)]
    assert 429 in codes  # guessing a signature burns the shared budget
