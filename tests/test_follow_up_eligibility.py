"""Unit tests for the pure Stage 7E eligibility rule
(app.services.follow_up_eligibility.evaluate_follow_up_eligibility) — no
DB access, mirrors tests/test_email_matching.py's style.
"""

from datetime import UTC, datetime, timedelta

from app.services.follow_up_eligibility import ThreadMessageInfo, evaluate_follow_up_eligibility

DELAY = timedelta(days=7)
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def _outbound(msg_id: int, when: datetime, *, trusted: bool = True) -> ThreadMessageInfo:
    return ThreadMessageInfo(
        gmail_message_id=msg_id, direction="OUTBOUND", timestamp=when, timestamp_is_trusted=trusted
    )


def _inbound(msg_id: int, when: datetime, *, trusted: bool = True) -> ThreadMessageInfo:
    return ThreadMessageInfo(
        gmail_message_id=msg_id, direction="INBOUND", timestamp=when, timestamp_is_trusted=trusted
    )


class TestJobStatusGate:
    def test_not_applied_status_is_not_eligible(self):
        result = evaluate_follow_up_eligibility(
            job_status="NEW",
            matched_thread_count=1,
            thread_messages=[_outbound(1, NOW - timedelta(days=10))],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "NEW" in result.reason
        assert result.anchor_gmail_message_id is None

    def test_terminal_statuses_are_suppressed(self):
        for status in ("INTERVIEW", "REJECTED", "OFFER", "WITHDRAWN"):
            result = evaluate_follow_up_eligibility(
                job_status=status,
                matched_thread_count=1,
                thread_messages=[_outbound(1, NOW - timedelta(days=10))],
                follow_up_delay=DELAY,
                now=NOW,
            )
            assert result.eligibility == "NOT_ELIGIBLE"
            assert status in result.reason

    def test_applied_status_with_no_other_blockers_is_eligible(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[_outbound(1, NOW - timedelta(days=10))],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "ELIGIBLE"
        assert result.anchor_gmail_message_id == 1
        assert result.due_at == NOW - timedelta(days=10) + DELAY


class TestMatchedThreadAmbiguity:
    def test_zero_matched_threads_is_not_eligible(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=0,
            thread_messages=[],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "No Stage 7B-matched" in result.reason
        assert result.anchor_gmail_message_id is None

    def test_multiple_matched_threads_is_not_eligible_never_guesses(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=2,
            thread_messages=[_outbound(1, NOW - timedelta(days=10))],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "ambiguous" in result.reason
        assert result.anchor_gmail_message_id is None


class TestNoOutboundAnchor:
    def test_no_messages_at_all_is_not_eligible(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "OUTBOUND" in result.reason
        assert result.anchor_gmail_message_id is None

    def test_inbound_only_thread_is_not_eligible(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[_inbound(1, NOW - timedelta(days=10))],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "OUTBOUND" in result.reason


class TestLaterInboundReplySuppresses:
    def test_reply_after_outbound_suppresses_follow_up(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[
                _outbound(1, NOW - timedelta(days=10)),
                _inbound(2, NOW - timedelta(days=9)),
            ],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "reply was received" in result.reason
        assert result.anchor_gmail_message_id == 1

    def test_reply_before_outbound_does_not_suppress(self):
        """An earlier inbound message (e.g. the original job posting/alert)
        that predates the candidate's own outbound message must not
        suppress a follow-up — only a reply AFTER the anchor counts."""
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[
                _inbound(1, NOW - timedelta(days=20)),
                _outbound(2, NOW - timedelta(days=10)),
            ],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "ELIGIBLE"
        assert result.anchor_gmail_message_id == 2

    def test_uses_latest_outbound_message_as_anchor(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[
                _outbound(1, NOW - timedelta(days=30)),
                _outbound(2, NOW - timedelta(days=10)),
            ],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "ELIGIBLE"
        assert result.anchor_gmail_message_id == 2


class TestUntrustedChronologyFailsClosed:
    """S7E-013 (Codex re-review, final safety fix): `timestamp` alone is
    not enough to trust — a legacy/backfilled row or a message whose IMAP
    fetch never returned a parseable INTERNALDATE must never become
    ELIGIBLE just because its (untrustworthy) timestamp happens to look
    old enough / early enough.
    """

    def test_untrusted_anchor_is_not_eligible(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[_outbound(1, NOW - timedelta(days=10), trusted=False)],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "not a provider-verified" in result.reason
        assert result.anchor_gmail_message_id == 1
        assert result.due_at is None

    def test_untrusted_inbound_suppresses_even_though_it_looks_earlier(self):
        """An untrusted inbound message that LOOKS like it predates the
        (trusted) outbound anchor must still fail closed — its real
        arrival order relative to the anchor cannot be verified at all,
        so this must never be treated as safely "before" it."""
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[
                _inbound(1, NOW - timedelta(days=20), trusted=False),
                _outbound(2, NOW - timedelta(days=10), trusted=True),
            ],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "not a provider-verified" in result.reason
        assert result.anchor_gmail_message_id == 2

    def test_trusted_anchor_and_trusted_inbound_reach_normal_eligibility(self):
        """Sanity check: once both are trusted, normal eligibility logic
        (not the trust gate) decides the outcome."""
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[
                _inbound(1, NOW - timedelta(days=20), trusted=True),
                _outbound(2, NOW - timedelta(days=10), trusted=True),
            ],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "ELIGIBLE"
        assert result.anchor_gmail_message_id == 2


class TestDelayNotElapsed:
    def test_delay_not_yet_elapsed_is_not_eligible(self):
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[_outbound(1, NOW - timedelta(days=2))],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "NOT_ELIGIBLE"
        assert "delay has not elapsed" in result.reason
        assert result.anchor_gmail_message_id == 1
        assert result.due_at == NOW - timedelta(days=2) + DELAY

    def test_delay_exactly_elapsed_is_eligible(self):
        anchor_time = NOW - DELAY
        result = evaluate_follow_up_eligibility(
            job_status="APPLIED",
            matched_thread_count=1,
            thread_messages=[_outbound(1, anchor_time)],
            follow_up_delay=DELAY,
            now=NOW,
        )
        assert result.eligibility == "ELIGIBLE"
