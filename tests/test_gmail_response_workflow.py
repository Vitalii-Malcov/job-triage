"""Stage 12 (controlled Gmail response workflow): service/repository tests for
the cross-revision guarantees added on top of Stages 7C/7D/7E.

- A response-draft revision can only be APPROVED while it is the CURRENT
  revision for its message (`is_current_response_draft_revision`).
- At most one live (PENDING/SENT/UNCERTAIN) reply per inbound message across
  ALL draft revisions, and at most one live follow-up per correspondence
  anchor across ALL proposals -- enforced by partial unique indexes, with the
  repository translating a violation into a typed error and the services
  pre-checking it.
- GET /response-drafts/pending's query (`get_pending_response_drafts`).

No real SMTP/IMAP anywhere: the outbound provider is an in-memory fake.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.follow_up_approval_repository import (
    FollowUpLiveSendExistsError,
    get_live_send_for_anchor,
    get_send_for_proposal,
)
from app.db.follow_up_approval_repository import (
    claim_send_attempt as claim_follow_up_send_attempt,
)
from app.db.follow_up_approval_repository import (
    retry_send_attempt as retry_follow_up_send_attempt,
)
from app.db.gmail_repository import upsert_message
from app.db.models import (
    CandidateProfileRecord,
    GmailMessageAnalysisRecord,
    JobRecord,
    ResponseDraftRecord,
    ResponseDraftSendRecord,
)
from app.db.response_draft_approval_repository import (
    ResponseDraftLiveSendExistsError,
    claim_send_attempt,
    get_live_send_for_message,
    get_send_for_draft,
    retry_send_attempt,
)
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.providers.email.base import ParsedGmailMessage
from app.providers.email.outbound_base import (
    EmailSendConnectionError,
    EmailSendOutcomeUnknownError,
    OutboundSendResult,
)
from app.services.follow_up import evaluate_follow_up_for_job
from app.services.follow_up_send import (
    FollowUpAnchorAlreadyFollowedUpError,
    approve_or_reject_follow_up,
    send_follow_up,
)
from app.services.gmail_message_analysis import analyze_gmail_message
from app.services.response_draft import (
    generate_response_draft_for_message,
    is_current_response_draft_revision,
)
from app.services.response_draft_send import (
    ResponseDraftAlreadyDecidedError,
    ResponseDraftAlreadySentError,
    ResponseDraftMessageAlreadyRepliedError,
    ResponseDraftSendFailedError,
    ResponseDraftSendOutcomeUncertainError,
    ResponseDraftSupersededError,
    approve_or_reject_response_draft,
    get_pending_response_drafts,
    get_response_draft_state,
    send_response_draft,
)

ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"
NOW = datetime(2026, 9, 6, tzinfo=UTC)
FOLLOW_UP_SETTINGS = Settings(follow_up_delay_days=7)


class FakeOutboundProvider:
    def __init__(self, *, fail: bool = False, uncertain: bool = False):
        self.fail = fail
        self.uncertain = uncertain
        self.sent_messages: list = []
        self.call_count = 0

    def send(self, message):
        self.call_count += 1
        if self.uncertain:
            raise EmailSendOutcomeUnknownError("simulated ambiguous provider outcome")
        if self.fail:
            raise EmailSendConnectionError("simulated provider failure")
        self.sent_messages.append(message)
        return OutboundSendResult(provider_message_id=f"msg-{self.call_count}")


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gmail_response_workflow.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _seed_job(db, *, uid: int = 1, title: str = "Backend Engineer", company: str = "Globex"):
    job = JobRecord(
        fingerprint=f"fp-{uid}",
        source="bundesagentur",
        title=title,
        company=company,
        location="Berlin",
        url=f"https://example.com/jobs/{uid}",
        description="",
        score=80,
        recommendation="APPLY",
        status="APPLIED",
    )
    db.add(job)
    db.commit()
    return job


def _seed_inbound(
    db,
    *,
    uid: int = 1,
    account_key: str = ACCOUNT,
    body_plain: str = ("We are pleased to offer you the position of Backend Engineer at Globex."),
) -> int:
    record, _created = upsert_message(
        db,
        ParsedGmailMessage(
            account_key=account_key,
            mailbox="INBOX",
            uid=uid,
            uid_validity=100,
            message_id_header=f"<{uid}@acme.example.com>",
            in_reply_to=None,
            references=(),
            from_address="hr@acme.example.com",
            from_display_name="Recruiter",
            to_addresses=(account_key,),
            cc_addresses=(),
            subject="Offer",
            sent_at=datetime.now(UTC),
            direction="INBOUND",
            body_plain=body_plain,
            body_truncated=False,
            has_html=False,
            attachments=(),
        ),
    )
    db.commit()
    return record.id


def _draft(db, msg_id: int, account_key: str = ACCOUNT) -> ResponseDraftRecord:
    analyze_gmail_message(db, account_key, msg_id)
    record, _created = generate_response_draft_for_message(db, account_key, msg_id)
    return record


def _bump_candidate_profile(db) -> None:
    """A candidate-profile edit -- bumps `profile_version`, so the next
    generation call produces a NEW draft revision for the same analysis."""
    if db.get(CandidateProfileRecord, 1) is None:
        db.add(CandidateProfileRecord(id=1, profile_version=1))
        db.commit()
    current = db.get(CandidateProfileRecord, 1)
    db.refresh(current)
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=current.profile_version,
            first_name="Jane",
            last_name=f"Doe{current.profile_version}",
        ),
    )


def _two_revisions(db, msg_id: int) -> tuple[ResponseDraftRecord, ResponseDraftRecord]:
    """Revision A, then (after a profile edit) the current revision B."""
    old = _draft(db, msg_id)
    _bump_candidate_profile(db)
    new = _draft(db, msg_id)
    assert new.id != old.id
    assert new.gmail_message_id == old.gmail_message_id
    return old, new


def _current_then_delayed_stale_draft(
    db, msg_id: int, monkeypatch
) -> tuple[ResponseDraftRecord, ResponseDraftRecord]:
    """S12-001 race through the real generator: worker A snapshots the
    candidate profile, the profile is edited, worker B generates the CURRENT
    draft, then A resumes with its stale snapshot and inserts LATER."""
    _bump_candidate_profile(db)
    analyze_gmail_message(db, ACCOUNT, msg_id)
    worker_a_snapshot = db.get(CandidateProfileRecord, 1)
    db.refresh(worker_a_snapshot)
    db.expunge(worker_a_snapshot)

    _bump_candidate_profile(db)
    current, created = generate_response_draft_for_message(db, ACCOUNT, msg_id)  # worker B
    assert created

    with monkeypatch.context() as patched:
        patched.setattr(
            "app.services.response_draft.get_candidate_profile", lambda _db: worker_a_snapshot
        )
        stale, created = generate_response_draft_for_message(db, ACCOUNT, msg_id)  # worker A
    assert created
    assert stale.candidate_profile_version < current.candidate_profile_version
    assert stale.analysis_id == current.analysis_id
    assert is_current_response_draft_revision(db, ACCOUNT, current)
    assert not is_current_response_draft_revision(db, ACCOUNT, stale)
    return current, stale


# --------------------------------------------------------------------------- currency


class TestCurrentRevision:
    def test_fresh_draft_is_current(self, db):
        _seed_job(db)
        draft = _draft(db, _seed_inbound(db))
        assert is_current_response_draft_revision(db, ACCOUNT, draft) is True

    def test_profile_edit_supersedes_the_old_revision(self, db):
        _seed_job(db)
        old, new = _two_revisions(db, _seed_inbound(db))
        assert is_current_response_draft_revision(db, ACCOUNT, old) is False
        assert is_current_response_draft_revision(db, ACCOUNT, new) is True

    def test_profile_edit_alone_supersedes_before_any_regeneration(self, db):
        _seed_job(db)
        draft = _draft(db, _seed_inbound(db))
        _bump_candidate_profile(db)
        assert is_current_response_draft_revision(db, ACCOUNT, draft) is False

    def test_newer_analysis_supersedes_the_old_revision(self, db):
        _seed_job(db)
        msg_id = _seed_inbound(db)
        old = _draft(db, msg_id)
        # A new tracked job changes the analysis context fingerprint, so a
        # re-analysis creates a NEW analysis row for the same message.
        _seed_job(db, uid=2, title="Data Engineer", company="Initech")
        new = _draft(db, msg_id)
        assert new.analysis_id != old.analysis_id
        assert is_current_response_draft_revision(db, ACCOUNT, old) is False
        assert is_current_response_draft_revision(db, ACCOUNT, new) is True

    def test_generator_version_mismatch_is_not_current(self, db):
        _seed_job(db)
        draft = _draft(db, _seed_inbound(db))
        draft.generator_version = "v0-old"
        assert is_current_response_draft_revision(db, ACCOUNT, draft) is False
        db.rollback()


class TestApprovalCurrency:
    def test_superseded_revision_cannot_be_approved(self, db):
        _seed_job(db)
        old, _new = _two_revisions(db, _seed_inbound(db))
        with pytest.raises(ResponseDraftSupersededError):
            approve_or_reject_response_draft(db, ACCOUNT, old.id, "APPROVED", None)
        assert get_response_draft_state(db, ACCOUNT, old.id).approval is None

    def test_superseded_revision_can_still_be_rejected(self, db):
        _seed_job(db)
        old, _new = _two_revisions(db, _seed_inbound(db))
        record = approve_or_reject_response_draft(db, ACCOUNT, old.id, "REJECTED", None)
        assert record.decision == "REJECTED"

    def test_current_revision_can_be_approved(self, db):
        _seed_job(db)
        _old, new = _two_revisions(db, _seed_inbound(db))
        record = approve_or_reject_response_draft(db, ACCOUNT, new.id, "APPROVED", None)
        assert record.decision == "APPROVED"
        assert record.response_draft_id == new.id

    def test_already_decided_takes_precedence_over_superseded(self, db):
        _seed_job(db)
        msg_id = _seed_inbound(db)
        old = _draft(db, msg_id)
        approve_or_reject_response_draft(db, ACCOUNT, old.id, "REJECTED", None)
        _bump_candidate_profile(db)
        _draft(db, msg_id)
        with pytest.raises(ResponseDraftAlreadyDecidedError):
            approve_or_reject_response_draft(db, ACCOUNT, old.id, "APPROVED", None)

    def test_state_reports_currency(self, db):
        _seed_job(db)
        old, new = _two_revisions(db, _seed_inbound(db))
        assert get_response_draft_state(db, ACCOUNT, old.id).is_current_revision is False
        assert get_response_draft_state(db, ACCOUNT, new.id).is_current_revision is True

    def test_approval_recorded_while_current_still_authorizes_its_pinned_content(self, db):
        """Unchanged Stage 7D contract: an approval valid when recorded keeps
        authorizing exactly its pinned subject/body after a later profile
        edit supersedes the revision."""
        _seed_job(db)
        msg_id = _seed_inbound(db)
        old = _draft(db, msg_id)
        approval = approve_or_reject_response_draft(db, ACCOUNT, old.id, "APPROVED", None)
        _bump_candidate_profile(db)
        _draft(db, msg_id)

        provider = FakeOutboundProvider()
        record = send_response_draft(db, ACCOUNT, old.id, provider)
        assert record.status == "SENT"
        assert provider.sent_messages[0].body == approval.pinned_body


# ------------------------------------------------------------- one reply per message


def _approve_both_revisions(db, msg_id: int):
    """A approved while current, then a profile edit, then B approved."""
    old = _draft(db, msg_id)
    approve_or_reject_response_draft(db, ACCOUNT, old.id, "APPROVED", None)
    _bump_candidate_profile(db)
    new = _draft(db, msg_id)
    approve_or_reject_response_draft(db, ACCOUNT, new.id, "APPROVED", None)
    return old, new


class TestOneLiveReplyPerMessage:
    def test_second_revision_cannot_be_sent_after_first_was_sent(self, db):
        _seed_job(db)
        old, new = _approve_both_revisions(db, _seed_inbound(db))
        provider = FakeOutboundProvider()

        assert send_response_draft(db, ACCOUNT, old.id, provider).status == "SENT"
        with pytest.raises(ResponseDraftMessageAlreadyRepliedError):
            send_response_draft(db, ACCOUNT, new.id, provider)

        assert provider.call_count == 1
        assert get_send_for_draft(db, ACCOUNT, new.id) is None

    def test_sending_the_same_revision_again_still_reports_already_sent(self, db):
        _seed_job(db)
        old, _new = _approve_both_revisions(db, _seed_inbound(db))
        provider = FakeOutboundProvider()
        send_response_draft(db, ACCOUNT, old.id, provider)
        with pytest.raises(ResponseDraftAlreadySentError):
            send_response_draft(db, ACCOUNT, old.id, provider)
        assert provider.call_count == 1

    def test_uncertain_send_blocks_every_other_revision(self, db):
        _seed_job(db)
        old, new = _approve_both_revisions(db, _seed_inbound(db))
        with pytest.raises(ResponseDraftSendOutcomeUncertainError):
            send_response_draft(db, ACCOUNT, old.id, FakeOutboundProvider(uncertain=True))

        provider = FakeOutboundProvider()
        with pytest.raises(ResponseDraftMessageAlreadyRepliedError):
            send_response_draft(db, ACCOUNT, new.id, provider)
        assert provider.call_count == 0

    def test_failed_send_does_not_block_another_revision(self, db):
        _seed_job(db)
        old, new = _approve_both_revisions(db, _seed_inbound(db))
        with pytest.raises(ResponseDraftSendFailedError):
            send_response_draft(db, ACCOUNT, old.id, FakeOutboundProvider(fail=True))
        assert get_send_for_draft(db, ACCOUNT, old.id).status == "FAILED"

        provider = FakeOutboundProvider()
        assert send_response_draft(db, ACCOUNT, new.id, provider).status == "SENT"

        # Retrying the FAILED old revision is now refused, never a 2nd reply.
        with pytest.raises(ResponseDraftMessageAlreadyRepliedError):
            send_response_draft(db, ACCOUNT, old.id, provider)
        assert provider.call_count == 1
        assert get_send_for_draft(db, ACCOUNT, old.id).status == "FAILED"

    def test_failed_send_of_same_revision_can_still_be_retried(self, db):
        _seed_job(db)
        msg_id = _seed_inbound(db)
        draft = _draft(db, msg_id)
        approve_or_reject_response_draft(db, ACCOUNT, draft.id, "APPROVED", None)
        with pytest.raises(ResponseDraftSendFailedError):
            send_response_draft(db, ACCOUNT, draft.id, FakeOutboundProvider(fail=True))
        record = send_response_draft(db, ACCOUNT, draft.id, FakeOutboundProvider())
        assert record.status == "SENT"
        assert record.attempt_count == 2

    def test_approving_another_revision_after_a_reply_is_refused(self, db):
        _seed_job(db)
        msg_id = _seed_inbound(db)
        old = _draft(db, msg_id)
        approve_or_reject_response_draft(db, ACCOUNT, old.id, "APPROVED", None)
        send_response_draft(db, ACCOUNT, old.id, FakeOutboundProvider())
        _bump_candidate_profile(db)
        new = _draft(db, msg_id)

        with pytest.raises(ResponseDraftMessageAlreadyRepliedError):
            approve_or_reject_response_draft(db, ACCOUNT, new.id, "APPROVED", None)
        # Rejecting is always allowed.
        assert (
            approve_or_reject_response_draft(db, ACCOUNT, new.id, "REJECTED", None).decision
            == "REJECTED"
        )

    def test_replies_to_different_messages_are_independent(self, db):
        _seed_job(db)
        first = _draft(db, _seed_inbound(db, uid=1))
        second = _draft(db, _seed_inbound(db, uid=2))
        provider = FakeOutboundProvider()
        for draft in (first, second):
            approve_or_reject_response_draft(db, ACCOUNT, draft.id, "APPROVED", None)
            assert send_response_draft(db, ACCOUNT, draft.id, provider).status == "SENT"
        assert provider.call_count == 2


class TestLiveSendIndexBackstop:
    """The repository-level guarantee, independent of the service pre-checks
    -- what makes a race between two different revisions safe."""

    def test_claim_for_second_revision_is_refused_by_the_index(self, db):
        _seed_job(db)
        old, new = _approve_both_revisions(db, _seed_inbound(db))
        approval_old = get_response_draft_state(db, ACCOUNT, old.id).approval
        approval_new = get_response_draft_state(db, ACCOUNT, new.id).approval

        _record, claimed = claim_send_attempt(
            db,
            account_key=ACCOUNT,
            response_draft_id=old.id,
            gmail_message_id=old.gmail_message_id,
            approval_id=approval_old.id,
        )
        assert claimed is True
        with pytest.raises(ResponseDraftLiveSendExistsError):
            claim_send_attempt(
                db,
                account_key=ACCOUNT,
                response_draft_id=new.id,
                gmail_message_id=new.gmail_message_id,
                approval_id=approval_new.id,
            )
        assert get_send_for_draft(db, ACCOUNT, new.id) is None
        assert get_live_send_for_message(db, ACCOUNT, old.gmail_message_id).response_draft_id == (
            old.id
        )

    def test_retry_into_pending_is_refused_while_another_revision_is_live(self, db):
        _seed_job(db)
        old, new = _approve_both_revisions(db, _seed_inbound(db))
        approval_old = get_response_draft_state(db, ACCOUNT, old.id).approval
        approval_new = get_response_draft_state(db, ACCOUNT, new.id).approval

        old_row, _ = claim_send_attempt(
            db,
            account_key=ACCOUNT,
            response_draft_id=old.id,
            gmail_message_id=old.gmail_message_id,
            approval_id=approval_old.id,
        )
        old_row.status = "FAILED"
        db.commit()
        claim_send_attempt(
            db,
            account_key=ACCOUNT,
            response_draft_id=new.id,
            gmail_message_id=new.gmail_message_id,
            approval_id=approval_new.id,
        )

        with pytest.raises(ResponseDraftLiveSendExistsError):
            retry_send_attempt(db, old_row)
        db.refresh(old_row)
        assert old_row.status == "FAILED"
        assert old_row.attempt_count == 1

    def test_failed_rows_do_not_count_as_live(self, db):
        _seed_job(db)
        old, new = _approve_both_revisions(db, _seed_inbound(db))
        for draft in (old, new):
            approval = get_response_draft_state(db, ACCOUNT, draft.id).approval
            row, claimed = claim_send_attempt(
                db,
                account_key=ACCOUNT,
                response_draft_id=draft.id,
                gmail_message_id=draft.gmail_message_id,
                approval_id=approval.id,
            )
            assert claimed is True
            row.status = "FAILED"
            db.commit()
        rows = db.scalars(select(ResponseDraftSendRecord)).all()
        assert len(rows) == 2
        assert get_live_send_for_message(db, ACCOUNT, old.gmail_message_id) is None

    def test_index_is_account_scoped(self, db):
        """Different accounts never collide, even on the same message id
        value (the index includes account_key)."""
        _seed_job(db)
        draft = _draft(db, _seed_inbound(db))
        approval = approve_or_reject_response_draft(db, ACCOUNT, draft.id, "APPROVED", None)
        claim_send_attempt(
            db,
            account_key=ACCOUNT,
            response_draft_id=draft.id,
            gmail_message_id=draft.gmail_message_id,
            approval_id=approval.id,
        )
        assert get_live_send_for_message(db, OTHER_ACCOUNT, draft.gmail_message_id) is None


# ---------------------------------------------------------------- pending queue


class TestPendingResponseDrafts:
    def test_lists_undecided_current_draft(self, db):
        job = _seed_job(db)
        draft = _draft(db, _seed_inbound(db))
        pending = get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)
        assert [p.response_draft_id for p in pending] == [draft.id]
        item = pending[0]
        assert item.is_current_revision is True
        assert item.matched_job_id == job.id
        assert item.match_type in {"APPLICATION", "JOB_ONLY"}
        assert item.provider == draft.provider

    def test_only_newest_revision_per_message_is_listed(self, db):
        _seed_job(db)
        _old, new = _two_revisions(db, _seed_inbound(db))
        pending = get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)
        assert [p.response_draft_id for p in pending] == [new.id]

    def test_newest_revision_is_flagged_when_superseded_without_regeneration(self, db):
        _seed_job(db)
        draft = _draft(db, _seed_inbound(db))
        _bump_candidate_profile(db)
        pending = get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)
        assert [(p.response_draft_id, p.is_current_revision) for p in pending] == [
            (draft.id, False)
        ]

    def test_decided_drafts_are_not_pending(self, db):
        _seed_job(db)
        draft = _draft(db, _seed_inbound(db))
        approve_or_reject_response_draft(db, ACCOUNT, draft.id, "REJECTED", None)
        assert get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0) == []

    def test_message_with_a_live_reply_is_not_pending(self, db):
        _seed_job(db)
        msg_id = _seed_inbound(db)
        old = _draft(db, msg_id)
        approve_or_reject_response_draft(db, ACCOUNT, old.id, "APPROVED", None)
        send_response_draft(db, ACCOUNT, old.id, FakeOutboundProvider())
        _bump_candidate_profile(db)
        _draft(db, msg_id)
        assert get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0) == []

    def test_no_response_recommended_is_not_pending(self, db):
        _seed_job(db)
        draft = _draft(
            db,
            _seed_inbound(
                db,
                body_plain=(
                    "We regret to inform you that we will not move forward with your application."
                ),
            ),
        )
        assert draft.status == "NO_RESPONSE_RECOMMENDED"
        assert get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0) == []

    def test_other_accounts_drafts_are_never_listed(self, db):
        _seed_job(db)
        _draft(db, _seed_inbound(db, uid=7, account_key=OTHER_ACCOUNT), account_key=OTHER_ACCOUNT)
        assert get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0) == []
        assert len(get_pending_response_drafts(db, OTHER_ACCOUNT, limit=20, offset=0)) == 1

    def test_pagination_is_newest_first_and_bounded(self, db):
        _seed_job(db)
        ids = [_draft(db, _seed_inbound(db, uid=uid)).id for uid in (1, 2, 3)]
        first_page = get_pending_response_drafts(db, ACCOUNT, limit=2, offset=0)
        second_page = get_pending_response_drafts(db, ACCOUNT, limit=2, offset=2)
        assert [p.response_draft_id for p in first_page] == [ids[2], ids[1]]
        assert [p.response_draft_id for p in second_page] == [ids[0]]

    def test_ambiguous_association_never_carries_a_job_link(self, db):
        # Two tracked jobs at the same company with the same title, and an
        # email naming only that -- the matcher must not pick a winner.
        _seed_job(db, uid=1, title="Backend Engineer", company="Globex")
        _seed_job(db, uid=2, title="Backend Engineer", company="Globex")
        draft = _draft(db, _seed_inbound(db))
        analysis = db.get(GmailMessageAnalysisRecord, draft.analysis_id)
        assert analysis.match_type == "AMBIGUOUS"
        assert analysis.matched_job_id is None
        assert draft.matched_job_id is None
        pending = get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)
        if pending:  # only PROPOSED drafts are listed
            assert pending[0].match_type == "AMBIGUOUS"
            assert pending[0].matched_job_id is None

    def test_current_draft_wins_over_a_later_inserted_stale_draft(self, db, monkeypatch):
        """S12-001 (Codex review): worker A reads the candidate profile,
        pauses; the profile is edited and worker B creates the CURRENT
        draft; A resumes and inserts its stale draft with a HIGHER id. The
        queue must still list B, never A."""
        _seed_job(db)
        msg_id = _seed_inbound(db)
        current, stale = _current_then_delayed_stale_draft(db, msg_id, monkeypatch)
        assert stale.id > current.id  # insertion order is reversed vs. freshness

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("listing must not generate")

        monkeypatch.setattr("app.services.response_draft.generate_response_draft", _forbidden)
        pending = get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)

        assert [(p.response_draft_id, p.is_current_revision) for p in pending] == [
            (current.id, True)
        ]
        # Regeneration resolves to the same current draft: the queue is not
        # relying on a repair that can never happen.
        monkeypatch.undo()
        again, created = generate_response_draft_for_message(db, ACCOUNT, msg_id)
        assert (again.id, created) == (current.id, False)

    def test_decided_current_draft_is_not_replaced_by_a_later_stale_draft(self, db, monkeypatch):
        _seed_job(db)
        msg_id = _seed_inbound(db)
        current, _stale = _current_then_delayed_stale_draft(db, msg_id, monkeypatch)
        approve_or_reject_response_draft(db, ACCOUNT, current.id, "REJECTED", None)
        assert get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0) == []

    def test_stale_only_message_falls_back_to_its_highest_id_revision(self, db):
        _seed_job(db)
        _old, newer = _two_revisions(db, _seed_inbound(db))
        _bump_candidate_profile(db)  # neither revision is current any more
        pending = get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)
        assert [(p.response_draft_id, p.is_current_revision) for p in pending] == [
            (newer.id, False)
        ]

    def test_race_message_pages_deterministically_by_listed_draft_id(self, db, monkeypatch):
        _seed_job(db)
        race_msg = _seed_inbound(db, uid=1)
        current, _stale = _current_then_delayed_stale_draft(db, race_msg, monkeypatch)
        later = _draft(db, _seed_inbound(db, uid=2))
        _draft(db, _seed_inbound(db, uid=3, account_key=OTHER_ACCOUNT), account_key=OTHER_ACCOUNT)
        first_page = get_pending_response_drafts(db, ACCOUNT, limit=1, offset=0)
        second_page = get_pending_response_drafts(db, ACCOUNT, limit=1, offset=1)
        assert [p.response_draft_id for p in first_page] == [later.id]
        assert [p.response_draft_id for p in second_page] == [current.id]
        assert get_pending_response_drafts(db, ACCOUNT, limit=1, offset=2) == []

    def test_listing_never_generates_decides_or_sends(self, db, monkeypatch):
        _seed_job(db)
        _draft(db, _seed_inbound(db))

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("listing must not call this")

        monkeypatch.setattr(
            "app.services.response_draft.generate_response_draft", _forbidden, raising=True
        )
        monkeypatch.setattr("app.services.response_draft_send.create_approval", _forbidden)
        monkeypatch.setattr("app.services.response_draft_send.claim_send_attempt", _forbidden)
        before = db.scalars(select(ResponseDraftRecord)).all()
        get_pending_response_drafts(db, ACCOUNT, limit=20, offset=0)
        assert db.scalars(select(ResponseDraftRecord)).all() == before


# ---------------------------------------------------------------- follow-ups


def _seed_follow_up_anchor(db):
    job = _seed_job(db)
    record, _created = upsert_message(
        db,
        ParsedGmailMessage(
            account_key=ACCOUNT,
            mailbox="INBOX",
            uid=50,
            uid_validity=100,
            message_id_header="<out50@example.com>",
            in_reply_to=None,
            references=(),
            from_address=ACCOUNT,
            from_display_name=None,
            to_addresses=("hr@acme.example.com",),
            cc_addresses=(),
            subject="My application at Globex",
            sent_at=None,
            direction="OUTBOUND",
            body_plain="I am applying for the Backend Engineer role at Globex.",
            body_truncated=False,
            has_html=False,
            attachments=(),
        ),
    )
    arrival = NOW - timedelta(days=10)
    record.received_at = arrival
    record.provider_arrival_at = arrival
    record.provider_arrival_is_trusted = True
    db.add(
        GmailMessageAnalysisRecord(
            account_key=ACCOUNT,
            gmail_message_id=record.id,
            analysis_version=1,
            input_fingerprint=f"fp-{record.id}",
            context_fingerprint="ctx",
            match_type="APPLICATION",
            matched_job_id=job.id,
            match_confidence="HIGH",
            match_score=90,
            classification="OTHER",
            classification_confidence="HIGH",
            is_automated=False,
            requires_human_review=True,
        )
    )
    db.commit()
    return job, record


def _two_proposals_same_anchor(db):
    """Two proposals for ONE anchor: a different follow-up delay changes the
    input fingerprint but not the anchor (both delays have elapsed)."""
    job, anchor = _seed_follow_up_anchor(db)
    first = evaluate_follow_up_for_job(
        db, ACCOUNT, job.id, settings=FOLLOW_UP_SETTINGS, now=NOW
    ).proposal
    second = evaluate_follow_up_for_job(
        db, ACCOUNT, job.id, settings=Settings(follow_up_delay_days=8), now=NOW
    ).proposal
    assert first is not None and second is not None
    assert first.id != second.id
    assert first.anchor_gmail_message_id == second.anchor_gmail_message_id == anchor.id
    return first, second


class TestOneLiveFollowUpPerAnchor:
    def test_second_proposal_cannot_be_sent_after_first(self, db):
        first, second = _two_proposals_same_anchor(db)
        approve_or_reject_follow_up(db, ACCOUNT, first.id, "APPROVED", None)
        approve_or_reject_follow_up(db, ACCOUNT, second.id, "APPROVED", None)
        provider = FakeOutboundProvider()

        sent = send_follow_up(db, ACCOUNT, first.id, provider, settings=FOLLOW_UP_SETTINGS, now=NOW)
        assert sent.status == "SENT"
        with pytest.raises(FollowUpAnchorAlreadyFollowedUpError):
            send_follow_up(db, ACCOUNT, second.id, provider, settings=FOLLOW_UP_SETTINGS, now=NOW)
        assert provider.call_count == 1
        assert get_send_for_proposal(db, ACCOUNT, second.id) is None

    def test_approving_second_proposal_after_first_was_sent_is_refused(self, db):
        first, second = _two_proposals_same_anchor(db)
        approve_or_reject_follow_up(db, ACCOUNT, first.id, "APPROVED", None)
        send_follow_up(
            db, ACCOUNT, first.id, FakeOutboundProvider(), settings=FOLLOW_UP_SETTINGS, now=NOW
        )
        with pytest.raises(FollowUpAnchorAlreadyFollowedUpError):
            approve_or_reject_follow_up(db, ACCOUNT, second.id, "APPROVED", None)
        assert (
            approve_or_reject_follow_up(db, ACCOUNT, second.id, "REJECTED", None).decision
            == "REJECTED"
        )

    def test_failed_follow_up_does_not_block_the_other_proposal(self, db):
        first, second = _two_proposals_same_anchor(db)
        approve_or_reject_follow_up(db, ACCOUNT, first.id, "APPROVED", None)
        approve_or_reject_follow_up(db, ACCOUNT, second.id, "APPROVED", None)
        with pytest.raises(Exception):  # noqa: B017 -- FollowUpSendFailedError
            send_follow_up(
                db,
                ACCOUNT,
                first.id,
                FakeOutboundProvider(fail=True),
                settings=FOLLOW_UP_SETTINGS,
                now=NOW,
            )
        assert get_send_for_proposal(db, ACCOUNT, first.id).status == "FAILED"
        sent = send_follow_up(
            db, ACCOUNT, second.id, FakeOutboundProvider(), settings=FOLLOW_UP_SETTINGS, now=NOW
        )
        assert sent.status == "SENT"

    def test_repository_index_refuses_second_claim_and_retry(self, db):
        first, second = _two_proposals_same_anchor(db)
        approval_first = approve_or_reject_follow_up(db, ACCOUNT, first.id, "APPROVED", None)
        approval_second = approve_or_reject_follow_up(db, ACCOUNT, second.id, "APPROVED", None)

        first_row, claimed = claim_follow_up_send_attempt(
            db,
            account_key=ACCOUNT,
            follow_up_proposal_id=first.id,
            gmail_message_id=first.anchor_gmail_message_id,
            approval_id=approval_first.id,
        )
        assert claimed is True
        with pytest.raises(FollowUpLiveSendExistsError):
            claim_follow_up_send_attempt(
                db,
                account_key=ACCOUNT,
                follow_up_proposal_id=second.id,
                gmail_message_id=second.anchor_gmail_message_id,
                approval_id=approval_second.id,
            )

        # Flip roles: first FAILED, second live -> retrying first is refused.
        first_row.status = "FAILED"
        db.commit()
        claim_follow_up_send_attempt(
            db,
            account_key=ACCOUNT,
            follow_up_proposal_id=second.id,
            gmail_message_id=second.anchor_gmail_message_id,
            approval_id=approval_second.id,
        )
        with pytest.raises(FollowUpLiveSendExistsError):
            retry_follow_up_send_attempt(db, first_row)
        db.refresh(first_row)
        assert first_row.status == "FAILED"
        live = get_live_send_for_anchor(db, ACCOUNT, first.anchor_gmail_message_id)
        assert live.follow_up_proposal_id == second.id
