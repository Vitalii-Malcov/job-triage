"""Stage 64: the documented API-key rotation works on an isolated configuration.

Nothing here reads `.env`, the production database or any real secret: both keys are generated
per test. It proves what `docs/STAGE63_API_KEY_ROTATION_PLAN.md` step 7 expects after the swap:
the old key is refused (API and UI session), the new key is accepted, the diagnostic reports
`strong`, and neither value reaches a log line.
"""

import logging
import secrets

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.key_strength import secret_strength
from app.security import auth as api_auth
from app.security import rate_limit
from app.ui import auth as ui_auth

OLD = "Old-Weak-Key-1234567"  # 20 characters: below the 32-character floor
assert len(OLD) < 32


def _app():
    app = FastAPI()

    @app.get("/probe", dependencies=[Depends(api_auth.require_api_key)])
    def probe():
        return {"ok": True}

    return app


@pytest.fixture
def rotate(monkeypatch):
    state = {}

    def use(key):
        settings = Settings(_env_file=None, api_key=key)
        state["settings"] = settings
        for target in ("app.security.auth", "app.security.rate_limit"):
            monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
        return settings

    rate_limit._auth_failure_requests.clear()
    return use


def test_old_key_is_weak_and_a_generated_key_is_strong():
    new = secrets.token_urlsafe(32)
    assert secret_strength(OLD) == "weak"
    assert secret_strength(new) == "strong"
    assert len(new) >= 32


def test_after_rotation_the_old_key_is_refused_and_the_new_one_accepted(rotate, caplog):
    new = secrets.token_urlsafe(32)
    client = TestClient(_app(), base_url="http://localhost")
    caplog.set_level(logging.DEBUG)

    rotate(OLD)
    assert client.get("/probe", headers={"X-API-Key": OLD}).status_code == 200

    rotate(new)  # the process restarted with the new API_KEY
    rate_limit._auth_failure_requests.clear()
    assert client.get("/probe", headers={"X-API-Key": OLD}).status_code == 401
    rate_limit._auth_failure_requests.clear()
    assert client.get("/probe", headers={"X-API-Key": new}).status_code == 200
    assert client.get("/probe").status_code == 401  # no key at all

    assert OLD not in caplog.text and new not in caplog.text


def test_a_ui_session_issued_for_the_old_key_does_not_survive_rotation():
    new = secrets.token_urlsafe(32)
    token = ui_auth.issue_session_token(OLD, hours=1)
    assert ui_auth.session_token_valid(token, OLD)
    assert not ui_auth.session_token_valid(token, new)  # everybody is signed out: expected
    assert ui_auth.session_token_valid(ui_auth.issue_session_token(new, hours=1), new)


def test_rotation_back_to_the_previous_value_restores_access_rollback(rotate):
    new = secrets.token_urlsafe(32)
    client = TestClient(_app(), base_url="http://localhost")
    rotate(new)
    assert client.get("/probe", headers={"X-API-Key": new}).status_code == 200
    rotate(OLD)  # rollback: the previous line is put back into .env
    rate_limit._auth_failure_requests.clear()
    assert client.get("/probe", headers={"X-API-Key": OLD}).status_code == 200
    rate_limit._auth_failure_requests.clear()
    assert client.get("/probe", headers={"X-API-Key": new}).status_code == 401
