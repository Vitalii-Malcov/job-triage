"""HARD-010 (adversarial hardening r1) + HARD-008 (Codex master review):
focused PostgreSQL integration tests for
`app.db.response_draft_approval_repository`'s send-claim CAS machinery
under genuine concurrency against a REAL PostgreSQL server -- not just
SQLite.

HARD-010's original scope: `claim_send_attempt`'s UNIQUE-constraint-backed
"first attempt wins" CAS holds "at most one winner" under genuine
concurrency. This closed a gap identified during that hardening pass's
test-quality audit: `claim_send_attempt`/`begin_transmission` (the
literal mechanism preventing a response-draft/follow-up email from being
sent twice under concurrent dispatch) was proven only against SQLite's
single-writer lock in
tests/test_response_draft_send_service.py::TestDoubleSendRetryConcurrency,
unlike the schedule-claim CAS
(tests/integration/test_scheduler_postgres_concurrency.py) and the
gmail-watermark commit-order race
(tests/integration/test_gmail_watermark_postgres_concurrency.py), which
both already had dedicated real-PostgreSQL proofs. See
docs/ADVERSARIAL_HARDENING_REPORT.md HARD-010.

HARD-008's added scope (Codex master review): the `send_attempted`
CAS/reconciliation model added to close the "successful SMTP + failed
SENT-commit leaves the row PENDING forever" gap (see
`app.services.response_draft_send`'s module docstring and
`app.db.models.ResponseDraftSendRecord.send_attempted`'s own docstring)
against real PostgreSQL: `begin_transmission`'s own CAS under
concurrency, a crash before `send_attempted` is set (safely reclaimed),
a crash after `send_attempted` is set but before the provider is ever
called (reconciled to UNCERTAIN, never a second provider call), and a
successful send whose `mark_send_sent` commit then fails (also
reconciled to UNCERTAIN on the next attempt, never auto-retried). Real
SMTP is never contacted anywhere in this module -- only the same
`FakeOutboundProvider` pattern the SQLite unit tests use.

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
    begin_transmission,
    claim_send_attempt,
    get_send_for_draft,
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


class _FakePostgresOutboundProvider:
    """Same pattern as tests/test_response_draft_send_service.py's
    FakeOutboundProvider -- never contacts real SMTP anywhere in this
    module, per this module's docstring and the HARD-008 task's explicit
    "never contact real SMTP" instruction.
    """

    def __init__(self, *, provider_message_id: str | None = "pg-msg-1"):
        self.provider_message_id = provider_message_id
        self.call_count = 0
        self.sent_messages: list = []

    def send(self, message):
        self.call_count += 1
        self.sent_messages.append(message)
        return OutboundSendResult(provider_message_id=self.provider_message_id)


class TestRealBeginTransmissionCasUnderConcurrency:
    """HARD-008: `begin_transmission`'s own `send_attempted: False -> True`
    CAS -- the actual exclusivity gate for calling the outbound provider
    -- holds "at most one winner" under genuine PostgreSQL concurrency,
    independent of `claim_send_attempt`'s own CAS (already proven above).
    """

    def test_only_one_thread_ever_wins_begin_transmission(self, pg_session_factory):
        setup_db = pg_session_factory()
        try:
            _seed_job(setup_db)
            draft_id, msg_id, approval_id = _seed_and_approve_draft(setup_db)
            record, claimed = claim_send_attempt(
                setup_db,
                account_key=ACCOUNT,
                response_draft_id=draft_id,
                gmail_message_id=msg_id,
                approval_id=approval_id,
            )
            assert claimed is True
            record_id = record.id
        finally:
            setup_db.close()

        barrier = threading.Barrier(5)
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            session = pg_session_factory()
            try:
                row = session.get(ResponseDraftSendRecord, record_id)
                barrier.wait(timeout=5)
                won = begin_transmission(session, row)
                with lock:
                    results.append(won)
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert len(results) == 5
        assert results.count(True) == 1
        assert results.count(False) == 4

        verify = pg_session_factory()
        try:
            record = get_send_for_draft(verify, ACCOUNT, draft_id)
            assert record.send_attempted is True
            assert record.status == "PENDING"
        finally:
            verify.close()


class TestRealCrashBeforeSendAttemptedIsSafelyReclaimed:
    """HARD-008 invariant 1, real PostgreSQL: a PENDING row with
    `send_attempted=False` (a bare `claim_send_attempt` INSERT, as if a
    prior process crashed before ever reaching `begin_transmission`) is
    safely reclaimed by a later `send_response_draft` call -- it actually
    sends, exactly once.
    """

    def test_reclaims_and_sends_successfully(self, pg_session_factory):
        db = pg_session_factory()
        try:
            _seed_job(db)
            draft_id, msg_id, approval_id = _seed_and_approve_draft(db)

            claim_send_attempt(
                db,
                account_key=ACCOUNT,
                response_draft_id=draft_id,
                gmail_message_id=msg_id,
                approval_id=approval_id,
            )

            provider = _FakePostgresOutboundProvider()
            record = send_response_draft(db, ACCOUNT, draft_id, provider)

            assert record.status == "SENT"
            assert record.send_attempted is True
            assert provider.call_count == 1
        finally:
            db.close()


class TestRealCrashAfterSendAttemptedBeforeSmtpReconciles:
    """HARD-008 invariants 3/4, real PostgreSQL: a PENDING row with
    `send_attempted=True` but the outbound provider NEVER actually
    called (simulating a crash in the narrow window between
    `begin_transmission`'s commit and the `provider.send()` call) is
    reconciled to UNCERTAIN on the next attempt -- never retried
    automatically, and the provider is never called for that reconciling
    attempt either (no duplicate/second send).
    """

    def test_reconciles_to_uncertain_without_calling_provider_again(self, pg_session_factory):
        db = pg_session_factory()
        try:
            _seed_job(db)
            draft_id, msg_id, approval_id = _seed_and_approve_draft(db)

            record, claimed = claim_send_attempt(
                db,
                account_key=ACCOUNT,
                response_draft_id=draft_id,
                gmail_message_id=msg_id,
                approval_id=approval_id,
            )
            assert claimed is True
            won = begin_transmission(db, record)
            assert won is True
            # Simulate the crash: nothing else happens on this "process"
            # -- the provider is never called, mark_send_sent never runs.
        finally:
            db.close()

        recovery_db = pg_session_factory()
        try:
            provider = _FakePostgresOutboundProvider()
            with pytest.raises(ResponseDraftSendOutcomeUncertainError):
                send_response_draft(recovery_db, ACCOUNT, draft_id, provider)

            assert provider.call_count == 0

            reconciled = get_send_for_draft(recovery_db, ACCOUNT, draft_id)
            assert reconciled.status == "UNCERTAIN"

            # Terminal: a further attempt is refused the same way, still
            # without ever calling the provider.
            provider_again = _FakePostgresOutboundProvider()
            with pytest.raises(ResponseDraftSendOutcomeUncertainError):
                send_response_draft(recovery_db, ACCOUNT, draft_id, provider_again)
            assert provider_again.call_count == 0
        finally:
            recovery_db.close()


class TestRealSmtpSuccessMarkSentFailureReconciles:
    """HARD-008 invariant 6, real PostgreSQL: the outbound provider
    SUCCEEDS (message genuinely transmitted) but the local commit
    recording SENT then fails (`mark_send_sent` monkeypatched to raise,
    simulating a DB connection drop at that exact instant). The row is
    left PENDING with `send_attempted=True`; a LATER attempt reconciles
    it to UNCERTAIN and never calls the provider again -- no automated
    duplicate send is ever possible.
    """

    def test_stranded_row_reconciles_and_never_double_sends(self, pg_session_factory, monkeypatch):
        db = pg_session_factory()
        try:
            _seed_job(db)
            draft_id, msg_id, approval_id = _seed_and_approve_draft(db)

            def _mark_send_sent_raises(*args, **kwargs):
                raise RuntimeError("simulated DB connection drop during the SENT commit")

            monkeypatch.setattr(
                "app.services.response_draft_send.mark_send_sent", _mark_send_sent_raises
            )

            provider = _FakePostgresOutboundProvider()
            with pytest.raises(RuntimeError):
                send_response_draft(db, ACCOUNT, draft_id, provider)

            # The message WAS actually transmitted.
            assert provider.call_count == 1
            monkeypatch.undo()

            stranded = get_send_for_draft(db, ACCOUNT, draft_id)
            assert stranded.status == "PENDING"
            assert stranded.send_attempted is True
        finally:
            db.close()

        recovery_db = pg_session_factory()
        try:
            provider_second_attempt = _FakePostgresOutboundProvider()
            with pytest.raises(ResponseDraftSendOutcomeUncertainError):
                send_response_draft(recovery_db, ACCOUNT, draft_id, provider_second_attempt)

            # No duplicate email.
            assert provider_second_attempt.call_count == 0

            reconciled = get_send_for_draft(recovery_db, ACCOUNT, draft_id)
            assert reconciled.status == "UNCERTAIN"
        finally:
            recovery_db.close()
