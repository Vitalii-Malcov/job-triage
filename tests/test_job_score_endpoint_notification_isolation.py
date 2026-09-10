"""Codex gate follow-up (Astra R4B, NEW-005: Telegram isolation)
regression: POST /jobs/score's `score_and_persist` call durably commits
the scored job FIRST -- the subsequent `TelegramNotifier.send_job` call
is best-effort orchestration on top of that already-successful write and
must never turn it into an HTTP failure, exactly like every collector
run's own send_job() call (app.services.collector_runner.run_xing/
run_bundesagentur). Before this fix, `score_job` was the one send_job()
call site not wrapped in try/except -- an exception from it propagated
uncaught past the endpoint, discarding the JobScore the caller would
otherwise have received for a job that was, in fact, already persisted.
"""

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
SECRET_TEXT = "secret-telegram-upstream-detail-must-not-leak"


class _RaisingNotifier:
    """Stands in for TelegramNotifier -- send_job always raises, modeling
    a bug/edge case in the notifier that isn't caught by its own internal
    httpx-error handling (app.services.telegram.send_telegram_text only
    classifies httpx.HTTPError subtypes; this proves the endpoint fails
    closed regardless of that internal contract, not merely trusts it).
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def send_job(self, job, score) -> bool:
        raise RuntimeError(SECRET_TEXT)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_job_score_endpoint_notification_isolation.db"
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
        api_key=API_KEY,
        rate_limit_requests=1000,
        rate_limit_window_seconds=60,
        min_job_score_to_notify=0,
        telegram_bot_token="fake-token",
        telegram_chat_id="fake-chat",
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _job_payload(**overrides) -> dict:
    data = {
        "source": "xing",
        "title": "Python Developer",
        "company": "Example GmbH",
        "location": "Berlin",
        "url": "https://example.com/jobs/notification-isolation",
        "description": "We build APIs with Python and FastAPI.",
        "skills": ["python", "fastapi"],
    }
    data.update(overrides)
    return data


def test_notification_exception_does_not_turn_successful_score_into_500(
    client, monkeypatch, caplog
):
    test_client, session_factory = client
    monkeypatch.setattr("app.api.routes.TelegramNotifier", _RaisingNotifier)

    with caplog.at_level("DEBUG"):
        response = test_client.post(
            "/api/v1/jobs/score", json=_job_payload(), headers=_auth_headers()
        )

    # The job scoring/persistence already durably succeeded before the
    # notifier was ever called -- an exception from send_job() must not
    # discard that success and turn it into a 500.
    assert response.status_code == 200
    body = response.json()
    assert "recommendation" in body
    assert "score" in body

    db = session_factory()
    try:
        persisted = db.scalars(
            select(JobRecord).where(
                JobRecord.url == "https://example.com/jobs/notification-isolation"
            )
        ).all()
        assert len(persisted) == 1
    finally:
        db.close()

    assert SECRET_TEXT not in caplog.text
    assert SECRET_TEXT not in response.text
    assert "RuntimeError" in caplog.text


def test_notification_failure_returned_false_is_logged_without_raising(client, monkeypatch):
    """Companion case: send_job() returning False (no exception) --
    e.g. the notifier is disabled/misconfigured -- must also stay
    best-effort and never affect the response.
    """
    test_client, _session_factory = client

    class _FalseNotifier:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def send_job(self, job, score) -> bool:
            return False

    monkeypatch.setattr("app.api.routes.TelegramNotifier", _FalseNotifier)

    response = test_client.post(
        "/api/v1/jobs/score",
        json=_job_payload(url="https://example.com/jobs/notification-isolation-false"),
        headers=_auth_headers(),
    )

    assert response.status_code == 200
