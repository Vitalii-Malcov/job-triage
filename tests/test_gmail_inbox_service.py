"""Tests for app.services.gmail_inbox.GmailInboxService (Stage 7A)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.gmail_repository import list_messages
from app.db.models import JobRecord
from app.providers.email.base import GmailFetchResult, ParsedGmailMessage
from app.services.gmail_inbox import GmailInboxService


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gmail_inbox_service.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


ACCOUNT = "me@example.com"


def _parsed(uid: int, message_id: str | None = None) -> ParsedGmailMessage:
    return ParsedGmailMessage(
        account_key=ACCOUNT,
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=message_id or f"<{uid}@example.com>",
        in_reply_to=None,
        references=(),
        from_address="recruiter@example.com",
        from_display_name=None,
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


class FakeProvider:
    def __init__(self, messages: list[ParsedGmailMessage], skipped_count: int = 0) -> None:
        self._messages = tuple(messages)
        self._skipped_count = skipped_count
        self.fetch_calls = 0

    async def fetch(self):
        self.fetch_calls += 1
        return GmailFetchResult(messages=self._messages, skipped_count=self._skipped_count)


@pytest.mark.asyncio
async def test_sync_reports_created_and_skipped_counts(db):
    provider = FakeProvider([_parsed(1), _parsed(2)], skipped_count=3)

    result = await GmailInboxService().sync(db, provider)

    assert result.fetched == 2
    assert result.created == 2
    assert result.duplicates == 0
    assert result.skipped == 3
    assert result.failed == 0


@pytest.mark.asyncio
async def test_sync_twice_reports_duplicates_on_second_run(db):
    provider_run_1 = FakeProvider([_parsed(1), _parsed(2)])
    provider_run_2 = FakeProvider([_parsed(1), _parsed(2)])

    first = await GmailInboxService().sync(db, provider_run_1)
    second = await GmailInboxService().sync(db, provider_run_2)

    assert first.created == 2
    assert first.duplicates == 0
    assert second.fetched == 2
    assert second.created == 0
    assert second.duplicates == 2
    assert second.failed == 0
    assert len(list_messages(db, ACCOUNT, limit=200, offset=0)) == 2


@pytest.mark.asyncio
async def test_one_message_persistence_failure_does_not_lose_the_others(db, monkeypatch):
    provider = FakeProvider([_parsed(1), _parsed(2), _parsed(3)])

    import app.services.gmail_inbox as service_module

    original_upsert = service_module.upsert_message
    call_count = {"n": 0}

    def flaky_upsert(db_, parsed):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated persistence failure")
        return original_upsert(db_, parsed)

    monkeypatch.setattr(service_module, "upsert_message", flaky_upsert)

    result = await GmailInboxService().sync(db, provider)

    assert result.fetched == 3
    assert result.created == 2
    assert result.failed == 1
    assert len(list_messages(db, ACCOUNT, limit=200, offset=0)) == 2


@pytest.mark.asyncio
async def test_sync_never_touches_job_records(db):
    """Stage 7A is read-only infrastructure with respect to Job/Application
    lifecycle — a sync run must never create, update, or reference
    JobRecord rows.
    """
    provider = FakeProvider([_parsed(1), _parsed(2)])

    await GmailInboxService().sync(db, provider)

    assert db.scalars(select(JobRecord)).all() == []


@pytest.mark.asyncio
async def test_sync_result_never_contains_message_content(db):
    provider = FakeProvider([_parsed(1)])

    result = await GmailInboxService().sync(db, provider)

    dumped = result.model_dump()
    assert set(dumped.keys()) == {"fetched", "created", "duplicates", "skipped", "failed"}


@pytest.mark.asyncio
async def test_persist_failure_never_logs_exception_text_or_pii(db, monkeypatch, caplog):
    """GMAIL-003: a persistence-layer exception's own message string could
    in principle embed row content (subject/body/addresses secrets) —
    this must never reach the logs, only the exception's type name.
    """
    import logging

    import app.services.gmail_inbox as service_module

    secret_markers = [
        "victim@example.com",
        "SECRET_PASSWORD_MARKER",
        "Private subject line",
        "attachment-name.pdf",
        "imap.internal.example.com",
    ]

    def poisoned_upsert(db_, parsed):
        raise RuntimeError(" ".join(secret_markers))

    monkeypatch.setattr(service_module, "upsert_message", poisoned_upsert)

    provider = FakeProvider([_parsed(1)])
    with caplog.at_level(logging.DEBUG):
        result = await GmailInboxService().sync(db, provider)

    assert result.failed == 1
    log_text = caplog.text
    for marker in secret_markers:
        assert marker not in log_text
