"""AUD-004 (Astra R3): a focused PostgreSQL integration test proving
`app.services.automation_gmail.prepare_gmail_response_drafts` never
permanently skips a Gmail message merely because PostgreSQL sequence
allocation order differed from commit-visibility order -- against a REAL
PostgreSQL server, not just SQLite (see
tests/test_automation_gmail.py's `TestPostgresCommitOrderWatermarkRace`
for the SQLite-based deterministic simulation of the same end state this
mirrors, and its own docstring for exactly why SQLite's single-writer
lock cannot itself produce this via genuinely concurrent transactions).

**The scenario.** Transaction A inserts message A and obtains a LOWER
`id` via `nextval()` (PostgreSQL sequences are NOT transactional -- the
value is consumed immediately, never rolled back) but does NOT commit
yet. Transaction B inserts message B, obtains a HIGHER `id`, and commits
FIRST. Automation runs under READ COMMITTED (PostgreSQL's default) and
sees only B (A's insert is still invisible, uncommitted). Transaction A
then commits. Required result: message A is still discovered and fully
processed on the very next scan -- never permanently skipped just
because a higher-id message was already scanned first.

**Local execution:** skipped automatically unless `TEST_POSTGRES_URL` is
set -- this project's default dev/test setup is SQLite-only.

**CI:** `.github/workflows/ci.yml`'s `scheduler-postgres` job (renamed
in comment only -- same real `postgres:16` service container) also runs
this module on every push/PR.

This module never duplicates the fetch/selection SQL itself -- it calls
the REAL `app.services.automation_gmail.prepare_gmail_response_drafts`,
synchronized via `threading.Event`s (never a plain `time.sleep` race)
against a second, genuinely independent connection that holds its own
INSERT open without committing. Self-contained (duplicates the tiny
`_settings`/body-classification helpers `tests/test_automation_gmail.py`
also defines) rather than importing from that SQLite-only module, exactly
like `tests/integration/test_scheduler_postgres_concurrency.py`.
"""

import asyncio
import os
import threading

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    GmailMessageAnalysisRecord,
    GmailMessageRecord,
    GmailThreadRecord,
    ResponseDraftRecord,
)
from app.services.automation_gmail import prepare_gmail_response_drafts

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job always sets it).",
)

ACCOUNT = "me@gmail-watermark-postgres-integration.example.com"

# body_plain that classifies as OFFER -> supported -> PROPOSED response draft
# -- mirrors tests/test_automation_gmail.py's own OFFER_BODY constant.
OFFER_BODY = "We are pleased to offer you the position of Backend Engineer at Globex."


def _settings(**overrides) -> Settings:
    data = {
        "automation_gmail_cycle_enabled": True,
        "automation_gmail_process_max_per_run": 100,
        "gmail_username": ACCOUNT,
        "gmail_app_password": "app-password",
    }
    data.update(overrides)
    return Settings(**data)


def _build_message(*, thread_id: int, uid: int, subject: str) -> GmailMessageRecord:
    return GmailMessageRecord(
        thread_id=thread_id,
        account_key=ACCOUNT,
        mailbox="INBOX",
        uid_validity=100,
        uid=uid,
        message_id_header=f"<{uid}@pg-watermark-race.example.com>",
        references_json="[]",
        to_addresses_json="[]",
        cc_addresses_json="[]",
        subject=subject,
        direction="INBOUND",
        body_plain=OFFER_BODY,
        body_truncated=False,
        has_html=False,
        attachments_json="[]",
    )


@pytest.fixture()
def pg_session_factory():
    engine = create_engine(TEST_POSTGRES_URL, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    with engine.begin() as conn:
        conn.execute(
            delete(GmailMessageAnalysisRecord).where(
                GmailMessageAnalysisRecord.account_key == ACCOUNT
            )
        )
        conn.execute(delete(ResponseDraftRecord).where(ResponseDraftRecord.account_key == ACCOUNT))
        conn.execute(delete(GmailMessageRecord).where(GmailMessageRecord.account_key == ACCOUNT))
        conn.execute(delete(GmailThreadRecord).where(GmailThreadRecord.account_key == ACCOUNT))
    engine.dispose()


class TestPostgresCommitOrderWatermarkRace:
    def test_lower_id_message_committed_after_higher_id_message_is_still_discovered(
        self, pg_session_factory
    ):
        setup_db = pg_session_factory()
        thread = GmailThreadRecord(
            thread_key="pg-watermark-race-thread", subject="subj", account_key=ACCOUNT
        )
        setup_db.add(thread)
        setup_db.commit()
        thread_id = thread.id
        setup_db.close()

        inserted_event = threading.Event()
        release_event = threading.Event()
        holder_state: dict[str, object] = {}
        holder_errors: list[BaseException] = []

        def _hold_message_a_open_uncommitted():
            # A genuinely independent connection: INSERT (which allocates
            # this row's sequence-based `id` immediately -- PostgreSQL
            # sequences are never transactional) but deliberately never
            # commits until told to, so its `id` stays lower than B's
            # while B's own insert+commit happens entirely first.
            session = pg_session_factory()
            try:
                message_a = _build_message(thread_id=thread_id, uid=1, subject="A")
                session.add(message_a)
                session.flush()  # allocates the sequence id, no commit
                holder_state["message_a_id"] = message_a.id
                inserted_event.set()
                if not release_event.wait(timeout=20):
                    raise AssertionError("release_event was never set -- test setup stalled")
                session.commit()
            except BaseException as exc:  # noqa: BLE001
                holder_errors.append(exc)
                session.rollback()
            finally:
                session.close()

        holder_thread = threading.Thread(target=_hold_message_a_open_uncommitted)
        holder_thread.start()
        try:
            assert inserted_event.wait(timeout=20), "message A's insert never happened"

            # B: inserted AND committed strictly AFTER A's own insert
            # statement already ran (A's sequence value is reserved), but
            # while A's transaction is still open -- B gets a HIGHER id
            # and becomes visible to other connections FIRST.
            session_b = pg_session_factory()
            try:
                message_b = _build_message(thread_id=thread_id, uid=2, subject="B")
                session_b.add(message_b)
                session_b.commit()
                message_b_id = message_b.id
            finally:
                session_b.close()

            assert holder_state["message_a_id"] < message_b_id, (
                "test setup invariant violated: A must have the LOWER id"
            )

            # First scan: under READ COMMITTED, A is still invisible
            # (uncommitted) -- automation sees and fully processes only B.
            scan_db = pg_session_factory()
            try:
                first = asyncio.run(
                    prepare_gmail_response_drafts(
                        scan_db, account_key=ACCOUNT, settings=_settings()
                    )
                )
            finally:
                scan_db.close()
            assert first["counters"]["scanned"] == 1
            assert first["items"][0]["gmail_message_id"] == message_b_id
            assert first["items"][0]["status"] == "ok"

            # Now let A commit -- it becomes visible only AFTER B was
            # already scanned and marked processed.
            release_event.set()
            holder_thread.join(timeout=20)
        finally:
            if holder_thread.is_alive():
                release_event.set()
                holder_thread.join(timeout=20)

        assert not holder_thread.is_alive(), "the holder thread did not terminate"
        assert holder_errors == [], f"holder thread raised: {holder_errors}"

        # Required invariant (AUD-004): A must still be discovered and
        # processed on the very next scan -- never permanently skipped
        # merely because B (a higher id) was already scanned first.
        verify_db = pg_session_factory()
        try:
            second = asyncio.run(
                prepare_gmail_response_drafts(verify_db, account_key=ACCOUNT, settings=_settings())
            )
        finally:
            verify_db.close()
        assert second["counters"]["scanned"] == 1
        assert second["items"][0]["gmail_message_id"] == holder_state["message_a_id"]
        assert second["items"][0]["status"] == "ok"

        # A third scan finds nothing new left -- both messages processed
        # exactly once, no duplicate side effects from the two-phase scan.
        final_db = pg_session_factory()
        try:
            third = asyncio.run(
                prepare_gmail_response_drafts(final_db, account_key=ACCOUNT, settings=_settings())
            )
            assert third["counters"]["scanned"] == 0
            assert (
                final_db.query(GmailMessageAnalysisRecord)
                .filter(GmailMessageAnalysisRecord.account_key == ACCOUNT)
                .count()
                == 2
            )
            assert (
                final_db.query(ResponseDraftRecord)
                .filter(ResponseDraftRecord.account_key == ACCOUNT)
                .count()
                == 2
            )
        finally:
            final_db.close()
