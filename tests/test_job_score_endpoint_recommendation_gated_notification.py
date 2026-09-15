"""S11E-001 (Codex Stage 11E review, BLOCKING) regression: POST
/jobs/score's Telegram notification must require BOTH
`result.recommendation == "APPLY"` AND `result.score >=
settings.min_job_score_to_notify` -- not score alone. Stage 11A/11B/11C/
11E can all deliberately leave a job at a HIGH score with
recommendation != "APPLY" (most directly, Stage 11E's own
low-unique-evidence-cardinality downgrade -- see
app.agents.evidence_cardinality_classifier); a score-only condition
would still fire a positive vacancy notification for exactly those
Stage-11E-rejected jobs. Mirrors the notification gate every collector
run already uses (app.services.collector_runner.run_bundesagentur/
run_xing: `result.recommendation == "APPLY" and result.score >=
settings.min_job_score_to_notify`).
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

GENERIC_DESCRIPTION = (
    "We are looking for a great colleague to join our growing team in a "
    "collaborative, supportive environment with excellent benefits. " * 30
)


class _SpyNotifier:
    """Stands in for TelegramNotifier -- records every send_job() call
    so tests can assert a notification did or did NOT occur, rather than
    only checking the HTTP response (which is unaffected by whether a
    notification fired, since it's best-effort orchestration on top of
    an already-committed score+persist).
    """

    calls: list = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def send_job(self, job, score) -> bool:
        _SpyNotifier.calls.append((job, score))
        return True


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _SpyNotifier.calls = []
    db_path = tmp_path / "test_job_score_recommendation_gated_notification.db"
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
    monkeypatch.setattr("app.api.routes.TelegramNotifier", _SpyNotifier)
    rate_limit_module._requests.clear()

    with TestClient(app) as test_client:
        yield test_client

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
        "url": "https://example.com/jobs/s11e-001-test",
        "description": GENERIC_DESCRIPTION,
        "must_have_skills": [],
        "nice_to_have_skills": [],
    }
    data.update(overrides)
    return data


def test_1_created_high_score_skip_sends_no_notification(client):
    # Solution-Architect-style: a single unique evidence signal
    # (must_have=["python"], no nice-to-have) gives a high raw score via
    # JobScorer's own full single-match credit, but Stage 11E's
    # low-unique-evidence-cardinality guard downgrades it to SKIP.
    # "Solution Architect" is deliberately NOT a Stage 11B RELEVANT title
    # (no "developer"/"engineer"/"entwickler" phrase), so Stage 11C's own
    # rescue never re-raises it back to MAYBE.
    response = client.post(
        "/api/v1/jobs/score",
        json=_job_payload(
            title="Solution Architect (m/w/d)",
            must_have_skills=["Python"],
            nice_to_have_skills=[],
        ),
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "SKIP"
    assert body["score"] > 0  # high score, but recommendation is SKIP
    assert _SpyNotifier.calls == []


def test_2_created_high_score_maybe_sends_no_notification(client):
    # Two DISTINCT unique evidence signals (must=python, nice=fastapi,
    # both matched) -- Stage 11E does NOT downgrade this (evidence
    # cardinality is SUFFICIENT), so it stays a genuine MAYBE via the
    # existing Stage 10 sparse-must-have APPLY cap. MAYBE must still
    # never notify.
    response = client.post(
        "/api/v1/jobs/score",
        json=_job_payload(
            url="https://example.com/jobs/s11e-001-test-maybe",
            must_have_skills=["Python"],
            nice_to_have_skills=["FastAPI"],
        ),
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "MAYBE"
    assert body["score"] > 0
    assert _SpyNotifier.calls == []


def test_3_created_high_score_apply_still_notifies(client):
    response = client.post(
        "/api/v1/jobs/score",
        json=_job_payload(
            url="https://example.com/jobs/s11e-001-test-apply",
            must_have_skills=["Python", "FastAPI", "Flask"],
            nice_to_have_skills=[],
        ),
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "APPLY"
    assert len(_SpyNotifier.calls) == 1


def test_4_duplicate_submission_apply_does_not_notify_again(client):
    payload = _job_payload(
        url="https://example.com/jobs/s11e-001-test-duplicate",
        must_have_skills=["Python", "FastAPI", "Flask"],
        nice_to_have_skills=[],
    )

    first = client.post("/api/v1/jobs/score", json=payload, headers=_auth_headers())
    assert first.status_code == 200
    assert first.json()["recommendation"] == "APPLY"
    assert len(_SpyNotifier.calls) == 1

    second = client.post("/api/v1/jobs/score", json=payload, headers=_auth_headers())
    assert second.status_code == 200
    assert second.json()["recommendation"] == "APPLY"
    # created=False on the second (duplicate fingerprint) submission --
    # notification count must remain exactly 1, not 2.
    assert len(_SpyNotifier.calls) == 1
