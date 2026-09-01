"""API + lifecycle-safety + privacy + performance tests for Stage 7B's
POST /gmail/messages/{id}/analyze, GET /gmail/messages/{id}/analysis,
GET /gmail/analyses.
"""

import inspect
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.agents.email_classifier as email_classifier_module
import app.db.gmail_analysis_repository as gmail_analysis_repository_module
import app.services.email_matching as email_matching_module
import app.services.gmail_message_analysis as gmail_message_analysis_module
from app.core.config import Settings
from app.db.base import Base
from app.db.gmail_repository import upsert_message
from app.db.models import JobRecord
from app.db.session import get_db
from app.main import app
from app.providers.email.base import ParsedGmailMessage
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"
ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"


def _parsed(
    uid: int, message_id: str | None = None, account_key: str = ACCOUNT, **overrides
) -> ParsedGmailMessage:
    data = dict(
        account_key=account_key,
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=message_id or f"<{uid}@example.com>",
        in_reply_to=None,
        references=(),
        from_address="hr@acme.example.com",
        from_display_name="Recruiter",
        to_addresses=(account_key,),
        cc_addresses=(),
        subject=f"Subject {uid}",
        sent_at=datetime.now(UTC),
        direction="INBOUND",
        body_plain="Vielen Dank für Ihre Bewerbung als Python Developer.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    data.update(overrides)
    return ParsedGmailMessage(**data)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_gmail_analysis_endpoints.db"
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
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._gmail_requests.clear()
    rate_limit_module._gmail_analysis_requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._gmail_requests.clear()
    rate_limit_module._gmail_analysis_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _seed_job_and_message(
    session_factory, *, status: str = "APPLIED", account_key: str = ACCOUNT, uid: int = 1
):
    db = session_factory()
    try:
        job = JobRecord(
            fingerprint=f"fp-{uid}-{account_key}",
            source="test",
            title="Python Developer",
            company="Acme GmbH",
            location="Berlin",
            url="https://acme.example.com/jobs/1",
            description="",
            score=80,
            recommendation="APPLY",
            status=status,
        )
        db.add(job)
        db.commit()
        msg, _ = upsert_message(db, _parsed(uid, account_key=account_key))
        db.commit()
        return job.id, msg.id
    finally:
        db.close()


class TestAuthAndRateLimit:
    def test_analyze_requires_api_key(self, client):
        test_client, session_factory = client
        _job_id, msg_id = _seed_job_and_message(session_factory)
        response = test_client.post(f"/api/v1/gmail/messages/{msg_id}/analyze")
        assert response.status_code == 401

    def test_get_analysis_requires_api_key(self, client):
        test_client, session_factory = client
        _job_id, msg_id = _seed_job_and_message(session_factory)
        response = test_client.get(f"/api/v1/gmail/messages/{msg_id}/analysis")
        assert response.status_code == 401

    def test_list_analyses_requires_api_key(self, client):
        test_client, _session_factory = client
        response = test_client.get("/api/v1/gmail/analyses")
        assert response.status_code == 401

    def test_analyze_rate_limited(self, client, monkeypatch):
        test_client, session_factory = client
        _job_id, msg_id = _seed_job_and_message(session_factory)
        monkeypatch.setattr(rate_limit_module, "GMAIL_ANALYSIS_RATE_LIMIT_REQUESTS", 2)

        r1 = test_client.post(f"/api/v1/gmail/messages/{msg_id}/analyze", headers=_auth_headers())
        r2 = test_client.post(f"/api/v1/gmail/messages/{msg_id}/analyze", headers=_auth_headers())
        r3 = test_client.post(f"/api/v1/gmail/messages/{msg_id}/analyze", headers=_auth_headers())

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429

    def test_analyze_missing_message_returns_404(self, client):
        test_client, _session_factory = client
        response = test_client.post(
            "/api/v1/gmail/messages/999999/analyze", headers=_auth_headers()
        )
        assert response.status_code == 404

    def test_get_analysis_before_analyze_returns_404(self, client):
        test_client, session_factory = client
        _job_id, msg_id = _seed_job_and_message(session_factory)
        response = test_client.get(
            f"/api/v1/gmail/messages/{msg_id}/analysis", headers=_auth_headers()
        )
        assert response.status_code == 404


class TestLifecycleSafety:
    def test_classification_never_mutates_application_status(self, client):
        """Even when the message classifies as an OFFER (a highly
        consequential category), JobRecord.status must remain whatever it
        was before analysis — this subsystem is information-only.
        """
        test_client, session_factory = client
        job_id, _msg_id = _seed_job_and_message(session_factory, status="APPLIED")

        # A message's body is immutable once persisted (Stage 7A) —
        # instead seed a SECOND
        # message whose content is a genuine offer, and analyze that one.
        db = session_factory()
        try:
            offer_msg, _ = upsert_message(
                db,
                _parsed(
                    2,
                    message_id="<offer@example.com>",
                    body_plain="Wir freuen uns, Ihnen die Stelle anzubieten.",
                ),
            )
            db.commit()
            offer_msg_id = offer_msg.id
        finally:
            db.close()

        response = test_client.post(
            f"/api/v1/gmail/messages/{offer_msg_id}/analyze", headers=_auth_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["classification"] == "OFFER"
        assert body["requires_human_review"] is True

        db = session_factory()
        try:
            job = db.get(JobRecord, job_id)
            assert job.status == "APPLIED"  # unchanged
        finally:
            db.close()

    def test_no_send_draft_reply_forward_endpoints_registered_for_analysis(self):
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        analysis_paths = {p for p in paths if "gmail" in p and ("analy" in p or "messages" in p)}
        forbidden_fragments = (
            "send",
            "draft",
            "reply",
            "forward",
            "trash",
            "spam",
            "label",
            "mark",
        )
        for path in analysis_paths:
            for fragment in forbidden_fragments:
                assert fragment not in path.lower(), (
                    f"{path} looks like an external-action endpoint"
                )

    def test_no_external_side_effect_imports_in_stage_7b_modules(self):
        """Static regression guard: none of the four Stage 7B modules
        import/reference any networking, email-sending, or LLM-provider
        primitive. New Stage 7B code must stay pure/deterministic per
        CLAUDE.md and the spec's hard boundary.
        """
        forbidden_substrings = (
            "smtplib",
            "imaplib",
            "requests.",
            "httpx.",
            "urllib.request",
            "urlopen(",
            ".sendmail(",
            "openai",
            "anthropic",
            "SMTP(",
        )
        modules = [
            email_classifier_module,
            email_matching_module,
            gmail_analysis_repository_module,
            gmail_message_analysis_module,
        ]
        for module in modules:
            source = inspect.getsource(module)
            for forbidden in forbidden_substrings:
                assert forbidden not in source, (
                    f"{module.__name__} contains forbidden {forbidden!r}"
                )


class TestPrivacyAndBounds:
    def test_sanitized_error_on_unexpected_exception(self, client, monkeypatch):
        test_client, session_factory = client
        _job_id, msg_id = _seed_job_and_message(session_factory)

        secret_marker = "sk-super-secret-body-content-should-never-leak"

        def _boom(*args, **kwargs):
            raise RuntimeError(secret_marker)

        monkeypatch.setattr("app.api.routes.analyze_gmail_message", _boom)

        response = test_client.post(
            f"/api/v1/gmail/messages/{msg_id}/analyze", headers=_auth_headers()
        )
        assert response.status_code == 500
        assert secret_marker not in response.text

    def test_list_response_never_contains_raw_body_field(self, client):
        test_client, session_factory = client
        _job_id, msg_id = _seed_job_and_message(session_factory)
        test_client.post(f"/api/v1/gmail/messages/{msg_id}/analyze", headers=_auth_headers())

        response = test_client.get("/api/v1/gmail/analyses", headers=_auth_headers())
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert "body_plain" not in body[0]
        assert "subject" not in body[0]
        assert "from_address" not in body[0]

    def test_evidence_is_bounded_in_api_response(self, client):
        test_client, session_factory = client
        _job_id, msg_id = _seed_job_and_message(session_factory)
        response = test_client.post(
            f"/api/v1/gmail/messages/{msg_id}/analyze", headers=_auth_headers()
        )
        body = response.json()
        assert len(body["match_evidence"]) <= 10
        assert len(body["classification_evidence"]) <= 8
        assert len(body["candidate_matches"]) <= 5

    def test_account_isolation_across_gmail_username_configs(self, client, monkeypatch):
        test_client, session_factory = client
        _job_id, own_msg_id = _seed_job_and_message(session_factory, account_key=ACCOUNT, uid=1)
        _other_job_id, other_msg_id = _seed_job_and_message(
            session_factory, account_key=OTHER_ACCOUNT, uid=2
        )

        test_client.post(f"/api/v1/gmail/messages/{own_msg_id}/analyze", headers=_auth_headers())

        # The other account's message exists in the DB but is not the
        # configured mailbox — GET /gmail/messages/{id}/analysis for it
        # (looked up under the CURRENT account_key) must 404, never leak.
        response = test_client.get(
            f"/api/v1/gmail/messages/{other_msg_id}/analysis", headers=_auth_headers()
        )
        assert response.status_code == 404

        listed = test_client.get("/api/v1/gmail/analyses", headers=_auth_headers()).json()
        assert len(listed) == 1
        assert listed[0]["gmail_message_id"] == own_msg_id

    def test_pagination_limit_is_capped(self, client):
        test_client, _session_factory = client
        response = test_client.get("/api/v1/gmail/analyses?limit=99999", headers=_auth_headers())
        assert response.status_code == 422

    def test_pagination_offset_rejects_negative(self, client):
        test_client, _session_factory = client
        response = test_client.get("/api/v1/gmail/analyses?offset=-1", headers=_auth_headers())
        assert response.status_code == 422

    def test_analyze_query_count_is_bounded_regardless_of_job_count(self, client):
        test_client, session_factory = client
        db = session_factory()
        try:
            for i in range(30):
                db.add(
                    JobRecord(
                        fingerprint=f"bulk-{i}",
                        source="test",
                        title=f"Role {i}",
                        company=f"Company {i}",
                        location="",
                        url=f"https://c{i}.example.com",
                        description="",
                        score=50,
                        recommendation="MAYBE",
                        status="NEW",
                    )
                )
            db.commit()
            msg, _ = upsert_message(db, _parsed(1))
            db.commit()
            msg_id = msg.id
            engine = db.get_bind()
        finally:
            db.close()

        statements: list[str] = []

        def _track(conn, cursor, statement, parameters, context, executemany):
            if statement.strip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", _track)
        try:
            response = test_client.post(
                f"/api/v1/gmail/messages/{msg_id}/analyze", headers=_auth_headers()
            )
        finally:
            event.remove(engine, "before_cursor_execute", _track)

        assert response.status_code == 200
        # Bounded: message lookup + job candidates + thread prior matches +
        # identity check — a small constant, never O(job count).
        assert len(statements) <= 6
