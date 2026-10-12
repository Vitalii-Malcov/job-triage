"""Stage 12 end-to-end LOCAL integration test of the controlled Gmail
response workflow, exercising the real production components together:

    persisted Gmail messages (as the read-only IMAP sync stores them)
    -> Stage 8D automated cycle (`prepare_gmail_response_drafts`):
       Stage 7B association + classification, Stage 7C local draft
    -> human-decision queue (`get_pending_response_drafts`)
    -> human approval (`approve_or_reject_response_draft`)
    -> Stage 7E follow-up evaluation

Offline and repeatable: no IMAP, no SMTP, no HTTP, no real Gmail account.
The only things replaced are failure injections in the dedicated
failure-path tests. Nothing here ever constructs an outbound provider; the
approval step is asserted to leave no send row behind.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.gmail_repository import upsert_message
from app.db.models import (
    FollowUpProposalRecord,
    GmailMessageAnalysisRecord,
    GmailMessageRecord,
    JobRecord,
    ResponseDraftApprovalRecord,
    ResponseDraftRecord,
    ResponseDraftSendRecord,
)
from app.db.repositories import sync_job_reference_tokens
from app.providers.email.base import ParsedGmailMessage
from app.services.automation_gmail import prepare_gmail_response_drafts
from app.services.follow_up import evaluate_follow_up_for_job
from app.services.gmail_message_analysis import analyze_gmail_message
from app.services.response_draft import generate_response_draft_for_message
from app.services.response_draft_send import (
    approve_or_reject_response_draft,
    get_pending_response_drafts,
    get_response_draft_state,
)

ACCOUNT = "me@example.com"
NOW = datetime(2026, 9, 6, tzinfo=UTC)
SETTINGS = Settings(automation_gmail_process_max_per_run=50, follow_up_delay_days=7)
GLOBEX_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1234567890-S"
INITECH_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-9876543210-S"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow_e2e.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _job(db, *, uid: int, title: str, company: str, url: str) -> JobRecord:
    job = JobRecord(
        fingerprint=f"fp-{uid}",
        source="bundesagentur",
        title=title,
        company=company,
        location="Berlin",
        url=url,
        description="",
        score=80,
        recommendation="APPLY",
        status="APPLIED",
    )
    db.add(job)
    db.commit()
    sync_job_reference_tokens(db, job)
    db.commit()
    return job


def _message(
    db,
    *,
    uid: int,
    direction: str,
    body: str,
    message_id: str,
    in_reply_to: str | None = None,
    arrived: datetime,
    mailbox: str | None = None,
    sender: str = "hr@globex.example.com",
) -> GmailMessageRecord:
    outbound = direction == "OUTBOUND"
    parsed = ParsedGmailMessage(
        account_key=ACCOUNT,
        mailbox=mailbox or ("[Gmail]/Sent Mail" if outbound else "INBOX"),
        uid=uid,
        uid_validity=100,
        message_id_header=message_id,
        in_reply_to=in_reply_to,
        references=(in_reply_to,) if in_reply_to else (),
        from_address=ACCOUNT if outbound else sender,
        from_display_name=None,
        to_addresses=("hr@globex.example.com",) if outbound else (ACCOUNT,),
        cc_addresses=(),
        subject="Bewerbung" if outbound else "Re: Bewerbung",
        sent_at=arrived,
        direction=direction,
        body_plain=body,
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    record, _created = upsert_message(db, parsed)
    # As a real sync with a parseable IMAP INTERNALDATE would persist it.
    record.received_at = arrived
    record.provider_arrival_at = arrived
    record.provider_arrival_is_trusted = True
    db.commit()
    db.refresh(record)
    return record


def _seed_world(db) -> dict:
    """Two applied jobs:

    - Globex: application sent 10 days ago, recruiter replied 2 days ago
      with an offer (a reply was received -> no follow-up).
    - Initech: application sent 10 days ago, no reply (-> follow-up due).

    Plus one unrelated newsletter (UNMATCHED, nothing to respond to)."""
    globex = _job(db, uid=1, title="Backend Engineer", company="Globex", url=GLOBEX_URL)
    initech = _job(db, uid=2, title="Data Engineer", company="Initech", url=INITECH_URL)
    globex_out = _message(
        db,
        uid=1,
        direction="OUTBOUND",
        message_id="<app-globex@me.example.com>",
        body=f"Sehr geehrte Damen und Herren, anbei meine Bewerbung. {GLOBEX_URL}",
        arrived=NOW - timedelta(days=10),
    )
    globex_reply = _message(
        db,
        uid=10,
        direction="INBOUND",
        message_id="<offer@globex.example.com>",
        in_reply_to="<app-globex@me.example.com>",
        body=(
            "We are pleased to offer you the position of Backend Engineer at Globex. "
            f"Referenz-Nr: 10000-1234567890-S {GLOBEX_URL}"
        ),
        arrived=NOW - timedelta(days=2),
    )
    initech_out = _message(
        db,
        uid=2,
        direction="OUTBOUND",
        message_id="<app-initech@me.example.com>",
        body=f"Sehr geehrte Damen und Herren, anbei meine Bewerbung. {INITECH_URL}",
        arrived=NOW - timedelta(days=10),
    )
    newsletter = _message(
        db,
        uid=11,
        direction="INBOUND",
        message_id="<news@example.org>",
        body="Our weekly product newsletter. Unsubscribe at any time.",
        arrived=NOW - timedelta(days=1),
        sender="noreply@news.example.org",
    )
    return {
        "globex": globex,
        "initech": initech,
        "globex_out": globex_out,
        "globex_reply": globex_reply,
        "initech_out": initech_out,
        "newsletter": newsletter,
    }


def _run_cycle(db) -> dict:
    return asyncio.run(prepare_gmail_response_drafts(db, account_key=ACCOUNT, settings=SETTINGS))


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _drafts_for(db, message_id: int) -> list[ResponseDraftRecord]:
    return list(
        db.scalars(
            select(ResponseDraftRecord).where(ResponseDraftRecord.gmail_message_id == message_id)
        )
    )


class TestControlledResponseWorkflow:
    def test_full_local_pipeline(self, db):
        world = _seed_world(db)

        step = _run_cycle(db)
        assert step["status"] == "ok"
        counters = step["counters"]
        assert counters["scanned"] == 4
        assert counters["outbound_analysis_only"] == 2
        assert counters["draft_created"] == 1
        assert counters["no_response_recommended"] == 1
        assert counters["failed"] == 0

        # Association: deterministic, evidence-based.
        reply_analysis = db.scalar(
            select(GmailMessageAnalysisRecord).where(
                GmailMessageAnalysisRecord.gmail_message_id == world["globex_reply"].id
            )
        )
        assert reply_analysis.match_type == "APPLICATION"
        assert reply_analysis.matched_job_id == world["globex"].id
        news_analysis = db.scalar(
            select(GmailMessageAnalysisRecord).where(
                GmailMessageAnalysisRecord.gmail_message_id == world["newsletter"].id
            )
        )
        assert news_analysis.match_type == "UNMATCHED"
        assert news_analysis.matched_job_id is None

        # Drafts: only for inbound mail, never for the user's own outbound mail.
        assert _drafts_for(db, world["globex_out"].id) == []
        assert _drafts_for(db, world["initech_out"].id) == []
        (reply_draft,) = _drafts_for(db, world["globex_reply"].id)
        assert reply_draft.status == "PROPOSED"
        assert reply_draft.matched_job_id == world["globex"].id
        assert reply_draft.requires_human_review is True
        assert reply_draft.provider == "deterministic_template"
        (news_draft,) = _drafts_for(db, world["newsletter"].id)
        assert news_draft.status == "NO_RESPONSE_RECOMMENDED"

        # Human-decision queue holds exactly the one actionable draft.
        pending = get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)
        assert [p.response_draft_id for p in pending] == [reply_draft.id]
        assert pending[0].is_current_revision is True
        assert pending[0].matched_job_id == world["globex"].id

        # Human approval records a decision and nothing else.
        approval = approve_or_reject_response_draft(
            db, ACCOUNT, reply_draft.id, "APPROVED", "looks right"
        )
        assert approval.pinned_body == reply_draft.body
        state = get_response_draft_state(db, ACCOUNT, reply_draft.id)
        assert state.approval.decision == "APPROVED"
        assert state.send is None
        assert _count(db, ResponseDraftSendRecord) == 0
        assert get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0) == []

        # Follow-up: due for the unanswered application only.
        globex_fu = evaluate_follow_up_for_job(
            db, ACCOUNT, world["globex"].id, settings=SETTINGS, now=NOW
        )
        assert globex_fu.eligibility == "NOT_ELIGIBLE"
        assert globex_fu.proposal is None
        initech_fu = evaluate_follow_up_for_job(
            db, ACCOUNT, world["initech"].id, settings=SETTINGS, now=NOW
        )
        assert initech_fu.eligibility == "ELIGIBLE"
        assert initech_fu.proposal.anchor_gmail_message_id == world["initech_out"].id
        assert _count(db, FollowUpProposalRecord) == 1

    def test_second_identical_run_is_idempotent(self, db):
        world = _seed_world(db)
        _run_cycle(db)
        evaluate_follow_up_for_job(db, ACCOUNT, world["initech"].id, settings=SETTINGS, now=NOW)
        snapshot = (
            _count(db, GmailMessageAnalysisRecord),
            _count(db, ResponseDraftRecord),
            _count(db, FollowUpProposalRecord),
        )

        again = _run_cycle(db)
        assert again["counters"]["scanned"] == 0  # every message already processed

        # Re-requesting a draft for every inbound message reuses the
        # existing revision (same analysis, profile and generator version).
        for message in db.scalars(select(GmailMessageRecord)):
            if message.direction == "INBOUND":
                _draft, draft_created = generate_response_draft_for_message(db, ACCOUNT, message.id)
                assert draft_created is False
        result = evaluate_follow_up_for_job(
            db, ACCOUNT, world["initech"].id, settings=SETTINGS, now=NOW
        )
        assert result.created is False

        assert snapshot == (
            _count(db, GmailMessageAnalysisRecord),
            _count(db, ResponseDraftRecord),
            _count(db, FollowUpProposalRecord),
        )

    def test_manual_reanalysis_settles_and_then_is_idempotent(self, db):
        """Analysis identity includes thread context (Stage 7B), so the first
        manual re-analysis after the cycle may add revisions for messages
        analysed before their thread siblings; an identical second pass on
        unchanged context must add nothing."""
        _seed_world(db)
        _run_cycle(db)
        ids = [m.id for m in db.scalars(select(GmailMessageRecord).order_by(GmailMessageRecord.id))]
        for message_id in ids:
            analyze_gmail_message(db, ACCOUNT, message_id)
        settled = _count(db, GmailMessageAnalysisRecord)
        for message_id in ids:
            _analysis, created = analyze_gmail_message(db, ACCOUNT, message_id)
            assert created is False
        assert _count(db, GmailMessageAnalysisRecord) == settled

    def test_duplicate_gmail_identity_creates_no_duplicate_work(self, db):
        world = _seed_world(db)
        _run_cycle(db)
        messages_before = _count(db, GmailMessageRecord)
        drafts_before = _count(db, ResponseDraftRecord)

        # The same (account, mailbox, UIDVALIDITY, UID) delivered again, as a
        # re-sync would.
        _message(
            db,
            uid=10,
            direction="INBOUND",
            message_id="<offer@globex.example.com>",
            in_reply_to="<app-globex@me.example.com>",
            body=world["globex_reply"].body_plain,
            arrived=NOW - timedelta(days=2),
        )
        assert _count(db, GmailMessageRecord) == messages_before
        assert _run_cycle(db)["counters"]["scanned"] == 0
        assert _count(db, ResponseDraftRecord) == drafts_before

    def test_same_uid_in_another_mailbox_or_uidvalidity_is_a_different_message(self, db):
        _seed_world(db)
        _run_cycle(db)
        before = _count(db, GmailMessageRecord)
        _message(
            db,
            uid=10,
            direction="INBOUND",
            message_id="<other-mailbox@globex.example.com>",
            body="Our weekly product newsletter.",
            arrived=NOW,
            mailbox="[Gmail]/Отправленные",
        )
        assert _count(db, GmailMessageRecord) == before + 1


class TestFailurePaths:
    def test_generation_failure_leaves_no_draft_and_is_retried(self, db, monkeypatch):
        world = _seed_world(db)
        import app.services.response_draft as response_draft_module

        real_generate = response_draft_module.generate_response_draft

        def _boom(**_kwargs):
            raise RuntimeError("simulated provider failure")

        monkeypatch.setattr(response_draft_module, "generate_response_draft", _boom)
        failed = _run_cycle(db)
        assert failed["counters"]["failed"] == 1
        assert failed["failures"][0]["phase"] == "response_draft"
        failed_id = failed["failures"][0]["gmail_message_id"]
        assert _drafts_for(db, failed_id) == []
        assert db.get(GmailMessageRecord, failed_id).automation_processed_at is None

        monkeypatch.setattr(response_draft_module, "generate_response_draft", real_generate)
        recovered = _run_cycle(db)
        assert recovered["counters"]["failed"] == 0
        assert len(_drafts_for(db, world["globex_reply"].id)) == 1
        assert len(_drafts_for(db, world["newsletter"].id)) == 1
        assert get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)[0].gmail_message_id == (
            world["globex_reply"].id
        )

    def test_failure_after_draft_commit_is_retried_without_duplicates(self, db, monkeypatch):
        """The reply's draft commits, but recording the message as processed
        then fails (crash window). The retry must reuse that draft, never
        create a second one."""
        world = _seed_world(db)
        import app.services.automation_gmail as automation_module

        real_mark = automation_module.mark_message_automation_processed
        reply_id = world["globex_reply"].id

        def _crash_on_reply(session, message_id):
            if message_id == reply_id:
                raise RuntimeError("simulated crash before the processed marker")
            return real_mark(session, message_id)

        monkeypatch.setattr(automation_module, "mark_message_automation_processed", _crash_on_reply)
        with pytest.raises(RuntimeError):
            _run_cycle(db)
        db.rollback()
        (committed_draft,) = _drafts_for(db, reply_id)
        assert db.get(GmailMessageRecord, reply_id).automation_processed_at is None

        monkeypatch.setattr(automation_module, "mark_message_automation_processed", real_mark)
        step = _run_cycle(db)
        assert step["counters"]["failed"] == 0
        assert step["counters"]["draft_reused"] == 1
        assert [d.id for d in _drafts_for(db, reply_id)] == [committed_draft.id]
        assert _count(db, ResponseDraftRecord) == 2  # reply + newsletter, nothing duplicated
        assert db.get(GmailMessageRecord, reply_id).automation_processed_at is not None

    def test_ambiguous_association_creates_no_job_link_and_no_follow_up(self, db):
        twin_a = _job(db, uid=1, title="Backend Engineer", company="Globex", url="")
        twin_b = _job(db, uid=2, title="Backend Engineer", company="Globex", url="")
        message = _message(
            db,
            uid=20,
            direction="INBOUND",
            message_id="<twins@globex.example.com>",
            body="We are pleased to offer you the position of Backend Engineer at Globex.",
            arrived=NOW - timedelta(days=1),
        )
        _run_cycle(db)
        analysis = db.scalar(
            select(GmailMessageAnalysisRecord).where(
                GmailMessageAnalysisRecord.gmail_message_id == message.id
            )
        )
        assert analysis.match_type == "AMBIGUOUS"
        assert analysis.matched_job_id is None
        assert analysis.requires_human_review is True
        for draft in _drafts_for(db, message.id):
            assert draft.matched_job_id is None
        for job in (twin_a, twin_b):
            result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
            assert result.proposal is None
        assert _count(db, FollowUpProposalRecord) == 0

    def test_rejected_draft_neither_sends_nor_creates_follow_up(self, db):
        world = _seed_world(db)
        _run_cycle(db)
        (reply_draft,) = _drafts_for(db, world["globex_reply"].id)
        approve_or_reject_response_draft(db, ACCOUNT, reply_draft.id, "REJECTED", None)
        assert _count(db, ResponseDraftApprovalRecord) == 1
        assert _count(db, ResponseDraftSendRecord) == 0
        assert _count(db, FollowUpProposalRecord) == 0
        result = evaluate_follow_up_for_job(
            db, ACCOUNT, world["globex"].id, settings=SETTINGS, now=NOW
        )
        assert result.proposal is None
