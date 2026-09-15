"""HARD-008-RR1 (Codex targeted re-review): proves the fail-closed
legacy-PENDING backfill in
`alembic/versions/a1b2c3d4e5f6_response_draft_send_attempted.py` against
REAL PostgreSQL, not just the SQLite reproduction in
`tests/test_migrations.py`.

**The scenario this closes.** Before this migration existed, a `PENDING`
`response_draft_sends` row was NOT provably pre-transmission: the outbound
provider could have already succeeded, with only the subsequent
`mark_send_sent()` commit failing (a DB connection drop or process kill at
that exact instant), leaving the row stranded `PENDING` forever. The
re-review's finding was that the ORIGINAL version of this migration
backfilled such a row to `send_attempted=False` ("provably
pre-transmission"), which would let the new HARD-008 recovery logic
(`app.services.response_draft_send._resolve_existing_send_record`) hand it
to a later request to silently RE-SEND -- reproducing the exact
duplicate-send failure HARD-008 exists to prevent, except now triggered by
the very migration meant to fix it.

**What this module proves, end to end, against real PostgreSQL:**
- A real, already-approved response draft's `response_draft_sends` row,
  inserted in the exact shape the pre-a1b2c3d4e5f6 schema had (no
  `send_attempted` column) with `status='PENDING'` -- simulating "the
  provider actually transmitted the email, but the process crashed before
  the SENT-recording commit landed" from an OLD code version -- comes out
  of the migration as `status='UNCERTAIN'`, `send_attempted=True`.
- The REAL `send_response_draft` call path (not just a raw column check)
  refuses to resend it: raises `ResponseDraftSendOutcomeUncertainError`,
  and the outbound provider is NEVER invoked.
- Both `upgrade` and `downgrade` are exercised against this same real
  server.

**Local execution:** skipped automatically unless `TEST_POSTGRES_URL` is
set, exactly like every other module in this directory. Unlike the other
three PostgreSQL integration tests here, this module ITSELF drives Alembic
`downgrade`/`upgrade` against that database (down to `c7d3f9a1e5b8` and
back to `head`) rather than assuming the schema is already at `head` --
it always restores `head` in a `finally` block, including on failure, so
it never leaves the shared CI database on a stale schema for the other
PostgreSQL integration tests that run in the same job.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import sessionmaker

from app.db.gmail_analysis_repository import GmailMessageAnalysisRecord
from app.db.gmail_repository import GmailMessageRecord, upsert_message
from app.db.models import JobRecord
from app.db.response_draft_approval_repository import (
    ResponseDraftApprovalRecord,
    ResponseDraftSendRecord,
)
from app.db.response_draft_repository import ResponseDraftRecord
from app.providers.email.base import ParsedGmailMessage
from app.providers.email.outbound_base import OutboundSendResult
from app.services.gmail_message_analysis import analyze_gmail_message
from app.services.response_draft import generate_response_draft_for_message
from app.services.response_draft_send import (
    ResponseDraftSendOutcomeUncertainError,
    approve_or_reject_response_draft,
    send_response_draft,
)

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

ACCOUNT = "me@hard008-rr1-postgres-migration.example.com"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_POSTGRES_URL)
    return cfg


@pytest.fixture()
def pg_session_factory():
    engine = create_engine(TEST_POSTGRES_URL, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    cleanup = session_factory()
    try:
        cleanup.execute(
            delete(ResponseDraftSendRecord).where(ResponseDraftSendRecord.account_key == ACCOUNT)
        )
        cleanup.execute(
            delete(ResponseDraftApprovalRecord).where(
                ResponseDraftApprovalRecord.account_key == ACCOUNT
            )
        )
        cleanup.execute(
            delete(ResponseDraftRecord).where(ResponseDraftRecord.account_key == ACCOUNT)
        )
        cleanup.execute(
            delete(GmailMessageAnalysisRecord).where(
                GmailMessageAnalysisRecord.account_key == ACCOUNT
            )
        )
        cleanup.execute(delete(GmailMessageRecord).where(GmailMessageRecord.account_key == ACCOUNT))
        cleanup.execute(delete(JobRecord).where(JobRecord.fingerprint.like("hard-008-rr1-%")))
        cleanup.commit()
    finally:
        cleanup.close()
    yield session_factory
    engine.dispose()


def _seed_job(db) -> int:
    job = JobRecord(
        fingerprint="hard-008-rr1-legacy-pending-migration",
        source="bundesagentur",
        title="Backend Engineer",
        company="RaceCo",
        location="Berlin",
        url="https://example.com/jobs/hard-008-rr1",
        description="",
        score=80,
        recommendation="APPLY",
        status="APPLIED",
    )
    db.add(job)
    db.commit()
    return job.id


def _seed_and_approve_draft(db) -> tuple[int, int, int]:
    parsed = ParsedGmailMessage(
        account_key=ACCOUNT,
        mailbox="INBOX",
        uid=1,
        uid_validity=100,
        message_id_header="<hard-008-rr1@acme.example.com>",
        in_reply_to=None,
        references=(),
        from_address="hr@acme.example.com",
        from_display_name="Recruiter",
        to_addresses=(ACCOUNT,),
        cc_addresses=(),
        subject="Offer",
        sent_at=datetime.now(UTC),
        direction="INBOUND",
        body_plain="We are pleased to offer you the position of Backend Engineer at RaceCo.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    msg, _created = upsert_message(db, parsed)
    db.commit()

    analyze_gmail_message(db, ACCOUNT, msg.id)
    draft, _created = generate_response_draft_for_message(db, ACCOUNT, msg.id)
    approval = approve_or_reject_response_draft(db, ACCOUNT, draft.id, "APPROVED", None)
    return draft.id, msg.id, approval.id


class _FakeOutboundProvider:
    """Never contacts real SMTP -- same pattern as every other PostgreSQL
    integration test in this directory."""

    def __init__(self) -> None:
        self.call_count = 0

    def send(self, message):
        self.call_count += 1
        return OutboundSendResult(provider_message_id="should-never-be-sent")


class TestLegacyPendingRowMigratesFailClosed:
    def test_legacy_pending_row_becomes_uncertain_and_is_never_auto_resent(
        self, pg_session_factory
    ):
        cfg = _alembic_config()

        # Seed a real, fully-approved draft while the schema is at head
        # (send_attempted already exists here, but no response_draft_sends
        # row is created yet) so response_draft_id/approval_id are real,
        # valid foreign keys throughout the downgrade/upgrade cycle below
        # -- only response_draft_sends.send_attempted itself is added and
        # removed, never response_drafts/response_draft_approvals.
        setup_db = pg_session_factory()
        try:
            _seed_job(setup_db)
            draft_id, _msg_id, approval_id = _seed_and_approve_draft(setup_db)
        finally:
            setup_db.close()

        try:
            # Drop send_attempted, then insert a response_draft_sends row
            # shaped EXACTLY like the pre-a1b2c3d4e5f6 schema: no
            # send_attempted column at all, status='PENDING' -- the OLD
            # code's own representation of "provider may have already
            # succeeded, but the SENT-recording commit never landed".
            downgrade(cfg, "c7d3f9a1e5b8")

            engine = create_engine(TEST_POSTGRES_URL)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO response_draft_sends (
                            account_key, response_draft_id, approval_id, gmail_message_id,
                            status, attempt_count, created_at, updated_at
                        ) VALUES (
                            :account_key, :response_draft_id, :approval_id, :gmail_message_id,
                            'PENDING', 1, now(), now()
                        )
                        """
                    ),
                    {
                        "account_key": ACCOUNT,
                        "response_draft_id": draft_id,
                        "approval_id": approval_id,
                        "gmail_message_id": _msg_id,
                    },
                )
            engine.dispose()

            upgrade(cfg, "head")

            verify_engine = create_engine(TEST_POSTGRES_URL)
            with verify_engine.connect() as connection:
                row = (
                    connection.execute(
                        text(
                            "SELECT status, send_attempted, last_error FROM response_draft_sends "
                            "WHERE account_key = :account_key"
                        ),
                        {"account_key": ACCOUNT},
                    )
                    .mappings()
                    .one()
                )
            verify_engine.dispose()

            assert row["status"] == "UNCERTAIN"
            assert row["send_attempted"] is True
            assert row["last_error"] and "HARD-008-RR1" in row["last_error"]

            # End-to-end proof, not just a column check: the real send
            # path refuses to resend, and the provider is NEVER called.
            recovery_db = pg_session_factory()
            try:
                provider = _FakeOutboundProvider()
                with pytest.raises(ResponseDraftSendOutcomeUncertainError):
                    send_response_draft(recovery_db, ACCOUNT, draft_id, provider)
                assert provider.call_count == 0
            finally:
                recovery_db.close()
        finally:
            # Always restore head, even on failure -- the other
            # PostgreSQL integration tests in this CI job assume the
            # schema is already at head and must never see this
            # module's own downgrade left in place.
            upgrade(cfg, "head")
