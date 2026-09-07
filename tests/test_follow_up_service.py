"""Integration tests for app.services.follow_up (Stage 7E orchestration):
eligibility gating end-to-end against real persisted Gmail/analysis data,
proposal content trust boundaries, and idempotent re-evaluation.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.gmail_repository import upsert_message
from app.db.models import CandidateProfileRecord, GmailMessageAnalysisRecord, JobRecord
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.providers.email.base import ParsedGmailMessage
from app.services.follow_up import (
    FollowUpJobNotFoundError,
    evaluate_follow_up_for_job,
    list_due_follow_ups,
)

ACCOUNT = "me@example.com"
NOW = datetime(2026, 9, 6, tzinfo=UTC)
SETTINGS = Settings(follow_up_delay_days=7)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'follow_up_service.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _add_job(db, *, uid=1, status="APPLIED", source="bundesagentur") -> JobRecord:
    job = JobRecord(
        fingerprint=f"fp-{uid}",
        source=source,
        title="Backend Engineer",
        company="Globex",
        location="Berlin",
        url="https://example.com/jobs/1",
        description="",
        score=80,
        recommendation="APPLY",
        status=status,
    )
    db.add(job)
    db.commit()
    return job


_UNSET = object()


def _add_message(db, *, uid, message_id, direction, sent_at, received_at=_UNSET, **overrides):
    """`received_at` (S7E-004: the trusted ordering timestamp — see
    app.db.follow_up_repository.get_thread_message_infos) defaults to
    `sent_at` here so existing test call sites that only pass `sent_at`
    keep controlling eligibility timing as before. `received_at` is not
    part of `ParsedGmailMessage` (it is always server-set to real
    wall-clock time at persist time in production — see
    GmailMessageRecord's docstring); tests that need a specific historical
    `received_at` set it directly on the persisted row afterwards, which
    is the only way to simulate "synced N days ago" since real sync time
    is never attacker/test-input-controlled in production.
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
        to_addresses=("hr@acme.example.com",) if direction == "OUTBOUND" else (ACCOUNT,),
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
    effective_received_at = sent_at if received_at is _UNSET else received_at
    if effective_received_at is not None:
        record.received_at = effective_received_at
        db.commit()
        db.refresh(record)
    return record


def _add_analysis(db, *, gmail_message_id, matched_job_id, match_type="APPLICATION") -> None:
    db.add(
        GmailMessageAnalysisRecord(
            account_key=ACCOUNT,
            gmail_message_id=gmail_message_id,
            analysis_version=1,
            input_fingerprint=f"fp-{gmail_message_id}",
            context_fingerprint="ctx",
            match_type=match_type,
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


def _seed_applied_job_with_outbound_anchor(db, *, outbound_sent_at, source="bundesagentur"):
    job = _add_job(db, source=source)
    outbound = _add_message(
        db, uid=1, message_id="<out@example.com>", direction="OUTBOUND", sent_at=outbound_sent_at
    )
    _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)
    return job, outbound


class TestJobNotFound:
    def test_unknown_job_id_raises(self, db):
        with pytest.raises(FollowUpJobNotFoundError):
            evaluate_follow_up_for_job(db, ACCOUNT, 999, settings=SETTINGS, now=NOW)


class TestNotApplied:
    def test_new_status_is_not_eligible_and_no_proposal_is_created(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )
        job.status = "NEW"
        db.commit()

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None


class TestTerminalStatusesSuppressed:
    @pytest.mark.parametrize("status", ["INTERVIEW", "REJECTED", "OFFER", "WITHDRAWN"])
    def test_terminal_status_suppresses_follow_up(self, db, status):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )
        job.status = status
        db.commit()

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None


class TestNoOutboundAnchor:
    def test_job_with_only_inbound_correspondence_is_not_eligible(self, db):
        job = _add_job(db)
        inbound = _add_message(
            db,
            uid=1,
            message_id="<in@example.com>",
            direction="INBOUND",
            sent_at=NOW - timedelta(days=10),
        )
        _add_analysis(db, gmail_message_id=inbound.id, matched_job_id=job.id)

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None

    def test_job_with_no_matched_correspondence_at_all_is_not_eligible(self, db):
        job = _add_job(db)
        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None


class TestDelayNotElapsed:
    def test_recent_outbound_message_is_not_yet_due(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=1)
        )
        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None
        assert result.due_at is not None
        assert result.due_at > NOW


class TestLaterInboundReplySuppressed:
    def test_reply_after_outbound_message_suppresses_follow_up(self, db):
        job, outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )
        reply = _add_message(
            db,
            uid=2,
            message_id="<reply@example.com>",
            in_reply_to="<out@example.com>",
            references=("<out@example.com>",),
            direction="INBOUND",
            sent_at=NOW - timedelta(days=9),
        )
        assert reply.thread_id == outbound.thread_id
        _add_analysis(db, gmail_message_id=reply.id, matched_job_id=job.id)

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None


class TestEligibleProposalCreation:
    def test_eligible_job_creates_a_persisted_proposal(self, db):
        job, outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "ELIGIBLE"
        assert result.created is True
        assert result.proposal is not None
        assert result.proposal.job_id == job.id
        assert result.proposal.anchor_gmail_message_id == outbound.id
        assert result.proposal.status == "PROPOSED"
        assert result.proposal.requires_human_review is True
        # Trusted job source (bundesagentur) -> real facts, no placeholders.
        assert "Backend Engineer" in result.proposal.subject
        assert "Globex" in result.proposal.subject
        # No candidate profile was seeded -> the only missing fact is the
        # candidate name (never a fabricated one).
        assert result.proposal.missing_fields == [
            "candidate name (not confirmed in candidate profile)"
        ]


class TestDuplicateScanIdempotent:
    def test_evaluating_the_same_job_twice_does_not_duplicate_the_proposal(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )

        first = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
        second = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert first.created is True
        assert second.created is False
        assert first.proposal.id == second.proposal.id

    def test_scanning_twice_via_list_due_follow_ups_does_not_duplicate(self, db):
        _seed_applied_job_with_outbound_anchor(db, outbound_sent_at=NOW - timedelta(days=10))

        first_summary = list_due_follow_ups(db, ACCOUNT, settings=SETTINGS, now=NOW)
        second_summary = list_due_follow_ups(db, ACCOUNT, settings=SETTINGS, now=NOW)

        assert first_summary.proposals_created == 1
        assert second_summary.proposals_created == 0
        assert first_summary.eligible == 1
        assert second_summary.eligible == 1


class TestNoInventedFacts:
    def test_untrusted_job_source_produces_placeholder_not_invented_facts(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10), source="xing"
        )

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "ELIGIBLE"
        assert "Backend Engineer" not in result.proposal.subject
        assert "Globex" not in result.proposal.subject
        assert "[position/company unknown" in result.proposal.subject
        assert any("matched job/company" in field for field in result.proposal.missing_fields)

    def test_no_candidate_profile_produces_name_placeholder(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.proposal.body.strip().endswith("[Your Name]")
        assert any("candidate name" in field for field in result.proposal.missing_fields)


class TestThreadJobAmbiguity:
    """S7E-005 (Codex remediation): a Gmail thread decisively matched to
    MORE THAN ONE job must never be reused as if it belonged exclusively
    to just one of them.
    """

    def test_thread_matched_to_two_jobs_is_not_eligible_for_either(self, db):
        job_a = _add_job(db, uid=1)
        job_b = _add_job(db, uid=2)
        outbound = _add_message(
            db,
            uid=1,
            message_id="<out@example.com>",
            direction="OUTBOUND",
            sent_at=NOW - timedelta(days=10),
        )
        reply = _add_message(
            db,
            uid=2,
            message_id="<reply@example.com>",
            in_reply_to="<out@example.com>",
            references=("<out@example.com>",),
            direction="INBOUND",
            sent_at=NOW - timedelta(days=9),
        )
        assert outbound.thread_id == reply.thread_id
        _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job_a.id)
        _add_analysis(db, gmail_message_id=reply.id, matched_job_id=job_b.id)

        result_a = evaluate_follow_up_for_job(db, ACCOUNT, job_a.id, settings=SETTINGS, now=NOW)
        result_b = evaluate_follow_up_for_job(db, ACCOUNT, job_b.id, settings=SETTINGS, now=NOW)

        assert result_a.eligibility == "NOT_ELIGIBLE"
        assert result_a.proposal is None
        assert "ambiguous" in result_a.reason.lower() or "decisively matched" in result_a.reason
        assert result_b.eligibility == "NOT_ELIGIBLE"
        assert result_b.proposal is None


class TestKeysetPagination:
    """S7E-006 (Codex remediation): a backlog larger than one scan's
    `limit` must be fully reachable across repeated calls via
    `after_job_id`/`next_cursor` — never the same offset=0 slice forever.
    """

    def test_next_cursor_advances_and_reaches_every_job(self, db):
        job_one, _ = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )
        job_two = _add_job(db, uid=2)
        outbound_two = _add_message(
            db,
            uid=3,
            message_id="<out2@example.com>",
            direction="OUTBOUND",
            sent_at=NOW - timedelta(days=10),
        )
        _add_analysis(db, gmail_message_id=outbound_two.id, matched_job_id=job_two.id)

        first_page = list_due_follow_ups(db, ACCOUNT, limit=1, settings=SETTINGS, now=NOW)
        assert first_page.scanned == 1
        assert first_page.next_cursor == job_one.id

        second_page = list_due_follow_ups(
            db, ACCOUNT, after_job_id=first_page.next_cursor, limit=1, settings=SETTINGS, now=NOW
        )
        assert second_page.scanned == 1
        assert second_page.results[0].job_id == job_two.id
        assert second_page.next_cursor == job_two.id

        third_page = list_due_follow_ups(
            db, ACCOUNT, after_job_id=second_page.next_cursor, limit=1, settings=SETTINGS, now=NOW
        )
        # No APPLIED job with id > job_two.id -> the backlog is drained.
        assert third_page.scanned == 0
        assert third_page.next_cursor is None

    def test_full_page_at_end_of_backlog_still_terminates_on_next_call(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )
        first_page = list_due_follow_ups(db, ACCOUNT, limit=1, settings=SETTINGS, now=NOW)
        assert first_page.next_cursor == job.id

        drained_page = list_due_follow_ups(
            db, ACCOUNT, after_job_id=first_page.next_cursor, limit=1, settings=SETTINGS, now=NOW
        )
        assert drained_page.scanned == 0
        assert drained_page.next_cursor is None


class TestRecipientSafety:
    """S7E-008 (Codex remediation): no safe, unambiguous recipient ->
    NOT_ELIGIBLE, never a guessed/fallback address."""

    def test_empty_to_addresses_is_not_eligible(self, db):
        job = _add_job(db)
        outbound = _add_message(
            db,
            uid=1,
            message_id="<out@example.com>",
            direction="OUTBOUND",
            sent_at=NOW - timedelta(days=10),
            to_addresses=(),
        )
        _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None
        assert "recipient" in result.reason.lower()

    def test_multiple_distinct_recipients_is_not_eligible(self, db):
        job = _add_job(db)
        outbound = _add_message(
            db,
            uid=1,
            message_id="<out@example.com>",
            direction="OUTBOUND",
            sent_at=NOW - timedelta(days=10),
            to_addresses=("hr@acme.example.com", "jobs@acme.example.com"),
        )
        _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None

    def test_self_addressed_recipient_is_not_eligible(self, db):
        job = _add_job(db)
        outbound = _add_message(
            db,
            uid=1,
            message_id="<out@example.com>",
            direction="OUTBOUND",
            sent_at=NOW - timedelta(days=10),
            to_addresses=(ACCOUNT,),
        )
        _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None

    def test_malformed_recipient_is_not_eligible(self, db):
        job = _add_job(db)
        outbound = _add_message(
            db,
            uid=1,
            message_id="<out@example.com>",
            direction="OUTBOUND",
            sent_at=NOW - timedelta(days=10),
            to_addresses=("not-an-email",),
        )
        _add_analysis(db, gmail_message_id=outbound.id, matched_job_id=job.id)

        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert result.eligibility == "NOT_ELIGIBLE"
        assert result.proposal is None

    def test_valid_single_recipient_is_pinned_on_the_proposal(self, db):
        job, outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )
        result = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
        assert result.eligibility == "ELIGIBLE"
        assert result.proposal.recipient == "hr@acme.example.com"


class TestProposalStaleness:
    """S7E-009 (Codex remediation): a changed trusted input (here, the
    candidate profile gaining a confirmed name between two evaluations)
    must produce a NEW proposal revision, never silently reuse the old
    one — and the fingerprint must actually differ.
    """

    def test_candidate_profile_change_produces_a_new_proposal_revision(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )

        first = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
        assert first.eligibility == "ELIGIBLE"
        assert first.created is True
        assert first.proposal.missing_fields == [
            "candidate name (not confirmed in candidate profile)"
        ]

        profile = CandidateProfileRecord(id=1, profile_version=1)
        db.add(profile)
        db.commit()
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=1, first_name="Jane", last_name="Doe"
            ),
        )

        second = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert second.eligibility == "ELIGIBLE"
        assert second.created is True
        assert second.proposal.id != first.proposal.id
        assert second.proposal.input_fingerprint != first.proposal.input_fingerprint
        assert "Jane Doe" in second.proposal.body
        assert second.proposal.missing_fields == []

    def test_unchanged_inputs_still_return_the_same_revision(self, db):
        job, _outbound = _seed_applied_job_with_outbound_anchor(
            db, outbound_sent_at=NOW - timedelta(days=10)
        )
        first = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)
        second = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=SETTINGS, now=NOW)

        assert first.proposal.id == second.proposal.id
        assert first.proposal.input_fingerprint == second.proposal.input_fingerprint
        assert second.created is False
