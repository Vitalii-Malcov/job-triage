"""HARD-010 (adversarial hardening r1): a focused PostgreSQL integration
test proving `app.db.response_draft_approval_repository.claim_send_attempt`'s
UNIQUE-constraint-backed "first attempt wins" CAS holds "at most one
winner" under genuine concurrency against a REAL PostgreSQL server --
not just SQLite.

This closes a gap identified during this hardening pass's test-quality
audit: `claim_send_attempt`/`begin_transmission` (the literal mechanism
preventing a response-draft/follow-up email from being sent twice under
concurrent dispatch) is proven only against SQLite's single-writer lock
in tests/test_response_draft_send_service.py::TestDoubleSendRetryConcurrency
today, unlike the schedule-claim CAS
(tests/integration/test_scheduler_postgres_concurrency.py) and the
gmail-watermark commit-order race
(tests/integration/test_gmail_watermark_postgres_concurrency.py), which
both already have dedicated real-PostgreSQL proofs. See
docs/ADVERSARIAL_HARDENING_REPORT.md HARD-010.

**Local execution:** skipped automatically unless `TEST_POSTGRES_URL` is
set (e.g. `postgresql+psycopg://user:password@localhost:5432/dbname`) --
this project's default dev/test setup is SQLite-only. If you do set it
locally, run `alembic upgrade head` (with `DATABASE_URL` pointed at that
same database) first -- this module has no `Base.metadata.create_all`
fallback, so the schema must already exist via the real migration chain,
exactly like this directory's two existing PostgreSQL integration tests.

**CI:** wired into `.github/workflows/ci.yml`'s existing `scheduler-postgres`
job (same real `postgres:16` service container the other two PostgreSQL
integration tests already use) -- runs on every push/PR, never silently
skipped there.

This module never duplicates the CAS SQL itself -- it calls the REAL
`claim_send_attempt`, synchronized with a `threading.Barrier`, exactly
like the two existing PostgreSQL integration tests in this directory.
Self-contained seed helpers (duplicated from
tests/test_response_draft_send_service.py's own, per the same convention
those two existing modules already follow) rather than importing from a
SQLite-only unit test module.
"""

import os
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.db.gmail_analysis_repository import GmailMessageAnalysisRecord
from app.db.gmail_repository import GmailMessageRecord, upsert_message
from app.db.models import JobRecord
from app.db.response_draft_approval_repository import (
    ResponseDraftApprovalRecord,
    ResponseDraftSendRecord,
    claim_send_attempt,
    get_send_for_draft,
)
from app.db.response_draft_repository import ResponseDraftRecord
from app.providers.email.base import ParsedGmailMessage
from app.services.gmail_message_analysis import analyze_gmail_message
from app.services.response_draft import generate_response_draft_for_message
from app.services.response_draft_send import approve_or_reject_response_draft

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it once this module is wired in).",
)

ACCOUNT = "me@postgres-integration.example.com"


@pytest.fixture()
def pg_session_factory():
    engine = create_engine(TEST_POSTGRES_URL, connect_args={}, pool_pre_ping=True)
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
        cleanup.execute(delete(JobRecord).where(JobRecord.fingerprint.like("hard-010-%")))
        cleanup.commit()
    finally:
        cleanup.close()
    yield session_factory
    engine.dispose()


def _seed_job(db) -> int:
    job = JobRecord(
        fingerprint="hard-010-concurrent-send",
        source="bundesagentur",
        title="Backend Engineer",
        company="RaceCo",
        location="Berlin",
        url="https://example.com/jobs/hard-010",
        description="",
        score=80,
        recommendation="APPLY",
        status="APPLIED",
    )
    db.add(job)
    db.commit()
    return job.id


def _seed_and_approve_draft(db) -> int:
    parsed = ParsedGmailMessage(
        account_key=ACCOUNT,
        mailbox="INBOX",
        uid=1,
        uid_validity=100,
        message_id_header="<hard-010@acme.example.com>",
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


class TestRealConcurrentSendAttemptClaim:
    def test_only_one_thread_ever_wins_the_send_claim(self, pg_session_factory):
        setup_db = pg_session_factory()
        try:
            _seed_job(setup_db)
            draft_id, msg_id, approval_id = _seed_and_approve_draft(setup_db)
        finally:
            setup_db.close()

        barrier = threading.Barrier(5)
        results: list[tuple] = []
        lock = threading.Lock()

        def worker() -> None:
            session = pg_session_factory()
            try:
                barrier.wait(timeout=5)
                record, claimed = claim_send_attempt(
                    session,
                    account_key=ACCOUNT,
                    response_draft_id=draft_id,
                    gmail_message_id=msg_id,
                    approval_id=approval_id,
                )
                with lock:
                    results.append((record.id, claimed))
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert len(results) == 5
        winners = [r for r in results if r[1] is True]
        losers = [r for r in results if r[1] is False]
        # Exactly one thread's INSERT durably wins -- PostgreSQL's real
        # UNIQUE constraint (uq_response_draft_sends_response_draft) is
        # the arbiter, not Python-level timing.
        assert len(winners) == 1
        assert len(losers) == 4
        # Every thread -- winner and losers alike -- observed the SAME
        # underlying row id (the losers' IntegrityError-recovery path
        # correctly re-reads and returns the winner's row, never
        # fabricates a second one).
        assert len({r[0] for r in results}) == 1

        verify = pg_session_factory()
        try:
            record = get_send_for_draft(verify, ACCOUNT, draft_id)
            assert record is not None
            assert record.status == "PENDING"
            assert record.attempt_count == 1
        finally:
            verify.close()
