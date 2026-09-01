"""Tests for app.db.gmail_repository (Stage 7A + security fix round) —
dedup identity, account scoping (GMAIL-002), race handling, neutral
threading (incl. GMAIL-011 collision guard), pagination, and the
GMAIL-008 grouped thread-count query.
"""

import json
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.gmail_repository import (
    get_known_uids,
    get_message_by_identity,
    get_or_create_thread,
    get_thread_by_id,
    get_thread_message_count,
    list_messages,
    list_messages_for_thread,
    list_threads_with_counts,
    resolve_thread_anchor,
    to_gmail_thread,
    upsert_message,
)
from app.db.models import GmailMessageRecord
from app.providers.email.base import ParsedGmailMessage

ACCOUNT_A = "a@example.com"
ACCOUNT_B = "b@example.com"


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
    account_key: str = ACCOUNT_A,
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
        account_key=account_key,
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
    assert record.account_key == ACCOUNT_A


def test_upsert_same_identity_twice_is_idempotent(db):
    first, created_first = upsert_message(db, _parsed())
    second, created_second = upsert_message(db, _parsed())

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(list_messages(db, ACCOUNT_A, limit=200, offset=0)) == 1


def test_same_message_id_but_different_uid_is_not_deduplicated(db):
    """Two distinct UIDs sharing a Message-ID (e.g. a malformed resend)
    must not be silently merged into one row — dedup identity is the
    provider (account_key, mailbox, uid_validity, uid), never the
    Message-ID alone.
    """
    upsert_message(db, _parsed(uid=1, message_id="<same@example.com>"))
    upsert_message(db, _parsed(uid=2, message_id="<same@example.com>"))

    assert len(list_messages(db, ACCOUNT_A, limit=200, offset=0)) == 2


def test_different_uid_validity_is_not_deduplicated(db):
    """A UIDVALIDITY change means old UIDs are no longer meaningful — the
    same raw uid number under a new UIDVALIDITY must be treated as a
    distinct identity, not a duplicate.
    """
    upsert_message(db, _parsed(uid=1, uid_validity=100))
    upsert_message(db, _parsed(uid=1, uid_validity=200))

    assert len(list_messages(db, ACCOUNT_A, limit=200, offset=0)) == 2


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

    def flaky_get(db_, account_key, mailbox, uid_validity, uid):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_get(db_, account_key, mailbox, uid_validity, uid)

    monkeypatch.setattr(gmail_repository_module, "get_message_by_identity", flaky_get)

    record, created = upsert_message(db, parsed)

    assert created is False
    assert record.id == winner.id
    assert len(list_messages(db, ACCOUNT_A, limit=200, offset=0)) == 1


# ---------------------------------------------------------------------------
# GMAIL-002: account scoping
# ---------------------------------------------------------------------------


def test_same_mailbox_uid_uidvalidity_across_two_accounts_remain_distinct(db):
    """The exact GMAIL-002 regression: switching GMAIL_USERNAME to a
    different account must never collide with another account's history
    merely because mailbox/uid_validity/uid happen to match.
    """
    record_a, created_a = upsert_message(db, _parsed(account_key=ACCOUNT_A, uid=1))
    record_b, created_b = upsert_message(db, _parsed(account_key=ACCOUNT_B, uid=1))

    assert created_a is True
    assert created_b is True
    assert record_a.id != record_b.id
    assert get_message_by_identity(db, ACCOUNT_A, "INBOX", 100, 1).id == record_a.id
    assert get_message_by_identity(db, ACCOUNT_B, "INBOX", 100, 1).id == record_b.id
    assert len(list_messages(db, ACCOUNT_A, limit=200, offset=0)) == 1
    assert len(list_messages(db, ACCOUNT_B, limit=200, offset=0)) == 1


def test_two_accounts_referencing_same_message_id_get_separate_threads(db):
    """Threads must be namespaced by account_key — the same Message-ID
    string in two different accounts is never the same conversation.
    """
    record_a, _ = upsert_message(
        db, _parsed(account_key=ACCOUNT_A, uid=1, message_id="<shared@example.com>")
    )
    record_b, _ = upsert_message(
        db, _parsed(account_key=ACCOUNT_B, uid=1, message_id="<shared@example.com>")
    )

    thread_a = get_thread_by_id(db, ACCOUNT_A, record_a.thread_id)
    thread_b = get_thread_by_id(db, ACCOUNT_B, record_b.thread_id)
    assert thread_a is not None
    assert thread_b is not None
    assert thread_a.id != thread_b.id
    # Cross-account lookup must not find the other account's thread.
    assert get_thread_by_id(db, ACCOUNT_B, thread_a.id) is None


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

    thread = get_thread_by_id(db, ACCOUNT_A, record.thread_id)
    assert thread.thread_key == "synthetic:INBOX:100:1"


def test_unrelated_messages_get_separate_threads(db):
    first, _ = upsert_message(db, _parsed(uid=1, message_id="<a@example.com>"))
    second, _ = upsert_message(db, _parsed(uid=2, message_id="<b@example.com>"))

    assert first.thread_id != second.thread_id


def test_get_or_create_thread_race_resolves_to_single_row(db):
    thread_a = get_or_create_thread(db, ACCOUNT_A, "<root@example.com>", "Subject")
    thread_b = get_or_create_thread(db, ACCOUNT_A, "<root@example.com>", "Subject")

    assert thread_a.id == thread_b.id


# ---------------------------------------------------------------------------
# GMAIL-011: Message-ID collision-aware threading
# ---------------------------------------------------------------------------


def test_duplicate_message_id_without_references_does_not_trust_merge(db):
    """Two independent messages sharing a Message-ID, neither of which
    actually references the other via References/In-Reply-To, must not
    be silently merged into the same thread merely because the header
    string matches — this could be a malformed resend or a malicious
    replay, not proof of shared conversation.
    """
    first, _ = upsert_message(
        db, _parsed(uid=1, message_id="<reused@example.com>", subject="First message")
    )
    second, _ = upsert_message(
        db, _parsed(uid=2, message_id="<reused@example.com>", subject="Unrelated second message")
    )

    assert first.thread_id != second.thread_id


def test_legitimate_reply_to_an_unambiguous_root_joins_same_thread(db):
    """A genuine reply that References a root whose Message-ID has never
    been contested must join that root's thread normally."""
    root, _ = upsert_message(db, _parsed(uid=1, message_id="<root@example.com>", subject="Root"))
    reply, _ = upsert_message(
        db,
        _parsed(
            uid=2,
            message_id="<reply@example.com>",
            references=("<root@example.com>",),
            subject="Re: Root",
        ),
    )

    assert reply.thread_id == root.thread_id


def test_reply_to_a_contested_message_id_does_not_join_either_side(db):
    """Once a Message-ID is proven ambiguous (two unrelated messages both
    claimed it), a later message that merely References that same ID
    must NOT be silently trusted as belonging to either side's
    conversation — GMAIL-011: "replies to an anchor already classified as
    ambiguous/reused must not automatically be treated as trusted common
    correspondence context."
    """
    root, _ = upsert_message(db, _parsed(uid=1, message_id="<root@example.com>", subject="Root"))
    # An unrelated message elsewhere happens to reuse "<root@example.com>"
    # as its own self-anchored identity — must get its own thread, and
    # must permanently mark that Message-ID as contested.
    unrelated, _ = upsert_message(
        db, _parsed(uid=2, message_id="<root@example.com>", subject="Unrelated")
    )
    assert unrelated.thread_id != root.thread_id

    # A message that References the now-contested anchor must not join
    # either root's or unrelated's thread.
    reply, _ = upsert_message(
        db,
        _parsed(
            uid=3,
            message_id="<reply@example.com>",
            references=("<root@example.com>",),
            subject="Re: Root",
        ),
    )

    assert reply.thread_id != root.thread_id
    assert reply.thread_id != unrelated.thread_id


def test_third_self_anchor_on_a_contested_message_id_also_gets_its_own_thread(db):
    """The contested flag is permanent — a THIRD message that also tries
    to self-anchor on an already-contested Message-ID must keep getting
    its own separate thread, never silently merged with any prior
    claimant.
    """
    root, _ = upsert_message(db, _parsed(uid=1, message_id="<root@example.com>", subject="Root"))
    second, _ = upsert_message(
        db, _parsed(uid=2, message_id="<root@example.com>", subject="Second")
    )
    third, _ = upsert_message(db, _parsed(uid=3, message_id="<root@example.com>", subject="Third"))

    thread_ids = {root.thread_id, second.thread_id, third.thread_id}
    assert len(thread_ids) == 3


def test_same_provider_message_retry_is_idempotent_even_when_self_anchored(db):
    """Re-syncing the exact same message (same provider identity) must
    never be treated as a Message-ID collision against itself."""
    parsed = _parsed(uid=1, message_id="<root@example.com>")
    first, created_first = upsert_message(db, parsed)
    second, created_second = upsert_message(db, parsed)

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert first.thread_id == second.thread_id


def test_cross_account_same_message_id_claims_are_independent(db):
    """Message-ID ownership claims are account-scoped — the same
    Message-ID self-anchored in two different accounts must never
    collide with (or contest) each other."""
    account_a_msg, _ = upsert_message(
        db, _parsed(account_key=ACCOUNT_A, uid=1, message_id="<shared@example.com>")
    )
    account_b_msg, _ = upsert_message(
        db, _parsed(account_key=ACCOUNT_B, uid=1, message_id="<shared@example.com>")
    )

    assert account_a_msg.thread_id != account_b_msg.thread_id

    # Neither claim should have been marked contested by the other.
    from app.db.gmail_repository import _get_message_id_claim

    claim_a = _get_message_id_claim(db, ACCOUNT_A, "<shared@example.com>")
    claim_b = _get_message_id_claim(db, ACCOUNT_B, "<shared@example.com>")
    assert claim_a.contested is False
    assert claim_b.contested is False


def test_concurrent_self_anchored_collision_across_real_sessions_and_threads(tmp_path):
    """GMAIL-011 (real concurrency, not monkeypatch simulation): two
    genuinely separate SQLAlchemy Sessions, each in its own OS thread,
    each importing a DIFFERENT provider message (different uid) that
    shares one reused Message-ID with no References/In-Reply-To.

    The old check-then-act guard could let both threads observe "not
    found yet" and both treat themselves as the legitimate owner. The
    DB-enforced claim (UNIQUE(account_key, message_id_header) + INSERT +
    IntegrityError-catch) must be the sole arbiter regardless of
    interleaving: both messages persist, their thread_ids differ, no
    IntegrityError escapes either thread, and both sessions remain
    usable afterward.
    """
    db_path = tmp_path / "gmail_repository_concurrency.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    barrier = threading.Barrier(2)
    results: dict[int, tuple[int, int, bool]] = {}
    errors: dict[int, BaseException] = {}

    def worker(uid: int) -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            record, created = upsert_message(
                session, _parsed(uid=uid, message_id="<race@example.com>")
            )
            results[uid] = (record.id, record.thread_id, created)
            # The session must remain usable after the race, whichever
            # side (winner or collision-loser) this thread landed on.
            session.execute(select(GmailMessageRecord)).all()
        except BaseException as exc:  # noqa: BLE001 - captured for the main thread to assert on
            errors[uid] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == {}, f"unhandled exception(s) in worker threads: {errors}"
    assert set(results.keys()) == {1, 2}

    _, thread_id_1, created_1 = results[1]
    _, thread_id_2, created_2 = results[2]
    assert created_1 is True
    assert created_2 is True
    assert thread_id_1 != thread_id_2

    verify_session = session_factory()
    try:
        assert len(list_messages(verify_session, ACCOUNT_A, limit=10, offset=0)) == 2
    finally:
        verify_session.close()


def test_to_gmail_thread_reports_message_count_from_grouped_query(db):
    upsert_message(db, _parsed(uid=1, message_id="<root@example.com>"))
    upsert_message(
        db,
        _parsed(uid=2, message_id="<reply@example.com>", references=("<root@example.com>",)),
    )

    pairs = list_threads_with_counts(db, ACCOUNT_A, limit=200, offset=0)
    assert len(pairs) == 1
    thread = to_gmail_thread(*pairs[0])
    assert thread.message_count == 2


# ---------------------------------------------------------------------------
# GMAIL-008: thread list query count must not scale with thread count
# ---------------------------------------------------------------------------


def test_list_threads_with_counts_query_count_is_bounded(db, tmp_path):
    for uid in range(1, 11):
        upsert_message(db, _parsed(uid=uid, message_id=f"<{uid}@example.com>"))

    executed_statements = []

    def _track(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _track)
    try:
        pairs = list_threads_with_counts(db, ACCOUNT_A, limit=200, offset=0)
    finally:
        event.remove(engine, "before_cursor_execute", _track)

    assert len(pairs) == 10
    # One query for the whole page, regardless of how many threads it
    # returns — not one COUNT query per thread (the N+1 GMAIL-008 bug).
    select_statements = [s for s in executed_statements if s.strip().upper().startswith("SELECT")]
    assert len(select_statements) == 1


# ---------------------------------------------------------------------------
# GMAIL-012: bulk known-UID lookup must not scale one-query-per-UID
# ---------------------------------------------------------------------------


def _count_select_statements(engine, action) -> int:
    executed = []

    def _track(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            executed.append(statement)

    event.listen(engine, "before_cursor_execute", _track)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", _track)
    return len(executed)


def test_get_known_uids_issues_one_query_for_a_single_candidate(db):
    engine = db.get_bind()
    query_count = _count_select_statements(
        engine, lambda: get_known_uids(db, ACCOUNT_A, "INBOX", 100, [1])
    )
    assert query_count == 1


def test_get_known_uids_issues_one_query_for_100_candidates(db):
    engine = db.get_bind()
    query_count = _count_select_statements(
        engine, lambda: get_known_uids(db, ACCOUNT_A, "INBOX", 100, list(range(1, 101)))
    )
    assert query_count == 1


def test_get_known_uids_issues_bounded_chunked_queries_for_10000_candidates(db):
    """The critical GMAIL-012 regression: 10,000 candidate UIDs must
    never produce anywhere close to 10,000 SELECTs — only
    ceil(10000 / KNOWN_UIDS_QUERY_CHUNK_SIZE) chunk queries.
    """
    from app.db.gmail_repository import KNOWN_UIDS_QUERY_CHUNK_SIZE

    engine = db.get_bind()
    candidate_uids = list(range(1, 10_001))
    query_count = _count_select_statements(
        engine, lambda: get_known_uids(db, ACCOUNT_A, "INBOX", 100, candidate_uids)
    )
    expected_chunks = -(-len(candidate_uids) // KNOWN_UIDS_QUERY_CHUNK_SIZE)  # ceil div
    assert query_count == expected_chunks
    assert query_count < 100  # nowhere close to one-query-per-UID


def test_get_known_uids_returns_correct_membership_across_chunk_boundaries(db):
    for uid in (1, 500, 501, 999, 1000, 1500):
        upsert_message(db, _parsed(uid=uid, message_id=f"<{uid}@example.com>"))

    known = get_known_uids(db, ACCOUNT_A, "INBOX", 100, list(range(1, 1501)))

    assert known == {1, 500, 501, 999, 1000, 1500}


def test_get_known_uids_is_scoped_by_account_mailbox_and_uid_validity(db):
    upsert_message(db, _parsed(account_key=ACCOUNT_A, uid=1, message_id="<a@example.com>"))
    upsert_message(db, _parsed(account_key=ACCOUNT_B, uid=1, message_id="<b@example.com>"))

    assert get_known_uids(db, ACCOUNT_A, "INBOX", 100, [1]) == {1}
    assert get_known_uids(db, ACCOUNT_B, "INBOX", 100, [1]) == {1}
    assert get_known_uids(db, ACCOUNT_A, "INBOX", 200, [1]) == set()
    assert get_known_uids(db, ACCOUNT_A, "OTHERBOX", 100, [1]) == set()


def test_get_thread_message_count_single_thread(db):
    record, _ = upsert_message(db, _parsed(uid=1, message_id="<root@example.com>"))
    upsert_message(
        db, _parsed(uid=2, message_id="<r2@example.com>", references=("<root@example.com>",))
    )

    assert get_thread_message_count(db, ACCOUNT_A, record.thread_id) == 2


def test_list_messages_for_thread_is_bounded_and_scoped(db):
    root, _ = upsert_message(db, _parsed(uid=1, message_id="<root@example.com>"))
    for uid in range(2, 6):
        upsert_message(
            db,
            _parsed(uid=uid, message_id=f"<{uid}@example.com>", references=("<root@example.com>",)),
        )

    messages = list_messages_for_thread(db, ACCOUNT_A, root.thread_id, limit=2)
    assert len(messages) == 2

    # Cross-account access must return nothing even for a valid thread id.
    assert list_messages_for_thread(db, ACCOUNT_B, root.thread_id, limit=200) == []


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_list_messages_respects_limit_and_offset(db):
    for uid in range(1, 6):
        upsert_message(db, _parsed(uid=uid, message_id=f"<{uid}@example.com>"))

    page_one = list_messages(db, ACCOUNT_A, limit=2, offset=0)
    page_two = list_messages(db, ACCOUNT_A, limit=2, offset=2)

    assert len(page_one) == 2
    assert len(page_two) == 2
    assert {record.id for record in page_one}.isdisjoint({record.id for record in page_two})


def test_list_threads_with_counts_respects_limit_and_offset(db):
    for uid in range(1, 6):
        upsert_message(db, _parsed(uid=uid, message_id=f"<{uid}@example.com>"))

    page = list_threads_with_counts(db, ACCOUNT_A, limit=3, offset=0)
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
