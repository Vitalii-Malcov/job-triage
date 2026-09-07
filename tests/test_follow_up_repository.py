"""Tests for app.db.follow_up_repository — matched-thread resolution,
thread<->job ambiguity (S7E-005), thread message timestamp extraction
(S7E-003/004), and idempotent/account-scoped follow-up proposal
persistence (S7E-009).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.follow_up_repository import (
    get_follow_up_proposal_by_anchor_and_fingerprint,
    get_matched_job_ids_for_thread,
    get_matched_thread_ids_for_job,
    get_or_create_follow_up_proposal,
    get_thread_message_infos,
)
from app.db.gmail_repository import upsert_message
from app.db.models import GmailMessageAnalysisRecord, JobRecord
from app.providers.email.base import ParsedGmailMessage

ACCOUNT_A = "a@example.com"
ACCOUNT_B = "b@example.com"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'follow_up_repository.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _add_job(db, **overrides) -> JobRecord:
    defaults = dict(
        fingerprint=f"fp-{id(overrides)}",
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
    defaults.update(overrides)
    job = JobRecord(**defaults)
    db.add(job)
    db.commit()
    return job


_UNSET = object()


def _add_message(
    db,
    *,
    account_key=ACCOUNT_A,
    uid=1,
    message_id="<msg1@example.com>",
    direction="OUTBOUND",
    sent_at=_UNSET,
    **overrides,
):
    data = dict(
        account_key=account_key,
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=message_id,
        in_reply_to=None,
        references=(),
        from_address=account_key,
        from_display_name=None,
        to_addresses=("hr@acme.example.com",),
        cc_addresses=(),
        subject="My application",
        sent_at=datetime.now(UTC) if sent_at is _UNSET else sent_at,
        direction=direction,
        body_plain="I am applying for the Backend Engineer role at Globex.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    data.update(overrides)
    record, _created = upsert_message(db, ParsedGmailMessage(**data))
    return record


def _add_analysis(
    db,
    *,
    gmail_message_id,
    account_key=ACCOUNT_A,
    matched_job_id,
    match_type="APPLICATION",
    analysis_version=1,
    input_fingerprint="fp",
) -> GmailMessageAnalysisRecord:
    record = GmailMessageAnalysisRecord(
        account_key=account_key,
        gmail_message_id=gmail_message_id,
        analysis_version=analysis_version,
        input_fingerprint=input_fingerprint,
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
    db.add(record)
    db.commit()
    return record


class TestMatchedThreadResolution:
    def test_no_analysis_yields_no_matched_threads(self, db):
        job = _add_job(db)
        assert get_matched_thread_ids_for_job(db, ACCOUNT_A, job.id) == frozenset()

    def test_decisive_match_resolves_the_thread(self, db):
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>")
        _add_analysis(db, gmail_message_id=message.id, matched_job_id=job.id)

        thread_ids = get_matched_thread_ids_for_job(db, ACCOUNT_A, job.id)
        assert thread_ids == frozenset({message.thread_id})

    def test_ambiguous_or_unmatched_analysis_does_not_resolve_a_thread(self, db):
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>")
        _add_analysis(db, gmail_message_id=message.id, matched_job_id=None, match_type="UNMATCHED")

        assert get_matched_thread_ids_for_job(db, ACCOUNT_A, job.id) == frozenset()

    def test_only_latest_analysis_revision_counts(self, db):
        """An old revision matching this job, superseded by a newer
        revision that does NOT, must not resolve the thread."""
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>")
        _add_analysis(db, gmail_message_id=message.id, matched_job_id=job.id, analysis_version=1)
        _add_analysis(
            db,
            gmail_message_id=message.id,
            matched_job_id=None,
            match_type="UNMATCHED",
            analysis_version=2,
        )

        assert get_matched_thread_ids_for_job(db, ACCOUNT_A, job.id) == frozenset()

    def test_cross_account_analysis_is_not_visible(self, db):
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>", account_key=ACCOUNT_B)
        _add_analysis(db, gmail_message_id=message.id, matched_job_id=job.id, account_key=ACCOUNT_B)

        assert get_matched_thread_ids_for_job(db, ACCOUNT_A, job.id) == frozenset()
        assert get_matched_thread_ids_for_job(db, ACCOUNT_B, job.id) == frozenset(
            {message.thread_id}
        )


class TestMatchedJobIdsForThread:
    """S7E-005 (Codex remediation): the reverse thread<->job ambiguity
    check — a thread decisively matched to more than one job must never be
    silently attributed to just one of them.
    """

    def test_single_matched_job_resolves(self, db):
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>")
        _add_analysis(db, gmail_message_id=message.id, matched_job_id=job.id)

        assert get_matched_job_ids_for_thread(db, ACCOUNT_A, message.thread_id) == frozenset(
            {job.id}
        )

    def test_two_jobs_matched_to_same_thread_is_ambiguous(self, db):
        job_a = _add_job(db, fingerprint="fp-a")
        job_b = _add_job(db, fingerprint="fp-b")
        msg_a = _add_message(db, uid=1, message_id="<a@example.com>")
        msg_b = _add_message(
            db,
            uid=2,
            message_id="<b@example.com>",
            in_reply_to="<a@example.com>",
            references=("<a@example.com>",),
        )
        assert msg_a.thread_id == msg_b.thread_id

        _add_analysis(db, gmail_message_id=msg_a.id, matched_job_id=job_a.id)
        _add_analysis(db, gmail_message_id=msg_b.id, matched_job_id=job_b.id)

        matched = get_matched_job_ids_for_thread(db, ACCOUNT_A, msg_a.thread_id)
        assert matched == frozenset({job_a.id, job_b.id})

    def test_no_analysis_yields_empty(self, db):
        message = _add_message(db, uid=1, message_id="<a@example.com>")
        assert get_matched_job_ids_for_thread(db, ACCOUNT_A, message.thread_id) == frozenset()

    def test_cross_account_not_visible(self, db):
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>", account_key=ACCOUNT_B)
        _add_analysis(db, gmail_message_id=message.id, matched_job_id=job.id, account_key=ACCOUNT_B)

        assert get_matched_job_ids_for_thread(db, ACCOUNT_A, message.thread_id) == frozenset()
        assert get_matched_job_ids_for_thread(db, ACCOUNT_B, message.thread_id) == frozenset(
            {job.id}
        )


class TestThreadMessageInfos:
    """S7E-003/004 (Codex remediation): only the latest OUTBOUND and
    latest INBOUND message are ever returned — never a bounded historical
    scan — and ordering is by trusted `received_at`, never the
    sender-controlled `sent_at` (RFC Date header).
    """

    def test_returns_latest_outbound_and_latest_inbound(self, db):
        outbound_time = datetime(2026, 1, 1, tzinfo=UTC)
        inbound_time = datetime(2026, 1, 5, tzinfo=UTC)
        out_msg = _add_message(
            db,
            uid=1,
            message_id="<root@example.com>",
            direction="OUTBOUND",
            sent_at=outbound_time,
        )
        in_msg = _add_message(
            db,
            uid=2,
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references=("<root@example.com>",),
            direction="INBOUND",
            sent_at=inbound_time,
        )
        assert out_msg.thread_id == in_msg.thread_id

        infos = get_thread_message_infos(db, ACCOUNT_A, out_msg.thread_id)
        by_direction = {info.direction: info for info in infos}
        assert by_direction["OUTBOUND"].gmail_message_id == out_msg.id
        assert by_direction["INBOUND"].gmail_message_id == in_msg.id

    def test_uses_received_at_not_sent_at_for_ordering(self, db):
        """A skewed/backdated `sent_at` (attacker-controlled RFC Date
        header) must never override the real, trusted `received_at`
        write-time ordering — S7E-004."""
        out_msg = _add_message(
            db,
            uid=1,
            message_id="<root@example.com>",
            direction="OUTBOUND",
            sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # A reply whose Date header claims to be BEFORE the outbound
        # message (backdated/skewed) but was actually received (synced)
        # afterwards — received_at (real wall-clock write time) must still
        # correctly identify it as the latest inbound message.
        in_msg = _add_message(
            db,
            uid=2,
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references=("<root@example.com>",),
            direction="INBOUND",
            sent_at=datetime(2020, 1, 1, tzinfo=UTC),
        )

        infos = get_thread_message_infos(db, ACCOUNT_A, out_msg.thread_id)
        by_direction = {info.direction: info for info in infos}
        stored_inbound = db.get(type(in_msg), in_msg.id)
        assert by_direction["INBOUND"].timestamp.replace(
            tzinfo=None
        ) == stored_inbound.received_at.replace(tzinfo=None)
        # The pure eligibility engine compares against this timestamp —
        # confirm it is NOT the (much older) sent_at value.
        assert by_direction["INBOUND"].timestamp.replace(tzinfo=None) != datetime(2020, 1, 1)

    def test_missing_sent_at_still_resolves_via_received_at(self, db):
        message = _add_message(db, uid=1, message_id="<a@example.com>", sent_at=None)
        infos = get_thread_message_infos(db, ACCOUNT_A, message.thread_id)
        assert infos[0].direction == "OUTBOUND"
        assert infos[0].timestamp is not None
        stored = db.get(type(message), message.id)
        assert infos[0].timestamp.replace(tzinfo=None) == stored.received_at.replace(tzinfo=None)

    def test_only_the_latest_message_per_direction_is_returned_regardless_of_thread_size(self, db):
        """A long thread (far more than the old 200-message cap) must
        still correctly surface only the true latest OUTBOUND/INBOUND
        message — S7E-003: there is no historical-scan limit to exceed at
        all with the new direct-query approach."""
        root = _add_message(
            db,
            uid=1,
            message_id="<root@example.com>",
            direction="OUTBOUND",
            sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # Simulate 5 older outbound messages (would have been long-ago
        # history in a real >200-message thread) followed by the true
        # latest one.
        latest_outbound = None
        for i in range(5):
            latest_outbound = _add_message(
                db,
                uid=10 + i,
                message_id=f"<out{i}@example.com>",
                in_reply_to="<root@example.com>",
                references=("<root@example.com>",),
                direction="OUTBOUND",
                sent_at=datetime(2026, 1, 2 + i, tzinfo=UTC),
            )

        infos = get_thread_message_infos(db, ACCOUNT_A, root.thread_id)
        by_direction = {info.direction: info for info in infos}
        assert by_direction["OUTBOUND"].gmail_message_id == latest_outbound.id


class TestFollowUpProposalIdempotency:
    def _create(
        self,
        db,
        *,
        account_key,
        job,
        message,
        input_fingerprint="fp-1",
        recipient="hr@acme.example.com",
    ):
        return get_or_create_follow_up_proposal(
            db,
            account_key=account_key,
            job_id=job.id,
            gmail_thread_id=message.thread_id,
            anchor_gmail_message_id=message.id,
            eligibility_reason="test reason",
            due_at=datetime.now(UTC) - timedelta(days=1),
            subject="Follow up",
            body="body text",
            language="en",
            missing_fields=(),
            recipient=recipient,
            input_fingerprint=input_fingerprint,
            provider="deterministic_template",
            generator_version="v1",
        )

    def test_second_call_for_same_anchor_and_fingerprint_returns_existing_row(self, db):
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>")

        record_one, created_one = self._create(db, account_key=ACCOUNT_A, job=job, message=message)
        record_two, created_two = self._create(db, account_key=ACCOUNT_A, job=job, message=message)

        assert created_one is True
        assert created_two is False
        assert record_one.id == record_two.id

    def test_changed_fingerprint_for_same_anchor_creates_a_new_revision(self, db):
        """S7E-009: a changed trusted input (different fingerprint) for
        the SAME anchor must never reuse the old, now-stale proposal."""
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>")

        record_one, created_one = self._create(
            db, account_key=ACCOUNT_A, job=job, message=message, input_fingerprint="fp-1"
        )
        record_two, created_two = self._create(
            db, account_key=ACCOUNT_A, job=job, message=message, input_fingerprint="fp-2"
        )

        assert created_one is True
        assert created_two is True
        assert record_one.id != record_two.id
        assert (
            get_follow_up_proposal_by_anchor_and_fingerprint(db, ACCOUNT_A, message.id, "fp-1").id
            == record_one.id
        )
        assert (
            get_follow_up_proposal_by_anchor_and_fingerprint(db, ACCOUNT_A, message.id, "fp-2").id
            == record_two.id
        )

    def test_different_accounts_can_each_have_their_own_proposal_for_same_message_id(self, db):
        """Not realistic (a message id is account-scoped identity-wise
        already), but confirms the UNIQUE constraint is truly scoped by
        account_key, not merely by (anchor_gmail_message_id, fingerprint)."""
        job = _add_job(db)
        message_a = _add_message(db, uid=1, message_id="<a@example.com>", account_key=ACCOUNT_A)
        message_b = _add_message(db, uid=1, message_id="<b@example.com>", account_key=ACCOUNT_B)

        _, created_a = self._create(db, account_key=ACCOUNT_A, job=job, message=message_a)
        _, created_b = self._create(db, account_key=ACCOUNT_B, job=job, message=message_b)

        assert created_a is True
        assert created_b is True

    def test_get_by_anchor_and_fingerprint_is_account_scoped(self, db):
        job = _add_job(db)
        message = _add_message(db, uid=1, message_id="<a@example.com>", account_key=ACCOUNT_A)
        self._create(db, account_key=ACCOUNT_A, job=job, message=message, input_fingerprint="fp-1")

        assert (
            get_follow_up_proposal_by_anchor_and_fingerprint(db, ACCOUNT_A, message.id, "fp-1")
            is not None
        )
        assert (
            get_follow_up_proposal_by_anchor_and_fingerprint(db, ACCOUNT_B, message.id, "fp-1")
            is None
        )
