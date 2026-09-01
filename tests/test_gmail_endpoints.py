"""API endpoint tests for Stage 7A Gmail Inbox Foundation:
POST /gmail/sync, GET /gmail/messages(/{id}), GET /gmail/threads(/{id}).

Includes the Stage 7A security fix round's endpoint-level regressions:
GMAIL-002 (account scoping), GMAIL-003 (sanitized errors/logs), GMAIL-007
(summary vs. detail), GMAIL-008 (thread list query count).
"""

import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.gmail_repository import upsert_message
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
ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"


def _parsed(uid: int, message_id: str | None = None, **overrides) -> ParsedGmailMessage:
    data = dict(
        account_key=ACCOUNT,
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
        gmail_username=ACCOUNT,
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


class TestGmailSyncErrorSanitization:
    """GMAIL-003: neither the HTTP response nor the server logs may ever
    carry an upstream exception's message text — only a fixed, generic
    detail string and the exception's type name.
    """

    SECRET_MARKERS = [
        "victim@example.com",
        "SECRET_PASSWORD_MARKER",
        "Private subject line",
        "attachment-name.pdf",
        "imap.internal.example.com",
    ]

    def test_poisoned_exception_text_never_reaches_response_or_logs(
        self, client, monkeypatch, caplog
    ):
        test_client, _ = client
        poisoned_message = " ".join(self.SECRET_MARKERS)
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(error=GmailConnectionError(poisoned_message)),
        )

        with caplog.at_level(logging.DEBUG):
            response = test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        assert response.status_code == 502
        assert response.json() == {"detail": "Gmail inbox sync failed"}
        for marker in self.SECRET_MARKERS:
            assert marker not in response.text
            assert marker not in caplog.text


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


class TestGmailMessageSummaryVsDetail:
    """GMAIL-007: the list endpoint must never return full correspondence
    content in bulk; the by-id detail endpoint remains the full-content
    representation.
    """

    def test_list_excludes_body_plain_and_full_recipients(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(
                messages=[_parsed(1, body_plain="Confidential recruiter message body")]
            ),
        )
        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())

        response = test_client.get("/api/v1/gmail/messages", headers=_auth_headers())

        assert response.status_code == 200
        item = response.json()[0]
        assert "body_plain" not in item
        assert "to_addresses" not in item
        assert "references" not in item
        assert "attachments" not in item
        assert "Confidential" not in response.text
        assert "attachment_count" in item

    def test_detail_includes_body_plain(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(
                messages=[_parsed(1, body_plain="Confidential recruiter message body")]
            ),
        )
        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())
        listed = test_client.get("/api/v1/gmail/messages", headers=_auth_headers()).json()
        message_id = listed[0]["id"]

        response = test_client.get(f"/api/v1/gmail/messages/{message_id}", headers=_auth_headers())

        assert response.status_code == 200
        assert response.json()["body_plain"] == "Confidential recruiter message body"


class TestGmailThreadDetail:
    def test_thread_detail_returns_bounded_message_list(self, client, monkeypatch):
        test_client, _ = client
        monkeypatch.setattr(
            "app.api.routes.GmailImapProvider",
            lambda **kwargs: FakeProvider(
                messages=[
                    _parsed(1, message_id="<root@example.com>"),
                    _parsed(
                        2,
                        message_id="<reply@example.com>",
                        references=("<root@example.com>",),
                    ),
                ]
            ),
        )
        test_client.post("/api/v1/gmail/sync", headers=_auth_headers())
        threads = test_client.get("/api/v1/gmail/threads", headers=_auth_headers()).json()
        thread_id = threads[0]["id"]

        response = test_client.get(f"/api/v1/gmail/threads/{thread_id}", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["message_count"] == 2
        assert len(body["messages"]) == 2
        # Thread detail message entries are summary-shaped, not full detail.
        assert "body_plain" not in body["messages"][0]

    def test_thread_detail_message_limit_is_bounded(self, client):
        test_client, _ = client
        response = test_client.get(
            "/api/v1/gmail/threads/1?message_limit=100000", headers=_auth_headers()
        )
        assert response.status_code == 422


class TestGmailAccountScoping:
    """GMAIL-002: read endpoints must scope to the currently configured
    Gmail account and never surface another account's persisted
    correspondence.
    """

    def test_get_endpoints_never_show_another_accounts_messages(self, client):
        test_client, session_factory = client
        session = session_factory()
        try:
            upsert_message(session, _parsed(1, account_key=OTHER_ACCOUNT))
        finally:
            session.close()

        messages = test_client.get("/api/v1/gmail/messages", headers=_auth_headers()).json()
        threads = test_client.get("/api/v1/gmail/threads", headers=_auth_headers()).json()

        assert messages == []
        assert threads == []


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
