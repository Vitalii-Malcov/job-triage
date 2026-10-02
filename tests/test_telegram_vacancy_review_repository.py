"""Stage 9A: persistence of Telegram vacancy-feed review state
(`TelegramVacancyReviewRecord`) -- one row per job, CAS transitions,
retryable failures, and the Alembic migration that creates the table."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import JobRecord, TelegramVacancyReviewRecord
from app.db.repositories import upsert_job
from app.db.telegram_vacancy_review_repository import (
    CALLBACK_TOKEN_PATTERN,
    MAX_DELIVERY_ATTEMPTS,
    claim_for_sending,
    dequeue,
    ensure_review,
    get_review_by_callback_token,
    list_queued,
    mark_send_failed,
    mark_sent,
    mark_uncertain,
    reconcile_stale_sending,
    record_decision,
)
from app.models.job import Job, JobScore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vacancy_reviews.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _job_id(db, suffix: str = "1") -> int:
    job = Job(
        source="test",
        title=f"Python Developer {suffix}",
        company="Acme GmbH",
        url=f"https://example.com/jobs/{suffix}",
    )
    record, _ = upsert_job(db, job, JobScore(score=90, recommendation="APPLY"))
    return record.id


def _row_count(db) -> int:
    return db.scalar(select(func.count(TelegramVacancyReviewRecord.id)))


class TestEnsureReview:
    def test_eligible_job_is_queued_with_opaque_token(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)

        assert review.state == "QUEUED_FOR_REVIEW"
        assert review.queued_at is not None
        assert CALLBACK_TOKEN_PATTERN.fullmatch(review.callback_token)

    def test_ineligible_job_is_only_discovered(self, db):
        review = ensure_review(db, _job_id(db), eligible=False)

        assert review.state == "DISCOVERED"
        assert review.queued_at is None

    def test_recollecting_same_job_never_creates_second_row(self, db):
        job_id = _job_id(db)
        first = ensure_review(db, job_id, eligible=True)
        second = ensure_review(db, job_id, eligible=True)

        assert first.id == second.id
        assert _row_count(db) == 1

    def test_discovered_job_becomes_queued_once_eligible(self, db):
        job_id = _job_id(db)
        ensure_review(db, job_id, eligible=False)

        review = ensure_review(db, job_id, eligible=True)

        assert review.state == "QUEUED_FOR_REVIEW"

    @pytest.mark.parametrize(
        "terminal_state",
        ["TELEGRAM_SENT", "DELIVERY_UNCERTAIN", "DELIVERY_FAILED", "SAVED", "SKIPPED"],
    )
    def test_recollection_never_requeues_a_resolved_row(self, db, terminal_state):
        job_id = _job_id(db)
        review = ensure_review(db, job_id, eligible=True)
        review.state = terminal_state
        db.commit()

        again = ensure_review(db, job_id, eligible=True)

        assert again.state == terminal_state

    def test_unique_job_id_is_enforced_by_database(self, db):
        job_id = _job_id(db)
        ensure_review(db, job_id, eligible=True)
        db.add(
            TelegramVacancyReviewRecord(
                job_id=job_id, state="DISCOVERED", callback_token="A" * 16, attempt_count=0
            )
        )
        with pytest.raises(Exception, match="UNIQUE"):
            db.commit()
        db.rollback()


class TestDeliveryTransitions:
    def test_claim_is_won_only_once(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)

        assert claim_for_sending(db, review) is True
        assert review.state == "SENDING"
        assert review.attempt_count == 1
        assert claim_for_sending(db, review) is False

    def test_sent_records_message_id(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim_for_sending(db, review)

        assert mark_sent(db, review, message_id=777) is True
        assert review.state == "TELEGRAM_SENT"
        assert review.telegram_message_id == 777
        assert review.sent_at is not None

    def test_failed_send_returns_to_queue_for_retry(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim_for_sending(db, review)

        assert mark_send_failed(db, review, last_error="FAILED") is True
        assert review.state == "QUEUED_FOR_REVIEW"
        assert review.last_error == "FAILED"
        assert [r.id for r in list_queued(db, limit=10)] == [review.id]
        assert _row_count(db) == 1

    def test_failed_send_stops_after_max_attempts(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            assert claim_for_sending(db, review) is True
            mark_send_failed(db, review, last_error="FAILED")

        assert review.state == "DELIVERY_FAILED"
        assert review.attempt_count == MAX_DELIVERY_ATTEMPTS
        assert list_queued(db, limit=10) == []

    def test_uncertain_is_terminal_for_delivery(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim_for_sending(db, review)

        assert mark_uncertain(db, review, last_error="UNCERTAIN") is True
        assert review.state == "DELIVERY_UNCERTAIN"
        assert claim_for_sending(db, review) is False

    def test_dequeue_moves_queued_back_to_discovered(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)

        assert dequeue(db, review) is True
        assert review.state == "DISCOVERED"
        assert list_queued(db, limit=10) == []

    def test_stale_sending_reconciled_to_uncertain_but_live_claim_untouched(self, db):
        stale = ensure_review(db, _job_id(db, "1"), eligible=True)
        live = ensure_review(db, _job_id(db, "2"), eligible=True)
        claim_for_sending(db, stale)
        claim_for_sending(db, live)
        stale.updated_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()

        assert reconcile_stale_sending(db) == 1

        db.refresh(stale)
        db.refresh(live)
        assert stale.state == "DELIVERY_UNCERTAIN"
        assert live.state == "SENDING"

    def test_list_queued_is_oldest_first_and_bounded(self, db):
        ids = [ensure_review(db, _job_id(db, str(i)), eligible=True).id for i in range(3)]

        assert [r.id for r in list_queued(db, limit=2)] == ids[:2]


class TestDecisions:
    def _sent_review(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim_for_sending(db, review)
        mark_sent(db, review, message_id=1)
        return review

    def test_save_persists(self, db):
        review = self._sent_review(db)

        assert record_decision(db, review, "SAVED") is True

        db.expire_all()
        assert db.get(TelegramVacancyReviewRecord, review.id).state == "SAVED"
        assert review.decided_at is not None

    def test_skip_persists(self, db):
        review = self._sent_review(db)

        assert record_decision(db, review, "SKIPPED") is True

        db.expire_all()
        assert db.get(TelegramVacancyReviewRecord, review.id).state == "SKIPPED"

    def test_repeated_decision_is_idempotent(self, db):
        review = self._sent_review(db)
        record_decision(db, review, "SAVED")

        assert record_decision(db, review, "SAVED") is False
        assert review.state == "SAVED"

    def test_decision_can_be_changed(self, db):
        review = self._sent_review(db)
        record_decision(db, review, "SAVED")

        assert record_decision(db, review, "SKIPPED") is True
        assert review.state == "SKIPPED"

    def test_undelivered_card_cannot_be_decided(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)

        assert record_decision(db, review, "SAVED") is False
        assert review.state == "QUEUED_FOR_REVIEW"

    def test_decision_never_touches_application_status(self, db):
        review = self._sent_review(db)
        record_decision(db, review, "SAVED")

        assert db.get(JobRecord, review.job_id).status == "NEW"

    def test_unsupported_decision_rejected(self, db):
        review = self._sent_review(db)
        with pytest.raises(ValueError):
            record_decision(db, review, "APPLIED")


class TestCallbackTokenLookup:
    def test_known_token_resolves(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)

        assert get_review_by_callback_token(db, review.callback_token).id == review.id

    @pytest.mark.parametrize(
        "token",
        ["", "1", "short", "A" * 17, "' OR 1=1 --....", "AAAAAAAAAAAAAAA/"],
    )
    def test_malformed_or_unknown_token_rejected(self, db, token):
        ensure_review(db, _job_id(db), eligible=True)

        assert get_review_by_callback_token(db, token) is None

    def test_well_formed_but_unknown_token_rejected(self, db):
        ensure_review(db, _job_id(db), eligible=True)

        assert get_review_by_callback_token(db, "Z" * 16) is None


def test_migration_upgrade_and_downgrade(tmp_path):
    db_path = tmp_path / "migration.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    upgrade(cfg, "head")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "telegram_vacancy_reviews" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("telegram_vacancy_reviews")}
    assert columns == {c.name for c in TelegramVacancyReviewRecord.__table__.columns}
    unique_sets = [
        uc["column_names"] for uc in inspector.get_unique_constraints("telegram_vacancy_reviews")
    ]
    assert ["job_id"] in unique_sets
    assert ["callback_token"] in unique_sets

    downgrade(cfg, "b2c5d8e4f7a1")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "telegram_vacancy_reviews" not in inspector.get_table_names()
