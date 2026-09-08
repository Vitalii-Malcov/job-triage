"""Stage 8E tests for app.services.telegram_digest.build_digest_text --
digest content/counters, the Telegram message-length bound, and the
hard privacy invariant that account_key (an email address), draft/
follow-up text, and job/recruiter content never appear in the rendered
text.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    AutomationRunRecord,
    FollowUpApprovalRecord,
    FollowUpProposalRecord,
    ResponseDraftApprovalRecord,
    ResponseDraftRecord,
)
from app.services.telegram import TELEGRAM_MESSAGE_HARD_LIMIT
from app.services.telegram_digest import DIGEST_REPLY_SOFT_LIMIT, build_digest_text

ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "test_telegram_digest.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _seed_run(db, *, account_key=ACCOUNT, status="COMPLETED", results=None, finished=True):
    run = AutomationRunRecord(
        account_key=account_key,
        status=status,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC) if finished else None,
        results_json=json.dumps(results or {}),
    )
    db.add(run)
    db.commit()
    return run


def _seed_response_draft(db, *, account_key=ACCOUNT, status="PROPOSED", gmail_message_id=1):
    draft = ResponseDraftRecord(
        account_key=account_key,
        gmail_message_id=gmail_message_id,
        analysis_id=1,
        analysis_version=1,
        candidate_profile_version=1,
        classification="INTERVIEW_INVITATION",
        status=status,
        subject="hi" if status == "PROPOSED" else None,
        body="body" if status == "PROPOSED" else None,
        language="en" if status == "PROPOSED" else None,
        provider="local",
        generator_version="v1",
    )
    db.add(draft)
    db.commit()
    return draft


def _seed_follow_up(db, *, account_key=ACCOUNT, anchor_gmail_message_id=1, fingerprint="fp-1"):
    proposal = FollowUpProposalRecord(
        account_key=account_key,
        job_id=1,
        gmail_thread_id=1,
        anchor_gmail_message_id=anchor_gmail_message_id,
        eligibility_reason="due",
        due_at=datetime.now(UTC),
        subject="Following up on my application",
        body="Dear recruiter, ...",
        language="en",
        recipient="hr@acme.example.com",
        input_fingerprint=fingerprint,
        provider="local",
        generator_version="v1",
        status="PROPOSED",
    )
    db.add(proposal)
    db.commit()
    return proposal


class TestNoRunYet:
    def test_reports_no_run_without_raising(self, db):
        text = build_digest_text(db, ACCOUNT)
        assert "Latest run: none yet" in text
        assert "Automation digest" in text


class TestLatestRunAndStepCounters:
    def test_includes_latest_run_id_status_and_step_counters(self, db):
        _seed_run(
            db,
            results={
                "bundesagentur": {
                    "status": "ok",
                    "counters": {"fetched": 10, "created": 3, "failed": 0},
                },
                "gmail_response_drafts": {
                    "status": "partial",
                    "counters": {"analyzed": 5, "drafted": 2},
                    "failures": [{"gmail_message_id": 9, "phase": "analysis", "error_type": "X"}],
                },
            },
        )

        text = build_digest_text(db, ACCOUNT)

        assert "Latest run: #1 status=COMPLETED" in text
        assert "Bundesagentur collector: ok" in text
        assert "fetched=10" in text
        assert "created=3" in text
        assert "Gmail response drafts: partial" in text
        assert "analyzed=5" in text
        assert "failures=1" in text

    def test_uses_only_the_most_recent_run(self, db):
        _seed_run(db, results={"bundesagentur": {"status": "ok", "counters": {"fetched": 1}}})
        _seed_run(db, results={"bundesagentur": {"status": "failed", "counters": None}})

        text = build_digest_text(db, ACCOUNT)

        assert "Latest run: #2 status=COMPLETED" in text
        assert "Bundesagentur collector: failed" in text

    def test_running_run_shows_finished_as_running(self, db):
        _seed_run(db, status="RUNNING", finished=False)

        text = build_digest_text(db, ACCOUNT)

        assert "status=RUNNING" in text
        assert "finished=running" in text

    def test_only_reports_steps_actually_present_in_results(self, db):
        _seed_run(db, results={"xing": {"status": "ok", "counters": {"fetched": 2}}})

        text = build_digest_text(db, ACCOUNT)

        assert "XING collector" in text
        assert "Bundesagentur collector" not in text
        assert "Shortlist drafts" not in text
        assert "Follow-up proposals" not in text


class TestPendingHumanActionCounts:
    def test_pending_response_draft_awaiting_decision_is_counted_and_listed(self, db):
        draft = _seed_response_draft(db)

        text = build_digest_text(db, ACCOUNT)

        assert "Pending response-draft approvals: 1" in text
        assert f"#{draft.id}" in text

    def test_decided_response_draft_is_not_counted(self, db):
        draft = _seed_response_draft(db)
        db.add(
            ResponseDraftApprovalRecord(
                account_key=ACCOUNT,
                response_draft_id=draft.id,
                gmail_message_id=1,
                decision="APPROVED",
                pinned_subject="hi",
                pinned_body="body",
            )
        )
        db.commit()

        text = build_digest_text(db, ACCOUNT)

        assert "Pending response-draft approvals: 0" in text

    def test_no_response_recommended_draft_is_not_counted_as_pending(self, db):
        _seed_response_draft(db, status="NO_RESPONSE_RECOMMENDED")

        text = build_digest_text(db, ACCOUNT)

        assert "Pending response-draft approvals: 0" in text

    def test_pending_follow_up_awaiting_decision_is_counted_and_listed(self, db):
        proposal = _seed_follow_up(db)

        text = build_digest_text(db, ACCOUNT)

        assert "Pending follow-up approvals: 1" in text
        assert f"#{proposal.id}" in text

    def test_decided_follow_up_is_not_counted(self, db):
        proposal = _seed_follow_up(db)
        db.add(
            FollowUpApprovalRecord(
                account_key=ACCOUNT,
                follow_up_proposal_id=proposal.id,
                gmail_message_id=1,
                decision="APPROVED",
                pinned_subject="Following up",
                pinned_body="Dear recruiter, ...",
                pinned_recipient="hr@acme.example.com",
            )
        )
        db.commit()

        text = build_digest_text(db, ACCOUNT)

        assert "Pending follow-up approvals: 0" in text


class TestAccountIsolation:
    def test_pending_counts_never_leak_across_accounts(self, db):
        _seed_response_draft(db, account_key=OTHER_ACCOUNT, gmail_message_id=2)
        _seed_follow_up(
            db, account_key=OTHER_ACCOUNT, anchor_gmail_message_id=2, fingerprint="fp-2"
        )
        _seed_run(db, account_key=OTHER_ACCOUNT, results={"xing": {"status": "ok"}})

        text = build_digest_text(db, ACCOUNT)

        assert "Pending response-draft approvals: 0" in text
        assert "Pending follow-up approvals: 0" in text
        assert "Latest run: none yet" in text


class TestPrivacyBoundary:
    """DIGEST-001: the rendered text must never contain the account's
    own email address, or any draft/job/recruiter content -- only ids,
    counts, and fixed status strings (see
    app.services.telegram_digest's module docstring)."""

    def test_account_key_itself_never_appears_in_rendered_text(self, db):
        _seed_run(db, results={"bundesagentur": {"status": "ok", "counters": {"fetched": 1}}})
        _seed_response_draft(db)
        _seed_follow_up(db)

        text = build_digest_text(db, ACCOUNT)

        assert ACCOUNT not in text

    def test_draft_and_follow_up_content_never_appears(self, db):
        _seed_response_draft(db)
        _seed_follow_up(db)

        text = build_digest_text(db, ACCOUNT)

        forbidden_texts = (
            "Following up on my application",
            "Dear recruiter",
            "hr@acme.example.com",
        )
        for forbidden in forbidden_texts:
            assert forbidden not in text


class TestMessageLength:
    def test_stays_under_telegram_hard_limit_with_many_steps_and_failures(self, db):
        failures = [
            {"gmail_message_id": i, "phase": "analysis", "error_type": "RuntimeError"}
            for i in range(50)
        ]
        _seed_run(
            db,
            results={
                "bundesagentur": {"status": "ok", "counters": {"fetched": 999999}},
                "xing": {"status": "ok", "counters": {"fetched": 999999}},
                "shortlist_drafts": {"status": "partial", "counters": {"created": 10}},
                "gmail_response_drafts": {
                    "status": "partial",
                    "counters": {"analyzed": 999999},
                    "failures": failures,
                },
                "follow_up_proposals": {"status": "ok", "counters": {"eligible": 999999}},
            },
        )
        for i in range(300):
            _seed_response_draft(db, gmail_message_id=i + 100)

        text = build_digest_text(db, ACCOUNT)

        assert len(text) <= DIGEST_REPLY_SOFT_LIMIT
        assert len(text) < TELEGRAM_MESSAGE_HARD_LIMIT

    def test_truncation_is_deterministic_across_repeated_calls(self, db):
        failures = [
            {"gmail_message_id": i, "phase": "analysis", "error_type": "RuntimeError"}
            for i in range(50)
        ]
        _seed_run(
            db,
            results={
                "gmail_response_drafts": {
                    "status": "partial",
                    "counters": {"analyzed": 1},
                    "failures": failures,
                }
            },
        )

        first = build_digest_text(db, ACCOUNT)
        second = build_digest_text(db, ACCOUNT)

        assert first == second
