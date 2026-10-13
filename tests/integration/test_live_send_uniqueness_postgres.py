"""Stage 12: real-PostgreSQL proof of the per-target live-send partial unique
indexes (migration a7c3e9f1b2d4):

- `uq_response_draft_sends_live_per_message`: two DIFFERENT approved draft
  revisions for the SAME inbound message, claimed concurrently from many
  threads -> exactly one PENDING claim in total; every other contender
  either gets the winner's own row back (same revision) or
  `ResponseDraftLiveSendExistsError` (other revision).
- `uq_follow_up_sends_live_per_anchor`: the same for two follow-up
  proposals sharing one correspondence anchor.
- A FAILED -> PENDING retry racing a sibling's fresh claim: at most one of
  them ends up live.
- S12-001: the pending queue's current-revision selection SQL, with the
  current draft inserted BEFORE a stale one (stale.id > current.id).
- S12-002: a failure creating the SECOND index rolls the whole migration
  back (transactional DDL) and a plain retry succeeds.

SQLite's single-writer lock serializes these trivially; PostgreSQL's MVCC is
where a check-then-act would genuinely race, so this is the real evidence.
The calls are the REAL repository functions, synchronized with a
`threading.Barrier`. No SMTP/IMAP anywhere.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first (no create_all fallback).
**CI:** part of the `scheduler-postgres` job's zero-skip gate.
"""

import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from alembic.ddl.postgresql import PostgresqlImpl
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.agents.response_draft_generator import RESPONSE_DRAFT_GENERATOR_VERSION
from app.db.follow_up_approval_repository import (
    FollowUpLiveSendExistsError,
)
from app.db.follow_up_approval_repository import (
    claim_send_attempt as claim_follow_up_send,
)
from app.db.follow_up_approval_repository import (
    create_approval as create_follow_up_approval,
)
from app.db.follow_up_repository import get_or_create_follow_up_proposal
from app.db.gmail_repository import upsert_message
from app.db.models import (
    LIVE_SEND_STATUSES,
    FollowUpApprovalRecord,
    FollowUpProposalRecord,
    FollowUpSendRecord,
    GmailMessageAnalysisRecord,
    GmailMessageIdClaimRecord,
    GmailMessageRecord,
    GmailThreadRecord,
    JobRecord,
    ResponseDraftApprovalRecord,
    ResponseDraftRecord,
    ResponseDraftSendRecord,
)
from app.db.response_draft_approval_repository import (
    ResponseDraftLiveSendExistsError,
    claim_send_attempt,
    create_approval,
    list_pending_response_drafts,
    retry_send_attempt,
)
from app.db.response_draft_repository import get_or_create_response_draft
from app.providers.email.base import ParsedGmailMessage
from app.services.gmail_message_analysis import analyze_gmail_message

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

ACCOUNT = "me@live-send-uniqueness-postgres.example.com"
JOB_FINGERPRINT = "stage-12-live-send-uniqueness"
THREADS = 8


@pytest.fixture()
def pg_session_factory():
    engine = create_engine(TEST_POSTGRES_URL, pool_pre_ping=True, pool_size=THREADS + 2)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _cleanup() -> None:
        with session_factory() as db:
            for model in (
                ResponseDraftSendRecord,
                ResponseDraftApprovalRecord,
                ResponseDraftRecord,
                GmailMessageAnalysisRecord,
                FollowUpSendRecord,
                FollowUpApprovalRecord,
                FollowUpProposalRecord,
                GmailMessageIdClaimRecord,
                GmailMessageRecord,
                GmailThreadRecord,
            ):
                db.execute(delete(model).where(model.account_key == ACCOUNT))
            db.execute(delete(JobRecord).where(JobRecord.fingerprint == JOB_FINGERPRINT))
            db.commit()

    _cleanup()
    yield session_factory
    _cleanup()
    engine.dispose()


def _seed_message(db) -> GmailMessageRecord:
    record, _created = upsert_message(
        db,
        ParsedGmailMessage(
            account_key=ACCOUNT,
            mailbox="INBOX",
            uid=1,
            uid_validity=100,
            message_id_header="<stage12-live@acme.example.com>",
            in_reply_to=None,
            references=(),
            from_address="hr@acme.example.com",
            from_display_name="Recruiter",
            to_addresses=(ACCOUNT,),
            cc_addresses=(),
            subject="Offer",
            sent_at=datetime.now(UTC),
            direction="INBOUND",
            body_plain="We are pleased to offer you the position.",
            body_truncated=False,
            has_html=False,
            attachments=(),
        ),
    )
    db.commit()
    return record


def _two_approved_draft_revisions(db, message_id: int) -> list[tuple[int, int]]:
    """Two revisions of a draft for ONE message (different candidate profile
    versions), each with its own APPROVED decision. Built through the real
    repositories, without touching the shared candidate-profile singleton."""
    revisions = []
    for profile_version in (1, 2):
        draft, _created = get_or_create_response_draft(
            db,
            account_key=ACCOUNT,
            gmail_message_id=message_id,
            analysis_id=1,
            analysis_version=1,
            candidate_profile_version=profile_version,
            matched_job_id=None,
            classification="OFFER",
            status="PROPOSED",
            reason=None,
            subject="Re: Offer",
            body=f"Revision {profile_version}",
            language="en",
            missing_fields=(),
            provider="deterministic_template",
            generator_version="v2",
        )
        approval, _created = create_approval(
            db,
            account_key=ACCOUNT,
            response_draft_id=draft.id,
            gmail_message_id=message_id,
            decision="APPROVED",
            decision_note=None,
            pinned_subject=draft.subject,
            pinned_body=draft.body,
        )
        revisions.append((draft.id, approval.id))
    return revisions


def _race(fn_by_index) -> list:
    barrier = threading.Barrier(THREADS)
    outcomes: list = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait(timeout=10)
        try:
            outcome = fn_by_index(index)
        except Exception as exc:  # recorded and asserted on by type
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(outcomes) == THREADS
    return outcomes


class TestResponseDraftLiveSendPerMessage:
    def test_concurrent_claims_for_two_revisions_have_exactly_one_winner(self, pg_session_factory):
        with pg_session_factory() as db:
            message = _seed_message(db)
            message_id = message.id
            revisions = _two_approved_draft_revisions(db, message_id)

        def claim(index: int):
            draft_id, approval_id = revisions[index % 2]
            with pg_session_factory() as session:
                record, claimed = claim_send_attempt(
                    session,
                    account_key=ACCOUNT,
                    response_draft_id=draft_id,
                    gmail_message_id=message_id,
                    approval_id=approval_id,
                )
                return (record.response_draft_id, claimed)

        outcomes = _race(claim)
        unexpected = [
            o
            for o in outcomes
            if isinstance(o, Exception) and not isinstance(o, ResponseDraftLiveSendExistsError)
        ]
        assert unexpected == []
        winners = [o for o in outcomes if isinstance(o, tuple) and o[1] is True]
        assert len(winners) == 1
        winning_draft = winners[0][0]
        # Same-revision losers see the winner's row; other-revision losers
        # are refused by the per-message index -- never a second claim.
        for outcome in outcomes:
            if isinstance(outcome, tuple):
                assert outcome[0] == winning_draft

        with pg_session_factory() as verify:
            live = verify.scalars(
                select(ResponseDraftSendRecord).where(
                    ResponseDraftSendRecord.account_key == ACCOUNT,
                    ResponseDraftSendRecord.gmail_message_id == message_id,
                    ResponseDraftSendRecord.status.in_(LIVE_SEND_STATUSES),
                )
            ).all()
            assert [row.response_draft_id for row in live] == [winning_draft]

    def test_retry_racing_a_sibling_claim_leaves_at_most_one_live_row(self, pg_session_factory):
        with pg_session_factory() as db:
            message_id = _seed_message(db).id
            (failed_draft, failed_approval), (fresh_draft, fresh_approval) = (
                _two_approved_draft_revisions(db, message_id)
            )
            failed_row, _ = claim_send_attempt(
                db,
                account_key=ACCOUNT,
                response_draft_id=failed_draft,
                gmail_message_id=message_id,
                approval_id=failed_approval,
            )
            failed_row.status = "FAILED"
            db.commit()
            failed_row_id = failed_row.id

        def contend(index: int):
            with pg_session_factory() as session:
                if index % 2 == 0:
                    row = session.get(ResponseDraftSendRecord, failed_row_id)
                    return ("retry", retry_send_attempt(session, row))
                _record, claimed = claim_send_attempt(
                    session,
                    account_key=ACCOUNT,
                    response_draft_id=fresh_draft,
                    gmail_message_id=message_id,
                    approval_id=fresh_approval,
                )
                return ("claim", claimed)

        outcomes = _race(contend)
        unexpected = [
            o
            for o in outcomes
            if isinstance(o, Exception) and not isinstance(o, ResponseDraftLiveSendExistsError)
        ]
        assert unexpected == []
        wins = [o for o in outcomes if isinstance(o, tuple) and o[1] is True]
        assert len(wins) == 1

        with pg_session_factory() as verify:
            live = verify.scalars(
                select(ResponseDraftSendRecord).where(
                    ResponseDraftSendRecord.account_key == ACCOUNT,
                    ResponseDraftSendRecord.gmail_message_id == message_id,
                    ResponseDraftSendRecord.status.in_(LIVE_SEND_STATUSES),
                )
            ).all()
            assert len(live) == 1


class TestFollowUpLiveSendPerAnchor:
    def test_concurrent_claims_for_two_proposals_have_exactly_one_winner(self, pg_session_factory):
        with pg_session_factory() as db:
            job = JobRecord(
                fingerprint=JOB_FINGERPRINT,
                source="bundesagentur",
                title="Backend Engineer",
                company="RaceCo",
                location="Berlin",
                url="https://example.com/jobs/stage12",
                description="",
                score=80,
                recommendation="APPLY",
                status="APPLIED",
            )
            db.add(job)
            db.commit()
            anchor = _seed_message(db)
            proposals = []
            for fingerprint in ("fp-a", "fp-b"):
                proposal, _created = get_or_create_follow_up_proposal(
                    db,
                    account_key=ACCOUNT,
                    job_id=job.id,
                    gmail_thread_id=anchor.thread_id,
                    anchor_gmail_message_id=anchor.id,
                    eligibility_reason="due",
                    due_at=datetime.now(UTC),
                    subject="Follow-up",
                    body=f"Body {fingerprint}",
                    language="en",
                    missing_fields=(),
                    recipient="hr@acme.example.com",
                    input_fingerprint=fingerprint,
                    provider="deterministic_template",
                    generator_version="v1",
                )
                approval, _created = create_follow_up_approval(
                    db,
                    account_key=ACCOUNT,
                    follow_up_proposal_id=proposal.id,
                    gmail_message_id=anchor.id,
                    decision="APPROVED",
                    decision_note=None,
                    pinned_subject=proposal.subject,
                    pinned_body=proposal.body,
                    pinned_recipient=proposal.recipient,
                )
                proposals.append((proposal.id, approval.id))
            anchor_id = anchor.id

        def claim(index: int):
            proposal_id, approval_id = proposals[index % 2]
            with pg_session_factory() as session:
                record, claimed = claim_follow_up_send(
                    session,
                    account_key=ACCOUNT,
                    follow_up_proposal_id=proposal_id,
                    gmail_message_id=anchor_id,
                    approval_id=approval_id,
                )
                return (record.follow_up_proposal_id, claimed)

        outcomes = _race(claim)
        unexpected = [
            o
            for o in outcomes
            if isinstance(o, Exception) and not isinstance(o, FollowUpLiveSendExistsError)
        ]
        assert unexpected == []
        winners = [o for o in outcomes if isinstance(o, tuple) and o[1] is True]
        assert len(winners) == 1

        with pg_session_factory() as verify:
            live = verify.scalars(
                select(FollowUpSendRecord).where(
                    FollowUpSendRecord.account_key == ACCOUNT,
                    FollowUpSendRecord.gmail_message_id == anchor_id,
                    FollowUpSendRecord.status.in_(LIVE_SEND_STATUSES),
                )
            ).all()
            assert [row.follow_up_proposal_id for row in live] == [winners[0][0]]


def _draft_revision(db, message_id: int, analysis_id: int, profile_version: int) -> int:
    draft, _created = get_or_create_response_draft(
        db,
        account_key=ACCOUNT,
        gmail_message_id=message_id,
        analysis_id=analysis_id,
        analysis_version=1,
        candidate_profile_version=profile_version,
        matched_job_id=None,
        classification="OFFER",
        status="PROPOSED",
        reason=None,
        subject="Re: Offer",
        body=f"Revision {profile_version}",
        language="en",
        missing_fields=(),
        provider="deterministic_template",
        generator_version=RESPONSE_DRAFT_GENERATOR_VERSION,
    )
    return draft.id


class TestPendingQueueCurrentRevisionOnPostgres:
    def test_current_draft_wins_over_a_later_inserted_stale_draft(self, pg_session_factory):
        """S12-001: the same SQL the SQLite suites prove, on PostgreSQL."""
        with pg_session_factory() as db:
            message = _seed_message(db)
            analysis, _created = analyze_gmail_message(db, ACCOUNT, message.id)
            current_id = _draft_revision(db, message.id, analysis.id, profile_version=2)
            stale_id = _draft_revision(db, message.id, analysis.id, profile_version=1)
            assert stale_id > current_id

            def listed(profile_version: int) -> list[int]:
                rows = list_pending_response_drafts(
                    db,
                    ACCOUNT,
                    current_candidate_profile_version=profile_version,
                    current_generator_version=RESPONSE_DRAFT_GENERATOR_VERSION,
                    limit=20,
                    offset=0,
                )
                return [draft.id for draft, _type, _confidence in rows]

            assert listed(2) == [current_id]
            assert listed(1) == [stale_id]
            assert listed(99) == [stale_id]  # no current revision: highest-id fallback


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "e8b1c4f7a2d9"
LIVE_SEND_INDEXES = {
    "uq_response_draft_sends_live_per_message": "response_draft_sends",
    "uq_follow_up_sends_live_per_anchor": "follow_up_sends",
}


class TestMigrationSecondIndexFailureOnPostgres:
    def test_second_index_failure_rolls_back_and_retry_succeeds(self, monkeypatch):
        """S12-002: PostgreSQL DDL is transactional, so the migration's
        SQLite-only cleanup is skipped and the failed upgrade leaves nothing
        behind. Always restores head for the other tests in this job."""
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", TEST_POSTGRES_URL)
        engine = create_engine(TEST_POSTGRES_URL, pool_pre_ping=True)

        def state() -> tuple[str, set[str]]:
            with engine.connect() as conn:
                revision = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            inspector = inspect(engine)
            names = {
                index["name"]
                for table in LIVE_SEND_INDEXES.values()
                for index in inspector.get_indexes(table)
            }
            return revision, names & set(LIVE_SEND_INDEXES)

        original = PostgresqlImpl.create_index

        def create_index(self, index, **kw):
            if index.name == "uq_follow_up_sends_live_per_anchor":
                raise sqlalchemy.exc.OperationalError("CREATE INDEX", {}, Exception("injected"))
            return original(self, index, **kw)

        try:
            downgrade(cfg, PREVIOUS_REVISION)
            with monkeypatch.context() as patched:
                patched.setattr(PostgresqlImpl, "create_index", create_index)
                with pytest.raises(sqlalchemy.exc.OperationalError):
                    upgrade(cfg, "head")
            assert state() == (PREVIOUS_REVISION, set())

            upgrade(cfg, "head")
            assert state() == ("a7c3e9f1b2d4", set(LIVE_SEND_INDEXES))
        finally:
            upgrade(cfg, "head")
            engine.dispose()
