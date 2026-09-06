"""Service-level tests for app.services.follow_up_send (Stage 7E): the
"NO APPROVAL = NO FOLLOW-UP SEND" gate, cross-account isolation,
concurrency/idempotency of sends, recipient trust boundary, and the
fail-closed UNCERTAIN safety net — mirrors
tests/test_response_draft_send_service.py's coverage for Stage 7D.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.follow_up_approval_repository import (
    claim_send_attempt,
    get_approval_for_proposal,
    get_send_for_proposal,
    retry_send_attempt,
)
from app.db.gmail_repository import upsert_message
from app.db.models import GmailMessageAnalysisRecord, JobRecord
from app.providers.email.base import ParsedGmailMessage
from app.providers.email.outbound_base import (
    EmailSendConnectionError,
    EmailSendOutcomeUnknownError,
    OutboundSendResult,
)
from app.services.follow_up import evaluate_follow_up_for_job
from app.services.follow_up_send import (
    FollowUpAlreadyDecidedError,
    FollowUpAlreadySentError,
    FollowUpMissingRecipientError,
    FollowUpNotApprovedError,
    FollowUpProposalNotFoundError,
    FollowUpSendFailedError,
    FollowUpSendInProgressError,
    FollowUpSendOutcomeUncertainError,
    approve_or_reject_follow_up,
    get_follow_up_state,
    send_follow_up,
)

ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"
NOW = datetime(2026, 9, 6, tzinfo=UTC)
SETTINGS = Settings(follow_up_delay_days=7)


class FakeOutboundProvider:
    def __init__(self, *, fail=False, uncertain=False, provider_message_id="msg-1"):
        self.fail = fail
        self.uncertain = uncertain
        self.provider_message_id = provider_message_id
        self.sent_messages: list = []
        self.call_count = 0

    def send(self, message):
        self.call_count += 1
        if self.uncertain:
            raise EmailSendOutcomeUnknownError("simulated ambiguous provider outcome")
        if self.fail:
            raise EmailSendConnectionError("simulated provider failure")
        self.sent_messages.append(message)
        return OutboundSendResult(provider_message_id=self.provider_message_id)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'follow_up_send_service.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _add_job(db, *, uid=1) -> JobRecord:
    job = JobRecord(
        fingerprint=f"fp-{uid}",
        source="bundesagentur",
        title="Backend Engineer",
        company="Globex",
        location="Berlin",
        url="https://example.com/jobs/1",
        description="",
        score=80,
        recommendation="APPLY",
        status="APPLIED",
    )
    db.add(job)
    db.commit()
    return job


def _add_outbound_anchor(
    db, *, uid=1, message_id="<out@example.com>", to_addresses=("hr@acme.example.com",)
):
    data = dict(
        account_key=ACCOUNT,
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=message_id,
        in_reply_to=None,
        references=(),
        from_address=ACCOUNT,
        from_display_name=None,
        to_addresses=to_addresses,
        cc_addresses=(),
        subject="My application at Globex",
        sent_at=NOW - timedelta(days=10),
        direction="OUTBOUND",
        body_plain="I am applying for the Backend Engineer role at Globex.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    record, _created = upsert_message(db, ParsedGmailMessage(**data))
    return record


def _add_analysis(db, *, gmail_message_id, matched_job_id):
    db.add(
        GmailMessageAnalysisRecord(
            account_key=ACCOUNT,
            gmail_message_id=gmail_message_id,
            analysis_version=1,
            input_fingerprint=f"fp-{gmail_message_id}",
            context_fingerprint="ctx",
            match_type="APPLICATION",
            matched_job_id=matched_job_id,
            match_confidence="HIGH",
            match_score=90,
            classification="OTHER",
            classification_confidence="HIGH",
            is_automated=False,
            requires_human_review=True,
        )
    )
    db.commit()


def _seed_proposal(db, *, uid=1, to_addresses=("hr@acme.example.com",)):
    job = _add_job(db, uid=uid)
    outbound = _add_outbound_anchor(
        db, uid=uid, message_id=f"<out{uid}@example.com>", to_addresses=to_addresses
    )
    _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)
    result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
    assert result.eligibility == "ELIGIBLE"
    return result.proposal, outbound


def _seed_and_approve(db, *, uid=1, decision="APPROVED", to_addresses=("hr@acme.example.com",)):
    proposal, outbound = _seed_proposal(db, uid=uid, to_addresses=to_addresses)
    approval = approve_or_reject_follow_up(db, ACCOUNT, proposal.id, decision, None)
    return proposal, outbound, approval


class TestApprovalMandatory:
    def test_send_without_any_decision_is_rejected(self, db):
        proposal, _outbound = _seed_proposal(db)
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpNotApprovedError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 0

    def test_send_with_rejected_decision_is_rejected(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db, decision="REJECTED")
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpNotApprovedError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 0

    def test_second_decision_on_same_proposal_is_rejected(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)

        with pytest.raises(FollowUpAlreadyDecidedError):
            approve_or_reject_follow_up(db, ACCOUNT, proposal.id, "REJECTED", None)

        approval = get_approval_for_proposal(db, ACCOUNT, proposal.id)
        assert approval.decision == "APPROVED"


class TestNoAutoSend:
    def test_approving_never_calls_the_provider(self, db):
        proposal, _outbound = _seed_proposal(db)
        approve_or_reject_follow_up(db, ACCOUNT, proposal.id, "APPROVED", None)
        # No provider is even constructed/passed at decision time — the
        # decision function's signature has no provider parameter at all.
        import inspect

        from app.services.follow_up_send import approve_or_reject_follow_up as fn

        assert "provider" not in inspect.signature(fn).parameters

    def test_approved_proposal_requires_an_explicit_send_call(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        state = get_follow_up_state(db, ACCOUNT, proposal.id)
        assert state.send is None


class TestWrongAccountBlocked:
    def test_send_for_other_accounts_proposal_is_not_found(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpProposalNotFoundError):
            send_follow_up(db, OTHER_ACCOUNT, proposal.id, provider)
        assert provider.call_count == 0

    def test_decision_for_other_accounts_proposal_is_not_found(self, db):
        proposal, _outbound = _seed_proposal(db)
        with pytest.raises(FollowUpProposalNotFoundError):
            approve_or_reject_follow_up(db, OTHER_ACCOUNT, proposal.id, "APPROVED", None)

    def test_state_for_other_accounts_proposal_is_not_found(self, db):
        proposal, _outbound = _seed_proposal(db)
        with pytest.raises(FollowUpProposalNotFoundError):
            get_follow_up_state(db, OTHER_ACCOUNT, proposal.id)


class TestRecipientTrustBoundary:
    def test_recipient_is_anchor_messages_own_to_address(self, db):
        proposal, _outbound, _approval = _seed_and_approve(
            db, to_addresses=("genuine-recruiter@acme.example.com",)
        )
        provider = FakeOutboundProvider()

        send_follow_up(db, ACCOUNT, proposal.id, provider)

        sent = provider.sent_messages[0]
        assert sent.to_address == "genuine-recruiter@acme.example.com"
        assert sent.subject == proposal.subject
        assert sent.body == proposal.body

    def test_missing_recipient_is_rejected(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db, to_addresses=())
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpMissingRecipientError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 0


class TestConcurrencyAndRetry:
    def test_second_send_after_success_is_rejected(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider()

        first = send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert first.status == "SENT"

        with pytest.raises(FollowUpAlreadySentError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 1

    def test_concurrent_pending_claim_blocks_a_second_request(self, db):
        proposal, _outbound, approval = _seed_and_approve(db)
        claim_send_attempt(
            db,
            account_key=ACCOUNT,
            follow_up_proposal_id=proposal.id,
            gmail_message_id=proposal.anchor_gmail_message_id,
            approval_id=approval.id,
        )
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpSendInProgressError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 0

    def test_provider_failure_marks_failed_and_allows_retry(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        failing_provider = FakeOutboundProvider(fail=True)

        with pytest.raises(FollowUpSendFailedError):
            send_follow_up(db, ACCOUNT, proposal.id, failing_provider)

        send_record = get_send_for_proposal(db, ACCOUNT, proposal.id)
        assert send_record.status == "FAILED"

        succeeding_provider = FakeOutboundProvider()
        record = send_follow_up(db, ACCOUNT, proposal.id, succeeding_provider)
        assert record.status == "SENT"
        assert record.attempt_count == 2

    def test_two_concurrent_retries_after_failure_only_one_wins(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        failing_provider = FakeOutboundProvider(fail=True)
        with pytest.raises(FollowUpSendFailedError):
            send_follow_up(db, ACCOUNT, proposal.id, failing_provider)

        record = get_send_for_proposal(db, ACCOUNT, proposal.id)
        won_first = retry_send_attempt(db, record)
        won_second = retry_send_attempt(db, record)

        assert won_first is True
        assert won_second is False


class TestUncertainSafety:
    def test_ambiguous_outcome_transitions_to_uncertain_not_failed(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider(uncertain=True)

        with pytest.raises(FollowUpSendOutcomeUncertainError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        send_record = get_send_for_proposal(db, ACCOUNT, proposal.id)
        assert send_record.status == "UNCERTAIN"
        assert send_record.sent_at is None

    def test_subsequent_send_after_uncertain_never_calls_provider_again(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider(uncertain=True)

        with pytest.raises(FollowUpSendOutcomeUncertainError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 1

        with pytest.raises(FollowUpSendOutcomeUncertainError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 1  # never called again

    def test_uncertain_send_is_never_automatically_retried(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider(uncertain=True)

        with pytest.raises(FollowUpSendOutcomeUncertainError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        record = get_send_for_proposal(db, ACCOUNT, proposal.id)
        won = retry_send_attempt(db, record)

        assert won is False
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "UNCERTAIN"

    def test_state_endpoint_helper_exposes_uncertain_status(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider(uncertain=True)

        with pytest.raises(FollowUpSendOutcomeUncertainError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        state = get_follow_up_state(db, ACCOUNT, proposal.id)
        assert state.send is not None
        assert state.send.status == "UNCERTAIN"


class TestNoOtherSideEffects:
    def test_job_status_never_mutated_by_send(self, db):
        job = _add_job(db)
        outbound = _add_outbound_anchor(db)
        _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)
        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
        approve_or_reject_follow_up(db, ACCOUNT, result.proposal.id, "APPROVED", None)
        provider = FakeOutboundProvider()

        send_follow_up(db, ACCOUNT, result.proposal.id, provider)

        refreshed = db.get(JobRecord, job.id)
        assert refreshed.status == "APPLIED"

    def test_no_telegram_or_url_fetch_imports_in_send_module(self):
        import inspect

        import app.services.follow_up_send as module

        source = inspect.getsource(module)
        for forbidden in (
            "TelegramNotifier",
            "requests.",
            "httpx.",
            "urllib.request",
            "urlopen(",
        ):
            assert forbidden not in source
