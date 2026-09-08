"""Stage 8E tests for `app.db.telegram_digest_repository` -- the
persisted once-per-(account_key, digest_date) claim/CAS underlying the
optional daily Telegram digest. Mirrors
tests/test_automation_schedule_repository.py's real
two-Session/independent-engine-connection proof style, applied to the
digest delivery claim instead of the schedule-slot claim.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.telegram_digest_repository import (
    claim_delivery,
    get_delivery,
    mark_failed,
    mark_sent,
    mark_uncertain,
    retry_delivery,
)

ACCOUNT = "me@example.com"
DAY = date(2026, 9, 8)


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_telegram_digest_repository.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class TestClaimDelivery:
    def test_first_claim_wins_and_starts_pending(self, session_factory):
        db = session_factory()
        try:
            record, claimed = claim_delivery(db, ACCOUNT, DAY)
            assert claimed is True
            assert record.status == "PENDING"
            assert record.account_key == ACCOUNT
            assert record.digest_date == DAY
        finally:
            db.close()

    def test_concurrent_claim_for_same_account_and_date_has_exactly_one_winner(
        self, session_factory
    ):
        """Two independent sessions racing the first claim for the SAME
        (account_key, digest_date) must never both win -- the UNIQUE
        constraint + IntegrityError-catch idiom collapses them to one
        row, mirroring app.db.automation_schedule_repository's own
        `claim_due_schedule` guarantee. This is the actual restart/
        multi-worker duplicate-send guard Stage 8E relies on."""
        session_a = session_factory()
        session_b = session_factory()
        try:
            record_a, claimed_a = claim_delivery(session_a, ACCOUNT, DAY)
            record_b, claimed_b = claim_delivery(session_b, ACCOUNT, DAY)

            assert claimed_a is True
            assert claimed_b is False
            assert record_a.id == record_b.id
        finally:
            session_a.close()
            session_b.close()

    def test_different_dates_claim_independently(self, session_factory):
        db = session_factory()
        try:
            _record_a, claimed_a = claim_delivery(db, ACCOUNT, DAY)
            _record_b, claimed_b = claim_delivery(db, ACCOUNT, DAY + timedelta(days=1))
            assert claimed_a is True
            assert claimed_b is True
        finally:
            db.close()

    def test_different_accounts_claim_independently_for_same_date(self, session_factory):
        db = session_factory()
        try:
            _record_a, claimed_a = claim_delivery(db, ACCOUNT, DAY)
            _record_b, claimed_b = claim_delivery(db, "other@example.com", DAY)
            assert claimed_a is True
            assert claimed_b is True
        finally:
            db.close()


class TestStateTransitions:
    def test_mark_sent_from_pending_succeeds_and_is_terminal(self, session_factory):
        db = session_factory()
        try:
            record, _claimed = claim_delivery(db, ACCOUNT, DAY)
            assert mark_sent(db, record) is True
            reloaded = get_delivery(db, ACCOUNT, DAY)
            assert reloaded.status == "SENT"
            assert reloaded.sent_at is not None
        finally:
            db.close()

    def test_mark_failed_from_pending_succeeds(self, session_factory):
        db = session_factory()
        try:
            record, _claimed = claim_delivery(db, ACCOUNT, DAY)
            assert mark_failed(db, record, last_error="FAILED") is True
            reloaded = get_delivery(db, ACCOUNT, DAY)
            assert reloaded.status == "FAILED"
        finally:
            db.close()

    def test_mark_uncertain_from_pending_succeeds(self, session_factory):
        db = session_factory()
        try:
            record, _claimed = claim_delivery(db, ACCOUNT, DAY)
            assert mark_uncertain(db, record, last_error="UNCERTAIN") is True
            reloaded = get_delivery(db, ACCOUNT, DAY)
            assert reloaded.status == "UNCERTAIN"
        finally:
            db.close()

    def test_retry_delivery_only_matches_failed_status(self, session_factory):
        db = session_factory()
        try:
            record, _claimed = claim_delivery(db, ACCOUNT, DAY)
            mark_failed(db, record, last_error="boom")

            won = retry_delivery(db, record)

            assert won is True
            assert get_delivery(db, ACCOUNT, DAY).status == "PENDING"
        finally:
            db.close()

    def test_retry_delivery_never_resurrects_uncertain(self, session_factory):
        """Terminal per TelegramDigestDeliveryRecord's docstring -- an
        UNCERTAIN outcome is never automatically retried, to avoid
        risking a real duplicate message landing in the operator's
        chat."""
        db = session_factory()
        try:
            record, _claimed = claim_delivery(db, ACCOUNT, DAY)
            mark_uncertain(db, record, last_error="ambiguous")

            won = retry_delivery(db, record)

            assert won is False
            assert get_delivery(db, ACCOUNT, DAY).status == "UNCERTAIN"
        finally:
            db.close()

    def test_retry_delivery_never_resurrects_sent(self, session_factory):
        db = session_factory()
        try:
            record, _claimed = claim_delivery(db, ACCOUNT, DAY)
            mark_sent(db, record)

            won = retry_delivery(db, record)

            assert won is False
            assert get_delivery(db, ACCOUNT, DAY).status == "SENT"
        finally:
            db.close()

    def test_concurrent_retry_of_the_same_failed_row_has_exactly_one_winner(self, session_factory):
        session_a = session_factory()
        session_b = session_factory()
        try:
            record, _claimed = claim_delivery(session_a, ACCOUNT, DAY)
            mark_failed(session_a, record, last_error="boom")

            record_a = get_delivery(session_a, ACCOUNT, DAY)
            record_b = get_delivery(session_b, ACCOUNT, DAY)

            won_a = retry_delivery(session_a, record_a)
            won_b = retry_delivery(session_b, record_b)

            assert won_a != won_b  # exactly one of the two wins
        finally:
            session_a.close()
            session_b.close()
