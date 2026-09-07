"""API tests for Stage 7E's follow-up endpoints: POST /follow-ups/evaluate,
POST /jobs/{id}/follow-up/evaluate, GET /follow-ups, GET /follow-ups/{id},
POST /follow-ups/{id}/decision, POST /follow-ups/{id}/send, and
GET /follow-ups/{id}/state.

Covers the golden path, the "NO APPROVAL = NO FOLLOW-UP SEND" gate over
HTTP, cross-account isolation, and auth/rate-limit wiring —
complementing the service-level (white-box) coverage in
tests/test_follow_up_service.py and tests/test_follow_up_send_service.py.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.gmail_repository import upsert_message
from app.db.models import GmailMessageAnalysisRecord, JobRecord
from app.db.session import get_db
from app.main import app
from app.providers.email.base import ParsedGmailMessage
from app.providers.email.outbound_base import OutboundSendResult
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"
ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"


class FakeOutboundProvider:
    def __init__(self):
        self.sent_messages: list = []
        self.call_count = 0

    def send(self, message):
        self.call_count += 1
        self.sent_messages.append(message)
        return OutboundSendResult(provider_message_id="msg-1")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_follow_up_endpoints.db"
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
        gmail_username=ACCOUNT,
        gmail_app_password="app-password",
        follow_up_delay_days=1,
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._follow_up_evaluate_requests.clear()
    rate_limit_module._follow_up_decision_requests.clear()
    rate_limit_module._follow_up_send_requests.clear()

    fake_provider = FakeOutboundProvider()
    monkeypatch.setattr("app.api.routes.GmailSmtpProvider", lambda **_kwargs: fake_provider)

    with TestClient(app) as test_client:
        yield test_client, session_factory, fake_provider

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._follow_up_evaluate_requests.clear()
    rate_limit_module._follow_up_decision_requests.clear()
    rate_limit_module._follow_up_send_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _seed_eligible_job(session_factory, *, account_key: str = ACCOUNT, uid: int = 1) -> int:
    """Seeds a job whose only outbound correspondence is already well past
    the (1-day) configured follow-up delay, with no later reply — always
    ELIGIBLE regardless of wall-clock time the test happens to run at.
    """
    db = session_factory()
    try:
        job = JobRecord(
            fingerprint=f"fp-{uid}-{account_key}",
            source="bundesagentur",
            title="Backend Engineer",
            company="Globex",
            location="Berlin",
            url="https://example.com/jobs/1",
            description="",
            score=80,
            recommendation="APPLY",
            status="APPLIED",
        )
        db.add(job)
        db.commit()

        outbound, _created = upsert_message(
            db,
            ParsedGmailMessage(
                account_key=account_key,
                mailbox="INBOX",
                uid=uid,
                uid_validity=100,
                message_id_header=f"<out{uid}@example.com>",
                in_reply_to=None,
                references=(),
                from_address=account_key,
                from_display_name=None,
                to_addresses=("hr@acme.example.com",),
                cc_addresses=(),
                subject="My application at Globex",
                sent_at=datetime.now(UTC) - timedelta(days=30),
                direction="OUTBOUND",
                body_plain="I am applying for the Backend Engineer role at Globex.",
                body_truncated=False,
                has_html=False,
                attachments=(),
            ),
        )
        # S7E-004 (Codex remediation): eligibility now orders by the
        # trusted `received_at` (real sync write time), never the
        # sender-controlled `sent_at` RFC Date header — see
        # app.db.follow_up_repository.get_thread_message_infos. Set
        # directly so this fixture stays "always ELIGIBLE regardless of
        # wall-clock time the test happens to run at" as documented above.
        outbound.received_at = datetime.now(UTC) - timedelta(days=30)
        db.commit()
        db.add(
            GmailMessageAnalysisRecord(
                account_key=account_key,
                gmail_message_id=outbound.id,
                analysis_version=1,
                input_fingerprint="fp",
                context_fingerprint="ctx",
                match_type="APPLICATION",
                matched_job_id=job.id,
                match_confidence="HIGH",
                match_score=90,
                classification="OTHER",
                classification_confidence="HIGH",
                is_automated=False,
                requires_human_review=True,
            )
        )
        db.commit()
        return job.id
    finally:
        db.close()


class TestEvaluateEndpoints:
    def test_bulk_evaluate_creates_a_proposal(self, client):
        test_client, session_factory, _provider = client
        _seed_eligible_job(session_factory)

        response = test_client.post("/api/v1/follow-ups/evaluate", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["scanned"] == 1
        assert body["eligible"] == 1
        assert body["proposals_created"] == 1
        assert body["results"][0]["proposal"]["status"] == "PROPOSED"

    def test_bulk_evaluate_is_idempotent(self, client):
        test_client, session_factory, _provider = client
        _seed_eligible_job(session_factory)

        test_client.post("/api/v1/follow-ups/evaluate", headers=_auth_headers())
        second = test_client.post("/api/v1/follow-ups/evaluate", headers=_auth_headers())

        assert second.json()["proposals_created"] == 0

    def test_single_job_evaluate_unknown_job_is_404(self, client):
        test_client, _session_factory, _provider = client
        response = test_client.post("/api/v1/jobs/999/follow-up/evaluate", headers=_auth_headers())
        assert response.status_code == 404

    def test_evaluate_requires_auth(self, client):
        test_client, _session_factory, _provider = client
        response = test_client.post("/api/v1/follow-ups/evaluate")
        assert response.status_code in (401, 403)

    def test_after_job_id_cursor_resumes_a_paginated_scan(self, client):
        """S7E-006 (Codex remediation): the response's `next_cursor` can
        be passed back as `after_job_id` to reach a job a smaller-limit
        scan didn't cover — never stuck rescanning the same jobs."""
        test_client, session_factory, _provider = client
        job_one = _seed_eligible_job(session_factory, uid=1)
        job_two = _seed_eligible_job(session_factory, uid=2)
        assert job_two > job_one

        first = test_client.post(
            "/api/v1/follow-ups/evaluate", headers=_auth_headers(), params={"after_job_id": job_one}
        )
        assert first.status_code == 200
        body = first.json()
        assert body["scanned"] == 1
        assert body["results"][0]["job_id"] == job_two


class TestListAndGet:
    def test_list_and_get_after_evaluate(self, client):
        test_client, session_factory, _provider = client
        _seed_eligible_job(session_factory)
        test_client.post("/api/v1/follow-ups/evaluate", headers=_auth_headers())

        listed = test_client.get("/api/v1/follow-ups", headers=_auth_headers())
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        follow_up_id = listed.json()[0]["id"]

        detail = test_client.get(f"/api/v1/follow-ups/{follow_up_id}", headers=_auth_headers())
        assert detail.status_code == 200
        assert detail.json()["id"] == follow_up_id

    def test_get_unknown_follow_up_is_404(self, client):
        test_client, _session_factory, _provider = client
        response = test_client.get("/api/v1/follow-ups/999", headers=_auth_headers())
        assert response.status_code == 404


class TestApprovalGate:
    def _create_proposal(self, test_client, session_factory) -> int:
        _seed_eligible_job(session_factory)
        test_client.post("/api/v1/follow-ups/evaluate", headers=_auth_headers())
        listed = test_client.get("/api/v1/follow-ups", headers=_auth_headers())
        return listed.json()[0]["id"]

    def test_send_without_approval_is_forbidden(self, client):
        test_client, session_factory, provider = client
        follow_up_id = self._create_proposal(test_client, session_factory)

        response = test_client.post(
            f"/api/v1/follow-ups/{follow_up_id}/send", headers=_auth_headers()
        )

        assert response.status_code == 403
        assert provider.call_count == 0

    def test_approve_then_send_succeeds(self, client):
        test_client, session_factory, provider = client
        follow_up_id = self._create_proposal(test_client, session_factory)

        proposal = test_client.get(
            f"/api/v1/follow-ups/{follow_up_id}", headers=_auth_headers()
        ).json()
        # S7E-008: the exact recipient must be visible on the proposal
        # itself, before any approval decision is made.
        assert proposal["recipient"] == "hr@acme.example.com"

        decision = test_client.post(
            f"/api/v1/follow-ups/{follow_up_id}/decision",
            headers=_auth_headers(),
            json={"decision": "APPROVED"},
        )
        assert decision.status_code == 200
        assert decision.json()["decision"] == "APPROVED"
        # S7E-008: pinned and visible in the human approval artifact
        # itself, alongside pinned_subject/pinned_body.
        assert decision.json()["pinned_recipient"] == "hr@acme.example.com"

        send = test_client.post(f"/api/v1/follow-ups/{follow_up_id}/send", headers=_auth_headers())
        assert send.status_code == 200
        assert send.json()["status"] == "SENT"
        assert provider.call_count == 1
        assert provider.sent_messages[0].to_address == "hr@acme.example.com"

        state = test_client.get(f"/api/v1/follow-ups/{follow_up_id}/state", headers=_auth_headers())
        assert state.status_code == 200
        assert state.json()["send"]["status"] == "SENT"

    def test_second_decision_is_conflict(self, client):
        test_client, session_factory, _provider = client
        follow_up_id = self._create_proposal(test_client, session_factory)

        test_client.post(
            f"/api/v1/follow-ups/{follow_up_id}/decision",
            headers=_auth_headers(),
            json={"decision": "APPROVED"},
        )
        second = test_client.post(
            f"/api/v1/follow-ups/{follow_up_id}/decision",
            headers=_auth_headers(),
            json={"decision": "REJECTED"},
        )
        assert second.status_code == 409

    def test_send_twice_is_conflict(self, client):
        test_client, session_factory, _provider = client
        follow_up_id = self._create_proposal(test_client, session_factory)
        test_client.post(
            f"/api/v1/follow-ups/{follow_up_id}/decision",
            headers=_auth_headers(),
            json={"decision": "APPROVED"},
        )
        test_client.post(f"/api/v1/follow-ups/{follow_up_id}/send", headers=_auth_headers())

        second_send = test_client.post(
            f"/api/v1/follow-ups/{follow_up_id}/send", headers=_auth_headers()
        )
        assert second_send.status_code == 409


class TestCrossAccountIsolation:
    def test_follow_up_created_for_other_account_is_invisible(self, client, monkeypatch):
        test_client, session_factory, _provider = client
        _seed_eligible_job(session_factory, account_key=OTHER_ACCOUNT)

        response = test_client.post("/api/v1/follow-ups/evaluate", headers=_auth_headers())

        # Configured account is ACCOUNT, so a job whose only correspondence
        # lives under OTHER_ACCOUNT contributes no matched thread at all.
        assert response.status_code == 200
        assert response.json()["proposals_created"] == 0
        listed = test_client.get("/api/v1/follow-ups", headers=_auth_headers())
        assert listed.json() == []
