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
    release_claim,
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

        claim = claim_for_sending(db, review)
        assert claim is not None
        assert review.state == "SENDING"
        assert review.attempt_count == 1
        assert review.claim_token == claim
        assert claim_for_sending(db, review) is None

    def test_sent_records_message_id(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim = claim_for_sending(db, review)

        assert mark_sent(db, review, claim_token=claim, message_id=777) is True
        assert review.state == "TELEGRAM_SENT"
        assert review.telegram_message_id == 777
        assert review.sent_at is not None
        assert review.claim_token is None

    def test_failed_send_returns_to_queue_for_retry(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim = claim_for_sending(db, review)

        assert mark_send_failed(db, review, claim_token=claim, last_error="FAILED") is True
        assert review.state == "QUEUED_FOR_REVIEW"
        assert review.last_error == "FAILED"
        assert review.claim_token is None
        assert [r.id for r in list_queued(db, limit=10)] == [review.id]
        assert _row_count(db) == 1

    def test_failed_send_stops_after_max_attempts(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            claim = claim_for_sending(db, review)
            assert claim is not None
            mark_send_failed(db, review, claim_token=claim, last_error="FAILED")

        assert review.state == "DELIVERY_FAILED"
        assert review.attempt_count == MAX_DELIVERY_ATTEMPTS
        assert review.claim_token is None
        assert list_queued(db, limit=10) == []

    def test_uncertain_is_terminal_for_delivery(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim = claim_for_sending(db, review)

        assert mark_uncertain(db, review, claim_token=claim, last_error="UNCERTAIN") is True
        assert review.state == "DELIVERY_UNCERTAIN"
        assert review.claim_token is None
        assert claim_for_sending(db, review) is None

    def test_dequeue_moves_queued_back_to_discovered(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)

        assert dequeue(db, review) is True
        assert review.state == "DISCOVERED"
        assert list_queued(db, limit=10) == []

    def test_stale_sending_reconciled_to_uncertain_but_live_claim_untouched(self, db):
        stale = ensure_review(db, _job_id(db, "1"), eligible=True)
        live = ensure_review(db, _job_id(db, "2"), eligible=True)
        stale_claim = claim_for_sending(db, stale)
        live_claim = claim_for_sending(db, live)
        stale.updated_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()

        assert reconcile_stale_sending(db) == 1

        db.refresh(stale)
        db.refresh(live)
        assert stale.state == "DELIVERY_UNCERTAIN"
        assert stale.claim_token is None
        assert live.state == "SENDING"
        assert live.claim_token == live_claim
        # The reconciled claim's late outcome can no longer be applied.
        assert mark_sent(db, stale, claim_token=stale_claim, message_id=1) is False
        assert stale.state == "DELIVERY_UNCERTAIN"

    def test_list_queued_is_oldest_first_and_bounded(self, db):
        ids = [ensure_review(db, _job_id(db, str(i)), eligible=True).id for i in range(3)]

        assert [r.id for r in list_queued(db, limit=2)] == ids[:2]


class TestClaimOwnership:
    """Codex S9A-CODEX-002 / S9A-CODEX-004: a known-unsent claim can be
    released back to the queue, but only by the exact claim that acquired
    `SENDING` -- enforced by the UPDATE's WHERE clause on `claim_token`."""

    def test_release_returns_known_unsent_row_to_queue_and_invalidates_claim(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        queued_at = review.queued_at
        claim = claim_for_sending(db, review)

        assert release_claim(db, review, claim_token=claim, reason="lease_lost_before_send")
        assert review.state == "QUEUED_FOR_REVIEW"
        assert review.attempt_count == 0
        assert review.claim_token is None
        assert review.queued_at == queued_at
        assert review.last_error == "lease_lost_before_send"
        assert [r.id for r in list_queued(db, limit=10)] == [review.id]
        assert _row_count(db) == 1

    def test_repeated_release_with_old_claim_is_rejected(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim = claim_for_sending(db, review)
        release_claim(db, review, claim_token=claim, reason="lease_lost_before_send")
        before = (review.state, review.attempt_count, review.claim_token, review.updated_at)

        assert release_claim(db, review, claim_token=claim, reason="again") is False
        assert (review.state, review.attempt_count, review.claim_token, review.updated_at) == (
            before
        )
        assert review.last_error == "lease_lost_before_send"

    def test_stale_release_cannot_release_a_newer_workers_claim(self, db):
        """The reproduced S9A-CODEX-004 scenario, with worker A and worker B
        in separate sessions so the stale caller's ORM object is never
        refreshed by B's claim: only the database predicate protects B."""
        job_id = _job_id(db)
        ensure_review(db, job_id, eligible=True)
        other = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)()
        try:
            review_a = db.scalar(
                select(TelegramVacancyReviewRecord).where(
                    TelegramVacancyReviewRecord.job_id == job_id
                )
            )
            claim_a = claim_for_sending(db, review_a)
            assert release_claim(db, review_a, claim_token=claim_a, reason="lease_lost")

            review_b = other.get(TelegramVacancyReviewRecord, review_a.id)
            claim_b = claim_for_sending(other, review_b)
            assert claim_b is not None and claim_b != claim_a

            # Stale A presents its OLD claim identity against B's live claim.
            assert release_claim(db, review_a, claim_token=claim_a, reason="stale") is False
            assert mark_send_failed(db, review_a, claim_token=claim_a, last_error="x") is False
            assert mark_uncertain(db, review_a, claim_token=claim_a, last_error="x") is False
            assert mark_sent(db, review_a, claim_token=claim_a, message_id=9) is False

            other.refresh(review_b)
            assert review_b.state == "SENDING"
            assert review_b.attempt_count == 1
            assert review_b.claim_token == claim_b
            assert review_b.telegram_message_id is None
            assert list_queued(other, limit=10) == []  # not re-claimable by a third worker

            # B's own claim still resolves normally.
            assert mark_sent(other, review_b, claim_token=claim_b, message_id=5) is True
            assert review_b.state == "TELEGRAM_SENT"
        finally:
            other.close()

    @pytest.mark.parametrize("terminal", ["TELEGRAM_SENT", "DELIVERY_UNCERTAIN", "DELIVERY_FAILED"])
    def test_stale_release_never_touches_a_terminal_row(self, db, terminal):
        review = ensure_review(db, _job_id(db), eligible=True)
        attempts = MAX_DELIVERY_ATTEMPTS if terminal == "DELIVERY_FAILED" else 1
        for _ in range(attempts):
            claim = claim_for_sending(db, review)
            if terminal == "TELEGRAM_SENT":
                mark_sent(db, review, claim_token=claim, message_id=1)
            elif terminal == "DELIVERY_UNCERTAIN":
                mark_uncertain(db, review, claim_token=claim, last_error="UNCERTAIN")
            else:
                mark_send_failed(db, review, claim_token=claim, last_error="FAILED")
        assert review.state == terminal
        before = (review.state, review.attempt_count, review.claim_token, review.updated_at)

        assert release_claim(db, review, claim_token=claim, reason="stale") is False
        assert (review.state, review.attempt_count, review.claim_token, review.updated_at) == (
            before
        )

    def test_release_of_a_queued_row_is_rejected(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)

        assert release_claim(db, review, claim_token="never-issued", reason="x") is False
        assert (review.state, review.attempt_count) == ("QUEUED_FOR_REVIEW", 0)

    def test_claim_tokens_never_repeat_across_release_reclaim_cycles(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        seen = set()
        for _ in range(50):
            claim = claim_for_sending(db, review)
            assert claim not in seen
            seen.add(claim)
            release_claim(db, review, claim_token=claim, reason="cycle")
        assert review.attempt_count == 0  # known-unsent releases consume no attempts

    def test_release_preserves_max_attempt_accounting(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        for _ in range(MAX_DELIVERY_ATTEMPTS - 1):
            claim = claim_for_sending(db, review)
            mark_send_failed(db, review, claim_token=claim, last_error="FAILED")
            claim = claim_for_sending(db, review)
            release_claim(db, review, claim_token=claim, reason="lease_lost_before_send")
        assert review.state == "QUEUED_FOR_REVIEW"
        assert review.attempt_count == MAX_DELIVERY_ATTEMPTS - 1

        claim = claim_for_sending(db, review)
        mark_send_failed(db, review, claim_token=claim, last_error="FAILED")

        assert review.state == "DELIVERY_FAILED"
        assert review.attempt_count == MAX_DELIVERY_ATTEMPTS


class TestDecisions:
    def _sent_review(self, db):
        review = ensure_review(db, _job_id(db), eligible=True)
        claim = claim_for_sending(db, review)
        mark_sent(db, review, claim_token=claim, message_id=1)
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

    columns = {c["name"]: c for c in inspector.get_columns("telegram_vacancy_reviews")}
    assert columns["claim_token"]["nullable"] is True

    # S9A-CODEX-004 migration: down to Stage 9A's table revision drops only
    # claim_token; up again restores it.
    downgrade(cfg, "9a1f4c7e2b3d")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    columns = {col["name"] for col in inspector.get_columns("telegram_vacancy_reviews")}
    assert "claim_token" not in columns
    assert columns == {c.name for c in TelegramVacancyReviewRecord.__table__.columns} - {
        "claim_token"
    }
    upgrade(cfg, "head")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    columns = {col["name"] for col in inspector.get_columns("telegram_vacancy_reviews")}
    assert "claim_token" in columns

    downgrade(cfg, "b2c5d8e4f7a1")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "telegram_vacancy_reviews" not in inspector.get_table_names()
