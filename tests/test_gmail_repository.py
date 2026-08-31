"""Tests for app.db.gmail_repository (Stage 7A) — dedup identity, race
handling, neutral threading, and pagination.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.gmail_repository import (
    get_or_create_thread,
    get_thread_by_id,
    list_messages,
    list_threads,
    resolve_thread_anchor,
    to_gmail_thread,
    upsert_message,
)
from app.providers.email.base import ParsedGmailMessage


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gmail_repository.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _parsed(
    *,
    uid: int = 1,
    uid_validity: int = 100,
    mailbox: str = "INBOX",
    message_id: str | None = "<msg1@example.com>",
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
    subject: str = "Hello",
    from_address: str | None = "recruiter@example.com",
) -> ParsedGmailMessage:
    return ParsedGmailMessage(
        mailbox=mailbox,
        uid=uid,
        uid_validity=uid_validity,
        message_id_header=message_id,
        in_reply_to=in_reply_to,
        references=references,
        from_address=from_address,
        from_display_name=None,
        to_addresses=("me@example.com",),
        cc_addresses=(),
        subject=subject,
        sent_at=datetime.now(UTC),
        direction="INBOUND",
        body_plain="hello",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )


# ---------------------------------------------------------------------------
# Dedup identity
# ---------------------------------------------------------------------------


def test_upsert_creates_new_message(db):
    record, created = upsert_message(db, _parsed())
    assert created is True
    assert record.mailbox == "INBOX"
    assert record.uid == 1
    assert record.uid_validity == 100


def test_upsert_same_identity_twice_is_idempotent(db):
    first, created_first = upsert_message(db, _parsed())
    second, created_second = upsert_message(db, _parsed())

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(list_messages(db, limit=200, offset=0)) == 1


def test_same_message_id_but_different_uid_is_not_deduplicated(db):
    """Two distinct UIDs sharing a Message-ID (e.g. a malformed resend)
    must not be silently merged into one row — dedup identity is the
    provider (mailbox, uid_validity, uid), never the Message-ID alone.
    """
    upsert_message(db, _parsed(uid=1, message_id="<same@example.com>"))
    upsert_message(db, _parsed(uid=2, message_id="<same@example.com>"))

    assert len(list_messages(db, limit=200, offset=0)) == 2


def test_different_uid_validity_is_not_deduplicated(db):
    """A UIDVALIDITY change means old UIDs are no longer meaningful — the
    same raw uid number under a new UIDVALIDITY must be treated as a
    distinct identity, not a duplicate.
    """
    upsert_message(db, _parsed(uid=1, uid_validity=100))
    upsert_message(db, _parsed(uid=1, uid_validity=200))

    assert len(list_messages(db, limit=200, offset=0)) == 2


def test_message_without_message_id_is_persisted_and_deduplicated(db):
    parsed = _parsed(message_id=None)
    first, created_first = upsert_message(db, parsed)
    second, created_second = upsert_message(db, parsed)

    assert created_first is True
    assert created_second is False
    assert first.message_id_header is None
    assert first.id == second.id


def test_concurrent_duplicate_insert_resolves_via_unique_constraint(db, monkeypatch):
    """Simulates two racing syncs both passing the initial
    get_message_by_identity() == None check before either commits — the
    DB UNIQUE constraint (not just the Python check) must be the real
    guard, per spec section 22. Forces the race by making this caller's
    own existence check lie once (return None) after a concurrent winner
    has already committed, so its INSERT collides and must be resolved
    via the IntegrityError-catch-and-reload path rather than crashing or
    creating a second row.
    """
    import app.db.gmail_repository as gmail_repository_module

    parsed = _parsed()
    winner, _ = upsert_message(db, parsed)

    original_get = gmail_repository_module.get_message_by_identity
    calls = {"n": 0}

    def flaky_get(db_, mailbox, uid_validity, uid):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_get(db_, mailbox, uid_validity, uid)

    monkeypatch.setattr(gmail_repository_module, "get_message_by_identity", flaky_get)

    record, created = upsert_message(db, parsed)

    assert created is False
    assert record.id == winner.id
    assert len(list_messages(db, limit=200, offset=0)) == 1


# ---------------------------------------------------------------------------
# Threading
# ---------------------------------------------------------------------------


def test_resolve_thread_anchor_prefers_references_root():
    anchor = resolve_thread_anchor(
        "<child@example.com>",
        "<parent@example.com>",
        ("<root@example.com>", "<parent@example.com>"),
    )
    assert anchor == "<root@example.com>"


def test_resolve_thread_anchor_falls_back_to_in_reply_to():
    anchor = resolve_thread_anchor("<child@example.com>", "<parent@example.com>", ())
    assert anchor == "<parent@example.com>"


def test_resolve_thread_anchor_falls_back_to_own_message_id():
    anchor = resolve_thread_anchor("<root@example.com>", None, ())
    assert anchor == "<root@example.com>"


def test_resolve_thread_anchor_none_when_nothing_available():
    assert resolve_thread_anchor(None, None, ()) is None


def test_reply_arriving_before_root_still_joins_same_thread_once_root_arrives(db):
    reply = _parsed(
        uid=2,
        message_id="<reply@example.com>",
        references=("<root@example.com>",),
        subject="Re: Application",
    )
    root = _parsed(uid=1, message_id="<root@example.com>", subject="Application")

    reply_record, _ = upsert_message(db, reply)
    root_record, _ = upsert_message(db, root)

    assert reply_record.thread_id == root_record.thread_id


def test_root_arriving_before_reply_joins_same_thread(db):
    root = _parsed(uid=1, message_id="<root@example.com>", subject="Application")
    reply = _parsed(
        uid=2,
        message_id="<reply@example.com>",
        references=("<root@example.com>",),
        subject="Re: Application",
    )

    root_record, _ = upsert_message(db, root)
    reply_record, _ = upsert_message(db, reply)

    assert reply_record.thread_id == root_record.thread_id


def test_message_with_no_threading_headers_gets_its_own_synthetic_thread(db):
    record, _ = upsert_message(db, _parsed(uid=1, message_id=None))

    thread = get_thread_by_id(db, record.thread_id)
    assert thread.thread_key == "synthetic:INBOX:100:1"


def test_unrelated_messages_get_separate_threads(db):
    first, _ = upsert_message(db, _parsed(uid=1, message_id="<a@example.com>"))
    second, _ = upsert_message(db, _parsed(uid=2, message_id="<b@example.com>"))

    assert first.thread_id != second.thread_id


def test_get_or_create_thread_race_resolves_to_single_row(db):
    thread_a = get_or_create_thread(db, "<root@example.com>", "Subject")
    thread_b = get_or_create_thread(db, "<root@example.com>", "Subject")

    assert thread_a.id == thread_b.id


def test_to_gmail_thread_reports_message_count(db):
    upsert_message(db, _parsed(uid=1, message_id="<root@example.com>"))
    upsert_message(
        db,
        _parsed(uid=2, message_id="<reply@example.com>", references=("<root@example.com>",)),
    )

    thread_record = get_or_create_thread(db, "<root@example.com>", "Subject")
    thread = to_gmail_thread(db, thread_record)

    assert thread.message_count == 2


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_list_messages_respects_limit_and_offset(db):
    for uid in range(1, 6):
        upsert_message(db, _parsed(uid=uid, message_id=f"<{uid}@example.com>"))

    page_one = list_messages(db, limit=2, offset=0)
    page_two = list_messages(db, limit=2, offset=2)

    assert len(page_one) == 2
    assert len(page_two) == 2
    assert {record.id for record in page_one}.isdisjoint({record.id for record in page_two})


def test_list_threads_respects_limit_and_offset(db):
    for uid in range(1, 6):
        upsert_message(db, _parsed(uid=uid, message_id=f"<{uid}@example.com>"))

    page = list_threads(db, limit=3, offset=0)
    assert len(page) == 3


# ---------------------------------------------------------------------------
# Content round-trip (no data loss for future stages)
# ---------------------------------------------------------------------------


def test_attachments_and_addresses_round_trip_through_json(db):
    from app.providers.email.base import ParsedAttachment

    parsed = _parsed()
    parsed = ParsedGmailMessage(
        **{**parsed.__dict__, "attachments": (ParsedAttachment("cv.pdf", "application/pdf", 1024),)}
    )
    record, _ = upsert_message(db, parsed)

    assert json.loads(record.attachments_json) == [
        {"filename": "cv.pdf", "content_type": "application/pdf", "size": 1024}
    ]
    assert json.loads(record.to_addresses_json) == ["me@example.com"]
