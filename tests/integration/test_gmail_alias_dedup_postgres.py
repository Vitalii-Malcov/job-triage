"""ASTRA-GMAIL-001-R1: a focused PostgreSQL integration test proving that
concurrent imports of the SAME Gmail message under two equivalent
mailbox spellings (`[Gmail]/Отправленные` vs its modified-UTF-7 form
`[Gmail]/&BB4E...-`) persist exactly ONE `gmail_messages` row -- against
a REAL PostgreSQL server (READ COMMITTED, independent connections), not
just SQLite (see tests/test_gmail_repository.py's
`test_concurrent_alias_imports_create_exactly_one_message` for the
SQLite-based deterministic version of the same interleaving).

The guarantee under test is the database's own
`uq_gmail_messages_account_provider_identity` UNIQUE constraint: since
`app.db.gmail_repository.upsert_message` persists only
`canonical_mailbox_identity(mailbox)`, both aliases insert the same key
and the loser's INSERT fails inside PostgreSQL itself -- no Python or
process-local lock is involved in the correctness argument.

**Local execution:** skipped automatically unless `TEST_POSTGRES_URL` is
set; run `alembic upgrade head` (with `DATABASE_URL` pointed at that same
database) first -- the schema must come from the real migration chain.

**CI:** `.github/workflows/ci.yml`'s PostgreSQL job runs this module with
zero skips allowed.
"""

import os
import threading

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

import app.db.gmail_repository as repository
from app.db.gmail_repository import upsert_message
from app.db.models import GmailMessageIdClaimRecord, GmailMessageRecord, GmailThreadRecord
from app.providers.email.base import ParsedGmailMessage

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job always sets it).",
)

ACCOUNT = "me@gmail-alias-dedup-postgres-integration.example.com"
SENT_RU = "[Gmail]/Отправленные"
SENT_RU_ENCODED = "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"

HEADER_VARIANTS = {
    "message-id": {"message_id_header": "<pg-race@example.com>"},
    "reply": {
        "message_id_header": "<pg-race-reply@example.com>",
        "in_reply_to": "<pg-race-root@example.com>",
        "references": ("<pg-race-root@example.com>",),
    },
    "no-threading-headers": {"message_id_header": None},
}
SPELLINGS = [(SENT_RU_ENCODED, SENT_RU), (SENT_RU, SENT_RU_ENCODED)]


def _parsed(mailbox: str, **headers) -> ParsedGmailMessage:
    fields = {"message_id_header": None, "in_reply_to": None, "references": ()}
    fields.update(headers)
    return ParsedGmailMessage(
        account_key=ACCOUNT,
        mailbox=mailbox,
        uid=7,
        uid_validity=100,
        from_address="recruiter@example.com",
        from_display_name=None,
        to_addresses=("me@example.com",),
        cc_addresses=(),
        subject="Hello",
        sent_at=None,
        direction="INBOUND",
        body_plain="hello",
        body_truncated=False,
        has_html=False,
        attachments=(),
        **fields,
    )


@pytest.fixture()
def pg_session_factory():
    # NEW-007: no Base.metadata.create_all -- schema from Alembic only.
    engine = create_engine(TEST_POSTGRES_URL, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def cleanup() -> None:
        with engine.begin() as conn:
            conn.execute(
                delete(GmailMessageIdClaimRecord).where(
                    GmailMessageIdClaimRecord.account_key == ACCOUNT
                )
            )
            conn.execute(
                delete(GmailMessageRecord).where(GmailMessageRecord.account_key == ACCOUNT)
            )
            conn.execute(delete(GmailThreadRecord).where(GmailThreadRecord.account_key == ACCOUNT))

    cleanup()
    yield factory
    cleanup()
    engine.dispose()


def _stored_rows(factory) -> list[tuple[str, int, int]]:
    with factory() as session:
        rows = session.scalars(
            select(GmailMessageRecord).where(GmailMessageRecord.account_key == ACCOUNT)
        ).all()
        return [(row.mailbox, row.uid_validity, row.uid) for row in rows]


@pytest.mark.parametrize("headers", HEADER_VARIANTS.values(), ids=HEADER_VARIANTS.keys())
@pytest.mark.parametrize(("spelling_a", "spelling_b"), SPELLINGS, ids=["enc-read", "read-enc"])
def test_alias_import_committed_after_the_lookup_is_resolved_as_existing(
    monkeypatch, pg_session_factory, spelling_a, spelling_b, headers
):
    """Astra's exact interleaving: A's duplicate lookup sees nothing, B
    (separate connection) inserts and commits, then A inserts."""
    original_resolve = repository._resolve_thread_for_message
    session_a, session_b = pg_session_factory(), pg_session_factory()
    results = {}

    def resolve_after_b_commits(db, parsed):
        if db is session_a and "b" not in results:
            results["b"] = upsert_message(session_b, _parsed(spelling_b, **headers))
        return original_resolve(db, parsed)

    monkeypatch.setattr(repository, "_resolve_thread_for_message", resolve_after_b_commits)
    try:
        record_a, created_a = upsert_message(session_a, _parsed(spelling_a, **headers))
        record_b, created_b = results["b"]
        assert (created_a, created_b) == (False, True)
        assert record_a.id == record_b.id
    finally:
        session_a.close()
        session_b.close()

    assert _stored_rows(pg_session_factory) == [(SENT_RU_ENCODED, 100, 7)]


@pytest.mark.parametrize("headers", HEADER_VARIANTS.values(), ids=HEADER_VARIANTS.keys())
@pytest.mark.parametrize(("spelling_a", "spelling_b"), SPELLINGS, ids=["enc-read", "read-enc"])
def test_truly_concurrent_alias_imports_create_exactly_one_row(
    monkeypatch, pg_session_factory, spelling_a, spelling_b, headers
):
    """Two real threads/connections, held at a barrier until BOTH have
    passed the pre-insert lookup, then released to race thread
    resolution, locking and INSERT."""
    original_resolve = repository._resolve_thread_for_message
    barrier = threading.Barrier(2, timeout=30)

    def resolve_after_both_looked_up(db, parsed):
        barrier.wait()
        return original_resolve(db, parsed)

    monkeypatch.setattr(repository, "_resolve_thread_for_message", resolve_after_both_looked_up)
    outcomes: dict[str, object] = {}

    def run(name: str, mailbox: str) -> None:
        with pg_session_factory() as session:
            try:
                record, created = upsert_message(session, _parsed(mailbox, **headers))
                outcomes[name] = (record.id, created)
            except Exception as exc:  # surfaced by the assertion below
                outcomes[name] = exc

    threads = [
        threading.Thread(target=run, args=("a", spelling_a)),
        threading.Thread(target=run, args=("b", spelling_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert all(isinstance(outcome, tuple) for outcome in outcomes.values()), outcomes
    (id_a, created_a), (id_b, created_b) = outcomes["a"], outcomes["b"]
    assert sorted([created_a, created_b]) == [False, True]
    assert id_a == id_b
    assert _stored_rows(pg_session_factory) == [(SENT_RU_ENCODED, 100, 7)]
