"""Stage 12 API tests: GET /response-drafts/pending, the 409 mappings for
superseded revisions / an already-replied message / an already-followed-up
anchor, `is_current_revision` on GET /response-drafts/{id}/state, and strict
decision bodies (`extra="forbid"`).

The outbound provider is an in-memory fake injected in place of
GmailSmtpProvider -- no SMTP anywhere.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.gmail_repository import upsert_message
from app.db.models import CandidateProfileRecord, JobRecord, ResponseDraftApprovalRecord
from app.db.session import get_db
from app.main import app
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.providers.email.base import ParsedGmailMessage
from app.providers.email.outbound_base import OutboundSendResult
from app.security import rate_limit as rate_limit_module
from app.services.gmail_message_analysis import analyze_gmail_message
from app.services.response_draft import generate_response_draft_for_message

API_KEY = "test-api-key"
ACCOUNT = "me@example.com"
HEADERS = {"X-API-Key": API_KEY}


class FakeOutboundProvider:
    def __init__(self):
        self.call_count = 0

    def send(self, message):
        self.call_count += 1
        return OutboundSendResult(provider_message_id=f"msg-{self.call_count}")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow_endpoints.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    settings = Settings(
        api_key=API_KEY,
        rate_limit_requests=1000,
        rate_limit_window_seconds=60,
        gmail_username=ACCOUNT,
        gmail_app_password="app-password",
        outbound_sending_enabled=True,
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: settings)
    for bucket in (
        rate_limit_module._requests,
        rate_limit_module._gmail_analysis_requests,
        rate_limit_module._response_draft_requests,
        rate_limit_module._response_draft_decision_requests,
        rate_limit_module._response_draft_send_requests,
    ):
        bucket.clear()
    provider = FakeOutboundProvider()
    monkeypatch.setattr("app.api.routes.GmailSmtpProvider", lambda **_kwargs: provider)
    with TestClient(app) as test_client:
        yield test_client, session_factory, provider
    app.dependency_overrides.clear()


def _seed(session_factory, *, uid: int = 1) -> int:
    with session_factory() as db:
        if db.scalar(select(func.count()).select_from(JobRecord)) == 0:
            db.add(
                JobRecord(
                    fingerprint="fp-1",
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
            )
            db.commit()
        record, _ = upsert_message(
            db,
            ParsedGmailMessage(
                account_key=ACCOUNT,
                mailbox="INBOX",
                uid=uid,
                uid_validity=100,
                message_id_header=f"<{uid}@acme.example.com>",
                in_reply_to=None,
                references=(),
                from_address="hr@acme.example.com",
                from_display_name="Recruiter",
                to_addresses=(ACCOUNT,),
                cc_addresses=(),
                subject="Offer",
                sent_at=datetime.now(UTC),
                direction="INBOUND",
                body_plain=(
                    "We are pleased to offer you the position of Backend Engineer at Globex."
                ),
                body_truncated=False,
                has_html=False,
                attachments=(),
            ),
        )
        db.commit()
        return record.id


def _generate(session_factory, msg_id: int) -> int:
    with session_factory() as db:
        analyze_gmail_message(db, ACCOUNT, msg_id)
        record, _ = generate_response_draft_for_message(db, ACCOUNT, msg_id)
        return record.id


def _bump_profile(session_factory) -> None:
    with session_factory() as db:
        if db.get(CandidateProfileRecord, 1) is None:
            db.add(CandidateProfileRecord(id=1, profile_version=1))
            db.commit()
        version = db.get(CandidateProfileRecord, 1).profile_version
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=version, first_name="Jane", last_name=f"Doe{version}"
            ),
        )


def _decide(test_client, draft_id: int, decision: str = "APPROVED", **extra):
    return test_client.post(
        f"/api/v1/response-drafts/{draft_id}/decision",
        json={"decision": decision, **extra},
        headers=HEADERS,
    )


class TestPendingEndpoint:
    def test_requires_api_key(self, client):
        test_client, _sf, _p = client
        assert test_client.get("/api/v1/response-drafts/pending").status_code == 401

    def test_lists_pending_draft_without_content(self, client):
        test_client, session_factory, _p = client
        draft_id = _generate(session_factory, _seed(session_factory))
        response = test_client.get("/api/v1/response-drafts/pending", headers=HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert [item["response_draft_id"] for item in body] == [draft_id]
        assert body[0]["is_current_revision"] is True
        assert "body" not in body[0] and "subject" not in body[0]

    @pytest.mark.parametrize(
        "query", ["limit=0", "limit=101", "offset=-1", "limit=abc", "offset=xyz"]
    )
    def test_rejects_invalid_pagination(self, client, query):
        test_client, _sf, _p = client
        response = test_client.get(f"/api/v1/response-drafts/pending?{query}", headers=HEADERS)
        assert response.status_code == 422

    def test_get_never_generates_or_constructs_a_provider(self, client, monkeypatch):
        test_client, session_factory, provider = client
        _generate(session_factory, _seed(session_factory))

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("a read endpoint must not call this")

        monkeypatch.setattr("app.services.response_draft.generate_response_draft", _forbidden)
        monkeypatch.setattr("app.api.routes.GmailSmtpProvider", _forbidden)
        response = test_client.get("/api/v1/response-drafts/pending", headers=HEADERS)
        assert response.status_code == 200
        assert provider.call_count == 0


class TestDecisionEndpoint:
    def test_extra_body_field_is_rejected(self, client):
        test_client, session_factory, _p = client
        draft_id = _generate(session_factory, _seed(session_factory))
        response = _decide(test_client, draft_id, version=2)
        assert response.status_code == 422
        with session_factory() as db:
            assert db.scalar(select(func.count()).select_from(ResponseDraftApprovalRecord)) == 0

    def test_malformed_decision_is_rejected(self, client):
        test_client, session_factory, _p = client
        draft_id = _generate(session_factory, _seed(session_factory))
        assert _decide(test_client, draft_id, decision="MAYBE").status_code == 422
        response = test_client.post(
            f"/api/v1/response-drafts/{draft_id}/decision", content="not json", headers=HEADERS
        )
        assert response.status_code == 422

    def test_nonexistent_and_invalid_ids(self, client):
        test_client, _sf, _p = client
        assert _decide(test_client, 999).status_code == 404
        response = test_client.post(
            "/api/v1/response-drafts/abc/decision", json={"decision": "APPROVED"}, headers=HEADERS
        )
        assert response.status_code == 422

    def test_superseded_revision_approval_is_409_and_reject_is_allowed(self, client):
        test_client, session_factory, _p = client
        msg_id = _seed(session_factory)
        old_id = _generate(session_factory, msg_id)
        _bump_profile(session_factory)
        new_id = _generate(session_factory, msg_id)

        response = _decide(test_client, old_id)
        assert response.status_code == 409
        assert response.json()["detail"] == "Response draft is superseded by a newer revision"
        assert _decide(test_client, old_id, decision="REJECTED").status_code == 200
        assert _decide(test_client, new_id).status_code == 200

    def test_state_reports_currency(self, client):
        test_client, session_factory, _p = client
        msg_id = _seed(session_factory)
        old_id = _generate(session_factory, msg_id)
        _bump_profile(session_factory)
        new_id = _generate(session_factory, msg_id)
        old_state = test_client.get(
            f"/api/v1/response-drafts/{old_id}/state", headers=HEADERS
        ).json()
        new_state = test_client.get(
            f"/api/v1/response-drafts/{new_id}/state", headers=HEADERS
        ).json()
        assert old_state["is_current_revision"] is False
        assert new_state["is_current_revision"] is True


class TestOneReplyPerMessageOverHttp:
    def test_second_revision_send_and_approval_are_409(self, client):
        test_client, session_factory, provider = client
        msg_id = _seed(session_factory)
        old_id = _generate(session_factory, msg_id)
        assert _decide(test_client, old_id).status_code == 200
        _bump_profile(session_factory)
        new_id = _generate(session_factory, msg_id)
        assert _decide(test_client, new_id).status_code == 200

        first = test_client.post(f"/api/v1/response-drafts/{old_id}/send", headers=HEADERS)
        assert first.status_code == 200
        assert first.json()["status"] == "SENT"

        second = test_client.post(f"/api/v1/response-drafts/{new_id}/send", headers=HEADERS)
        assert second.status_code == 409
        assert "Another revision" in second.json()["detail"]
        assert provider.call_count == 1

        # The pending queue no longer offers anything for this message.
        assert test_client.get("/api/v1/response-drafts/pending", headers=HEADERS).json() == []


class TestFollowUpDecisionBody:
    def test_extra_body_field_is_rejected_before_any_lookup(self, client):
        test_client, _sf, _p = client
        response = test_client.post(
            "/api/v1/follow-ups/1/decision",
            json={"decision": "APPROVED", "proposal_version": 3},
            headers=HEADERS,
        )
        assert response.status_code == 422
