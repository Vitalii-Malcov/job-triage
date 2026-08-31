"""API endpoint tests for Stage 7A Gmail Inbox Foundation:
POST /gmail/sync, GET /gmail/messages(/{id}), GET /gmail/threads(/{id}).
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.providers.email.base import (
    GmailAuthError,
    GmailConnectionError,
    GmailFetchResult,
    ParsedGmailMessage,
)
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


def _parsed(uid: int, message_id: str | None = None, **overrides) -> ParsedGmailMessage:
    data = dict(
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=message_id or f"<{uid}@example.com>",
        in_reply_to=None,
        references=(),
        from_address="recruiter@example.com",
        from_display_name="Recruiter",
        to_addresses=("me@example.com",),
        cc_addresses=(),
        subject=f"Subject {uid}",
        sent_at=datetime.now(UTC),
        direction="INBOUND",
        body_plain="hello",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    data.update(overrides)
    return ParsedGmailMessage(**data)


class FakeProvider:
    def __init__(
        self,
        messages: list[ParsedGmailMessage] | None = None,
        error: Exception | None = None,
    ):
        self._messages = tuple(messages or [])
        self._error = error

    async def fetch(self):
        if self._error is not None:
            raise self._error
        return GmailFetchResult(messages=self._messages, skipped_count=0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_gmail_endpoints.db"
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
        gmail_username="me@example.com",
        gmail_app_password="app-password",
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._gmail_requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._gmail_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


class TestRunGmailSync:
    def test_requires_api_key_auth(self, client):
        test_client, _ = client
        response = test_client.post("/api/v1/gmail/sync")
        assert response.status_code == 401

    def test_missing_mailbox_config_returns_503(self, client, monkeypatch):
        test_client, _ = client
        unconfigured = Settings(
            api_key=API_KEY,
            rate_limit_requests=1000,
            gmail_username="",
            gmail_app_password="",
        )
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)

        response = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert response.status_code == 503

    def test_missing_mailbox_config_returns_503_for_whitespace_only_password(
        self, client, monkeypatch
    ):
        test_client, _ = client
        unconfigured = Settings(
            api_key=API_KEY,
            rate_limit_requests=1000,
            gmail_username="me@example.com",
            gmail_app_password="   ",
        )
        monkeypatch.setattr("app.api.routes.get_settings", lambda: unconfigured)

        response = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert response.status_code == 503

    def test_successful_run_reports_counts(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1), _parsed(2)]),
        )

        response = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert response.status_code == 200
        assert response.json() == {
            "fetched": 2,
            "created": 2,
            "duplicates": 0,
            "skipped": 0,
            "failed": 0,
        }

    def test_second_run_reports_duplicates(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1), _parsed(2)]),
        )

        first = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())
        second = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert first.json()["created"] == 2
        assert second.json() == {
            "fetched": 2,
            "created": 0,
            "duplicates": 2,
            "skipped": 0,
            "failed": 0,
        }

    def test_auth_error_returns_502(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(error=GmailAuthError("login rejected")),
        )

        response = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert response.status_code == 502

    def test_connection_error_returns_502(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(error=GmailConnectionError("boom")),
        )

        response = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert response.status_code == 502

    def test_sync_response_never_contains_message_content(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1, subject="Secret subject")]),
        )

        response = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert response.status_code == 200
        assert "Secret" not in response.text
        assert set(response.json().keys()) == {
            "fetched",
            "created",
            "duplicates",
            "skipped",
            "failed",
        }

    def test_gmail_rate_limit_is_stricter_than_general_limit(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1)]),
        )
        monkeypatch.setattr("app.security.rate_limit.GMAIL_RATE_LIMIT_REQUESTS", 1)
        rate_limit_module._gmail_requests.clear()

        first = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())
        second = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert first.status_code == 200
        assert second.status_code == 429


class TestGetGmailMessagesAndThreads:
    def test_requires_api_key_auth(self, client):
        test_client, _ = client
        assert test_client.get("/api/v1/gmail/messages").status_code == 401
        assert test_client.get("/api/v1/gmail/threads").status_code == 401

    def test_get_never_triggers_a_sync(self, client, monkeypatch):
        test_client, _ = client

        def boom(**kwargs):
            raise AssertionError("GET must never construct/trigger a mailbox provider")

        monkeypatch.setattr("app.api.routes.GmailImapProvider", boom)

        response = test_client.get("/api/v1/gmail/messages", headers=_auth_headers())
        assert response.status_code == 200
        assert response.json() == []

    def test_list_messages_after_sync(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1), _parsed(2)]),
        )
        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        response = test_client.get("/api/v1/gmail/messages", headers=_auth_headers())

        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_pagination_limit_is_bounded(self, client):
        test_client, _ = client
        response = test_client.get("/api/v1/gmail/messages?limit=100000", headers=_auth_headers())
        assert response.status_code == 422

    def test_pagination_limit_and_offset_applied(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(
                messages=[_parsed(uid) for uid in range(1, 6)],
            ),
        )
        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        response = test_client.get(
            "/api/v1/gmail/messages?limit=2&offset=0", headers=_auth_headers()
        )
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_get_message_by_id_not_found_returns_404(self, client):
        test_client, _ = client
        response = test_client.get("/api/v1/gmail/messages/999999", headers=_auth_headers())
        assert response.status_code == 404

    def test_get_message_by_id_returns_full_message(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1, subject="Interview invite")]),
        )
        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())
        listed = test_client.get("/api/v1/gmail/messages", headers=_auth_headers()).json()
        message_id = listed[0]["id"]

        response = test_client.get(f"/api/v1/gmail/messages/{message_id}", headers=_auth_headers())

        assert response.status_code == 200
        assert response.json()["subject"] == "Interview invite"

    def test_get_thread_by_id_not_found_returns_404(self, client):
        test_client, _ = client
        response = test_client.get("/api/v1/gmail/threads/999999", headers=_auth_headers())
        assert response.status_code == 404

    def test_list_threads_after_sync(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1), _parsed(2)]),
        )
        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        response = test_client.get("/api/v1/gmail/threads", headers=_auth_headers())

        assert response.status_code == 200
        threads = response.json()
        assert len(threads) == 2
        assert all("message_count" in thread for thread in threads)


class TestGmailInboxIsReadOnly:
    """Regression guard: nothing under the Gmail Inbox Foundation surface
    can send/reply/draft mail, mutate ApplicationStatus, or trigger a
    Job/Application linkage."""

    def test_no_send_or_draft_endpoints_are_registered(self):
        from app.api.routes import router as gmail_router

        paths = {
            getattr(route, "path", None)
            for route in gmail_router.routes
            if getattr(route, "path", None)
        }
        assert any("/gmail" in path for path in paths), "expected Gmail routes to be registered"
        for forbidden_fragment in ("/send", "/reply", "/draft"):
            assert not any("/gmail" in path and forbidden_fragment in path for path in paths), (
                f"unexpected Gmail write endpoint containing {forbidden_fragment!r}"
            )

    def test_sync_does_not_create_any_job_records(self, client, monkeypatch):
        test_client, session_factory = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(messages=[_parsed(1), _parsed(2)]),
        )

        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        from sqlalchemy import select

        from app.db.models import JobRecord

        session = session_factory()
        try:
            assert session.scalars(select(JobRecord)).all() == []
        finally:
            session.close()
