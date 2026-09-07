"""Service-level tests for app.services.follow_up_send (Stage 7E): the
"NO APPROVAL = NO FOLLOW-UP SEND" gate, cross-account isolation,
concurrency/idempotency of sends, recipient trust boundary, and the
fail-closed UNCERTAIN safety net — mirrors
tests/test_response_draft_send_service.py's coverage for Stage 7D.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.follow_up_approval_repository import (
    begin_transmission,
    claim_send_attempt,
    get_approval_for_proposal,
    get_send_for_proposal,
    retry_send_attempt,
)
from app.db.gmail_repository import get_message_by_id, upsert_message
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
    FollowUpProposalStaleAtSendTimeError,
    FollowUpSendFailedError,
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


_UNSET = object()


def _add_message(
    db,
    *,
    uid,
    message_id,
    direction="OUTBOUND",
    to_addresses=("hr@acme.example.com",),
    received_at=_UNSET,
    sent_at=None,
    **overrides,
):
    """`received_at` defaults to `NOW - 10 days` (well past the 7-day
    follow_up_delay) so existing call sites keep behaving as before
    without needing to pass it explicitly — see
    tests/test_follow_up_service.py's identical helper for why this must
    be set directly on the persisted row. `provider_arrival_at` (S7E-011:
    the actual trusted ordering timestamp — see
    app.db.follow_up_repository.get_thread_message_infos) is set to the
    SAME value here so this fixture controls eligibility timing exactly
    as before S7E-011.
    """
    data = dict(
        account_key=ACCOUNT,
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=message_id,
        in_reply_to=None,
        references=(),
        from_address=ACCOUNT if direction == "OUTBOUND" else "hr@acme.example.com",
        from_display_name=None,
        to_addresses=to_addresses,
        cc_addresses=(),
        subject="My application at Globex",
        sent_at=sent_at,
        direction=direction,
        body_plain="I am applying for the Backend Engineer role at Globex.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    data.update(overrides)
    record, _created = upsert_message(db, ParsedGmailMessage(**data))
    effective_received_at = (NOW - timedelta(days=10)) if received_at is _UNSET else received_at
    if effective_received_at is not None:
        record.received_at = effective_received_at
        record.provider_arrival_at = effective_received_at
        db.commit()
        db.refresh(record)
    return record


def _add_outbound_anchor(
    db, *, uid=1, message_id="<out@example.com>", to_addresses=("hr@acme.example.com",)
):
    return _add_message(
        db, uid=uid, message_id=message_id, direction="OUTBOUND", to_addresses=to_addresses
    )


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

    def test_ambiguous_to_addresses_never_becomes_a_proposal(self, db):
        """S7E-008: an anchor with no safe, unambiguous recipient never
        even becomes an ELIGIBLE proposal in the first place — recipient
        validation happens at proposal-build time, not send time. See
        tests/test_follow_up_service.py::TestRecipientSafety for the
        full matrix of rejected cases.
        """
        job = _add_job(db)
        outbound = _add_outbound_anchor(db, to_addresses=())
        _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None

    def test_blank_pinned_recipient_is_rejected_at_send_time(self, db):
        """Defense in depth: `_build_outbound_message` must never send to
        a blank recipient even if the pinned value is somehow empty
        (e.g. legacy/corrupted data predating S7E-008)."""
        proposal, _outbound, approval = _seed_and_approve(db)
        approval.pinned_recipient = ""
        db.commit()
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

    def test_concurrent_in_flight_transmission_blocks_a_second_request(self, db):
        """S7E-010: a claim that has ALREADY won `begin_transmission`
        (i.e. transmission may genuinely be underway) must never let a
        second request also call the provider — it fails closed to
        UNCERTAIN instead of blindly retrying."""
        proposal, _outbound, approval = _seed_and_approve(db)
        send_record, _claimed = claim_send_attempt(
            db,
            account_key=ACCOUNT,
            follow_up_proposal_id=proposal.id,
            gmail_message_id=proposal.anchor_gmail_message_id,
            approval_id=approval.id,
        )
        won = begin_transmission(db, send_record)
        assert won is True
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpSendOutcomeUncertainError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)
        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "UNCERTAIN"

    def test_pending_not_yet_attempted_is_safely_taken_over(self, db):
        """S7E-010: a PENDING claim that never reached `begin_transmission`
        (e.g. the process that claimed it crashed before calling the
        provider) is PROVABLY pre-transmission-safe — a later request must
        be able to take it over and actually send, never blocked forever."""
        proposal, _outbound, approval = _seed_and_approve(db)
        claim_send_attempt(
            db,
            account_key=ACCOUNT,
            follow_up_proposal_id=proposal.id,
            gmail_message_id=proposal.anchor_gmail_message_id,
            approval_id=approval.id,
        )
        provider = FakeOutboundProvider()

        record = send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert record.status == "SENT"
        assert provider.call_count == 1

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


class TestSendTimeRevalidation:
    """S7E-002 (Codex remediation): immediately before the outbound
    provider is ever called, eligibility is recomputed from scratch. Any
    state change between approval and send that would have made the
    proposal ineligible must fail closed — never silently send stale or
    re-targeted content. In every case here: the provider is NEVER
    called, and the underlying send attempt ends up FAILED (transmission
    was never attempted), not UNCERTAIN.
    """

    def test_job_no_longer_applied_is_stale(self, db):
        proposal, _outbound, _approval = _seed_and_approve(db)
        job = db.get(JobRecord, proposal.job_id)
        job.status = "INTERVIEW"
        db.commit()
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpProposalStaleAtSendTimeError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "FAILED"

    def test_newer_inbound_reply_since_approval_is_stale(self, db):
        proposal, outbound, _approval = _seed_and_approve(db)
        _add_message(
            db,
            uid=99,
            message_id="<reply@example.com>",
            in_reply_to=outbound.message_id_header,
            references=(outbound.message_id_header,),
            direction="INBOUND",
            received_at=NOW,  # after the outbound anchor's received_at
        )
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpProposalStaleAtSendTimeError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "FAILED"

    def test_thread_becomes_ambiguous_since_approval_is_stale(self, db):
        """S7E-005 enforced again at send time: a second job decisively
        matched to the SAME thread after approval must block the send."""
        proposal, outbound, _approval = _seed_and_approve(db)
        other_job = _add_job(db, uid=999)
        reply = _add_message(
            db,
            uid=98,
            message_id="<reply2@example.com>",
            in_reply_to=outbound.message_id_header,
            references=(outbound.message_id_header,),
            direction="INBOUND",
        )
        _add_analysis(db, gmail_message_id=reply.id, matched_job_id=other_job.id)
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpProposalStaleAtSendTimeError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "FAILED"

    def test_newer_outbound_message_changes_the_anchor_and_is_stale(self, db):
        """The proposal was built from an OLDER outbound anchor; a newer
        outbound message sent since then means the thread's true latest
        OUTBOUND anchor has changed — the pinned content no longer
        corresponds to the current correspondence state."""
        proposal, outbound, _approval = _seed_and_approve(db)
        _add_message(
            db,
            uid=97,
            message_id="<out2@example.com>",
            in_reply_to=outbound.message_id_header,
            references=(outbound.message_id_header,),
            direction="OUTBOUND",
            received_at=NOW,
        )
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpProposalStaleAtSendTimeError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "FAILED"

    def test_recipient_changed_since_approval_is_stale(self, db):
        """Anchor content is normally immutable — this simulates a
        hypothetical future bug/direct-DB edit to demonstrate the
        defense-in-depth re-check still catches a recipient mismatch
        between the pinned value and a fresh derivation."""
        proposal, outbound, approval = _seed_and_approve(db)
        assert approval.pinned_recipient == "hr@acme.example.com"

        stored = get_message_by_id(db, ACCOUNT, outbound.id)
        stored.to_addresses_json = json.dumps(["different-recruiter@acme.example.com"])
        db.commit()
        provider = FakeOutboundProvider()

        with pytest.raises(FollowUpProposalStaleAtSendTimeError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "FAILED"

    def test_unchanged_state_still_sends_successfully(self, db):
        """Sanity check: revalidation must not be so strict it rejects the
        genuinely-unchanged happy path."""
        proposal, _outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider()

        record = send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert record.status == "SENT"
        assert provider.call_count == 1


class TestReplyRaceAtDispatch:
    """S7E-012 (Codex re-review, MEDIUM): `begin_transmission`'s CAS only
    protects against ANOTHER CONCURRENT SEND REQUEST for this exact
    proposal — it says nothing about, and is never touched by, a Gmail
    sync (`app.services.gmail_inbox.GmailInboxService`), which persists
    new `GmailMessageRecord` rows on its own independent
    schedule/connection. A recruiter reply can legitimately land in the
    window between `_revalidate_or_fail_closed` passing and
    `provider.send` actually being invoked. This adversarial regression
    proves the LAST gate (`_fail_closed_if_reply_raced_dispatch`) catches
    exactly that: revalidation passes, a reply is committed immediately
    afterward (simulating the concurrent Gmail sync), and the outbound
    provider must NEVER be called.
    """

    def test_reply_landing_after_revalidation_blocks_dispatch(self, db, monkeypatch):
        proposal, outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider()

        import app.services.follow_up_send as send_module

        real_revalidate = send_module._revalidate_or_fail_closed

        def _revalidate_then_race_a_reply(*args, **kwargs):
            # Revalidation genuinely passes here: at this instant, no
            # reply exists yet.
            real_revalidate(*args, **kwargs)
            # Immediately afterward — the exact critical window S7E-012
            # closes — a concurrent Gmail sync commits a brand-new
            # INBOUND reply for this same thread.
            _add_message(
                db,
                uid=99,
                message_id="<race-reply@example.com>",
                in_reply_to=outbound.message_id_header,
                references=(outbound.message_id_header,),
                direction="INBOUND",
                received_at=NOW,
            )

        monkeypatch.setattr(
            send_module, "_revalidate_or_fail_closed", _revalidate_then_race_a_reply
        )

        with pytest.raises(FollowUpProposalStaleAtSendTimeError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "FAILED"

    def test_anchor_superseded_immediately_before_dispatch_also_blocks(self, db, monkeypatch):
        """Same critical window, different race: a newer OUTBOUND message
        (not a reply) becomes the thread's true latest anchor immediately
        after revalidation passed — the pinned content would no longer
        correspond to the current correspondence state."""
        proposal, outbound, _approval = _seed_and_approve(db)
        provider = FakeOutboundProvider()

        import app.services.follow_up_send as send_module

        real_revalidate = send_module._revalidate_or_fail_closed

        def _revalidate_then_race_a_newer_outbound(*args, **kwargs):
            real_revalidate(*args, **kwargs)
            _add_message(
                db,
                uid=98,
                message_id="<race-out@example.com>",
                in_reply_to=outbound.message_id_header,
                references=(outbound.message_id_header,),
                direction="OUTBOUND",
                received_at=NOW,
            )

        monkeypatch.setattr(
            send_module, "_revalidate_or_fail_closed", _revalidate_then_race_a_newer_outbound
        )

        with pytest.raises(FollowUpProposalStaleAtSendTimeError):
            send_follow_up(db, ACCOUNT, proposal.id, provider)

        assert provider.call_count == 0
        assert get_send_for_proposal(db, ACCOUNT, proposal.id).status == "FAILED"
