"""Stage 9B: the Telegram Bewerbung preparation ledger -- UNIQUE(review_id)
insert races, generation/token ownership fencing of every PREPARING exit,
attempt budget, stale recovery, preview delivery fencing, DB constraints,
and the Alembic migration that creates the table."""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import TelegramBewerbungPreparationRecord as Prep
from app.db.models import TelegramVacancyReviewRecord
from app.db.repositories import upsert_job
from app.db.telegram_bewerbung_repository import (
    MAX_PREPARATION_ATTEMPTS,
    PreparationClaim,
    PreviewClaim,
    claim_preparation,
    claim_preview,
    create_preparation_claim,
    fail_preparation,
    get_preparation_by_package_token,
    get_preparation_for_review,
    publish_preparation,
    reconcile_stale_preparation,
    reconcile_stale_preview,
    resolve_preview,
)
from app.db.telegram_vacancy_review_repository import ensure_review
from app.models.job import Job, JobScore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IDENTITY_A = "a" * 64
IDENTITY_B = "b" * 64


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bewerbung_prep.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def make_session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def db(make_session):
    session = make_session()
    try:
        yield session
    finally:
        session.close()


def _review_id(db, suffix: str = "1") -> int:
    job, _ = upsert_job(
        db,
        Job(source="t", title=f"Dev {suffix}", company="Acme", url=f"https://e.com/{suffix}"),
        JobScore(score=90, recommendation="APPLY"),
    )
    return ensure_review(db, job.id, eligible=True).id


def _publish(db, claim: PreparationClaim, **overrides) -> str | None:
    values = dict(
        match_id=1,
        cv_draft_id=2,
        bewerbung_draft_id=3,
        preview_text="ENTWURF — NICHT GESENDET",
        preview_renderer_version="tg-bw-preview-v1",
    )
    values.update(overrides)
    token = publish_preparation(db, claim, **values)
    db.commit()
    return token


def _reload(db, preparation_id: int) -> Prep:
    db.expire_all()
    return db.get(Prep, preparation_id)


def _prepared(db) -> Prep:
    claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
    _publish(db, claim)
    return _reload(db, claim.preparation_id)


class TestPreparationClaims:
    def test_first_claim_inserts_preparing_generation_one(self, db):
        review_id = _review_id(db)

        claim = create_preparation_claim(db, review_id, input_identity=IDENTITY_A)

        record = get_preparation_for_review(db, review_id)
        assert (record.state, record.generation, record.attempt_count) == ("PREPARING", 1, 1)
        assert record.prep_claim_token == claim.token
        assert record.preview_state == "NONE"

    def test_concurrent_first_claim_loses_on_unique_review_id(self, db, make_session):
        review_id = _review_id(db)
        other = make_session()
        try:
            assert create_preparation_claim(db, review_id, input_identity=IDENTITY_A)
            assert create_preparation_claim(other, review_id, input_identity=IDENTITY_A) is None
            # The losing session rolled back and is still usable.
            assert get_preparation_for_review(other, review_id).generation == 1
        finally:
            other.close()
        assert db.scalar(select(func.count(Prep.id))) == 1

    def test_reclaim_advances_generation_and_rotates_token(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        fail_preparation(db, claim, error_code="X")

        second = claim_preparation(db, _reload(db, claim.preparation_id), input_identity=IDENTITY_A)

        assert second.generation == 2 and second.token != claim.token
        record = _reload(db, claim.preparation_id)
        assert (record.state, record.generation, record.attempt_count) == ("PREPARING", 2, 2)
        assert record.last_error is None

    def test_reclaim_from_observed_state_loses_to_a_concurrent_reclaim(self, db, make_session):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        fail_preparation(db, claim, error_code="X")
        other = make_session()
        try:
            stale_view = other.get(Prep, claim.preparation_id)
            assert claim_preparation(
                db, _reload(db, claim.preparation_id), input_identity=IDENTITY_A
            )
            assert claim_preparation(other, stale_view, input_identity=IDENTITY_A) is None
        finally:
            other.close()
        assert _reload(db, claim.preparation_id).generation == 2

    def test_preparing_row_cannot_be_reclaimed(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        assert (
            claim_preparation(db, _reload(db, claim.preparation_id), input_identity=IDENTITY_B)
            is None
        )


class TestOwnershipFencing:
    def _stale_and_newer(self, db):
        old = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        record = _reload(db, old.preparation_id)
        assert reconcile_stale_preparation(db, record, now=datetime.now(UTC) + timedelta(hours=1))
        newer = claim_preparation(db, _reload(db, old.preparation_id), input_identity=IDENTITY_A)
        assert newer is not None
        return old, newer

    def test_stale_claim_cannot_publish_newer_generation(self, db):
        old, newer = self._stale_and_newer(db)

        assert (
            publish_preparation(
                db,
                old,
                match_id=9,
                cv_draft_id=9,
                bewerbung_draft_id=9,
                preview_text="x",
                preview_renderer_version="v",
            )
            is None
        )
        db.commit()

        record = _reload(db, old.preparation_id)
        assert (record.state, record.generation, record.prep_claim_token) == (
            "PREPARING",
            newer.generation,
            newer.token,
        )
        assert record.bewerbung_draft_id is None

    def test_stale_claim_cannot_fail_or_reset_newer_generation(self, db):
        old, newer = self._stale_and_newer(db)
        attempts = _reload(db, old.preparation_id).attempt_count

        assert fail_preparation(db, old, error_code="STALE_WORKER") is False

        record = _reload(db, old.preparation_id)
        assert record.state == "PREPARING"
        assert record.attempt_count == attempts
        assert record.last_error is None

    def test_same_token_with_wrong_generation_is_rejected(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        forged = PreparationClaim(claim.preparation_id, claim.generation + 1, claim.token)
        assert fail_preparation(db, forged, error_code="X") is False
        assert _reload(db, claim.preparation_id).state == "PREPARING"

    def test_publication_is_not_committed_by_the_repository(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)

        assert publish_preparation(
            db,
            claim,
            match_id=1,
            cv_draft_id=2,
            bewerbung_draft_id=3,
            preview_text="x",
            preview_renderer_version="v",
        )
        db.rollback()

        record = _reload(db, claim.preparation_id)
        assert record.state == "PREPARING"
        assert record.prep_claim_token == claim.token

    def test_every_exit_from_preparing_clears_the_token(self, db):
        a = create_preparation_claim(db, _review_id(db, "1"), input_identity=IDENTITY_A)
        b = create_preparation_claim(db, _review_id(db, "2"), input_identity=IDENTITY_A)
        c = create_preparation_claim(db, _review_id(db, "3"), input_identity=IDENTITY_A)
        _publish(db, a)
        fail_preparation(db, b, error_code="X")
        reconcile_stale_preparation(
            db, _reload(db, c.preparation_id), now=datetime.now(UTC) + timedelta(hours=1)
        )
        for claim in (a, b, c):
            assert _reload(db, claim.preparation_id).prep_claim_token is None


class TestAttemptBudget:
    def test_same_identity_is_refused_after_max_attempts(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        for _ in range(MAX_PREPARATION_ATTEMPTS - 1):
            fail_preparation(db, claim, error_code="X")
            claim = claim_preparation(
                db, _reload(db, claim.preparation_id), input_identity=IDENTITY_A
            )
            assert claim is not None
        fail_preparation(db, claim, error_code="X")
        record = _reload(db, claim.preparation_id)
        assert record.attempt_count == MAX_PREPARATION_ATTEMPTS

        assert claim_preparation(db, record, input_identity=IDENTITY_A) is None
        assert _reload(db, claim.preparation_id).state == "FAILED"

    def test_changed_identity_starts_a_fresh_budget(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        for _ in range(MAX_PREPARATION_ATTEMPTS - 1):
            fail_preparation(db, claim, error_code="X")
            claim = claim_preparation(
                db, _reload(db, claim.preparation_id), input_identity=IDENTITY_A
            )
        fail_preparation(db, claim, error_code="X")

        fresh = claim_preparation(db, _reload(db, claim.preparation_id), input_identity=IDENTITY_B)

        record = _reload(db, claim.preparation_id)
        assert fresh is not None
        assert (record.attempt_count, record.input_identity) == (1, IDENTITY_B)

    def test_stale_recovery_counts_the_crashed_attempt(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        record = _reload(db, claim.preparation_id)

        assert reconcile_stale_preparation(db, record, now=datetime.now(UTC) + timedelta(hours=1))

        record = _reload(db, claim.preparation_id)
        assert (record.state, record.last_error, record.attempt_count) == (
            "FAILED",
            "STALE_PREPARATION",
            1,
        )

    def test_fresh_claim_is_not_reconciled(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        assert reconcile_stale_preparation(db, _reload(db, claim.preparation_id)) is False
        assert _reload(db, claim.preparation_id).state == "PREPARING"

    def test_reclaim_from_prepared_is_refused_while_preview_is_sending(self, db):
        record = _prepared(db)
        assert claim_preview(db, record, allowed_states=("NONE",))

        assert claim_preparation(db, _reload(db, record.id), input_identity=IDENTITY_B) is None
        assert _reload(db, record.id).state == "PREPARED"


class TestPreviewDelivery:
    def test_none_sending_sent(self, db):
        record = _prepared(db)

        claim = claim_preview(db, record, allowed_states=("NONE", "FAILED"))
        assert _reload(db, record.id).preview_state == "SENDING"
        assert resolve_preview(db, claim, "SENT", message_id=2**40) is True

        record = _reload(db, record.id)
        assert (record.preview_state, record.preview_message_id) == ("SENT", 2**40)
        assert record.preview_claim_token is None and record.preview_sent_at is not None

    def test_concurrent_preview_claims_admit_one_winner(self, db, make_session):
        record = _prepared(db)
        other = make_session()
        try:
            stale_view = other.get(Prep, record.id)
            assert claim_preview(db, record, allowed_states=("NONE",)) is not None
            assert claim_preview(other, stale_view, allowed_states=("NONE",)) is None
        finally:
            other.close()

    def test_stale_preview_token_cannot_resolve(self, db):
        record = _prepared(db)
        claim = claim_preview(db, record, allowed_states=("NONE",))
        forged = PreviewClaim(claim.preparation_id, claim.generation, claim.package_token, "x" * 22)

        assert resolve_preview(db, forged, "SENT", message_id=1) is False
        assert _reload(db, record.id).preview_state == "SENDING"

    def test_old_generation_preview_token_cannot_resolve_newer_generation(self, db):
        record = _prepared(db)
        old = claim_preview(db, record, allowed_states=("NONE",))
        resolve_preview(db, old, "FAILED")
        newer = claim_preparation(db, _reload(db, record.id), input_identity=IDENTITY_B)
        _publish(db, newer)
        current = claim_preview(db, _reload(db, record.id), allowed_states=("NONE",))

        assert resolve_preview(db, old, "SENT", message_id=1) is False
        record = _reload(db, record.id)
        assert record.preview_state == "SENDING"
        assert record.preview_claim_token == current.token

    def test_stale_sending_becomes_uncertain_not_failed(self, db):
        record = _prepared(db)
        claim = claim_preview(db, record, allowed_states=("NONE",))

        assert reconcile_stale_preview(
            db, _reload(db, record.id), now=datetime.now(UTC) + timedelta(hours=1)
        )

        assert _reload(db, record.id).preview_state == "UNCERTAIN"
        # The late result of the reconciled send can no longer be applied.
        assert resolve_preview(db, claim, "SENT", message_id=1) is False

    def test_fresh_sending_is_not_reconciled(self, db):
        record = _prepared(db)
        claim_preview(db, record, allowed_states=("NONE",))
        assert reconcile_stale_preview(db, _reload(db, record.id)) is False

    def test_uncertain_is_not_claimable_without_explicit_permission(self, db):
        record = _prepared(db)
        claim = claim_preview(db, record, allowed_states=("NONE",))
        resolve_preview(db, claim, "UNCERTAIN")

        assert claim_preview(db, _reload(db, record.id), allowed_states=("NONE", "FAILED")) is None
        assert claim_preview(db, _reload(db, record.id), allowed_states=("UNCERTAIN",))

    def test_sending_can_never_be_an_allowed_source(self, db):
        with pytest.raises(ValueError):
            claim_preview(db, _prepared(db), allowed_states=("SENDING",))

    def test_preview_requires_prepared_package(self, db):
        claim = create_preparation_claim(db, _review_id(db), input_identity=IDENTITY_A)
        assert (
            claim_preview(db, _reload(db, claim.preparation_id), allowed_states=("NONE",)) is None
        )

    def test_republication_rotates_package_token_and_resets_preview(self, db):
        record = _prepared(db)
        old_token = record.package_token
        claim = claim_preview(db, record, allowed_states=("NONE",))
        resolve_preview(db, claim, "SENT", message_id=5)
        newer = claim_preparation(db, _reload(db, record.id), input_identity=IDENTITY_B)
        _publish(db, newer)

        record = _reload(db, record.id)
        assert record.package_token != old_token
        assert (record.preview_state, record.preview_message_id) == ("NONE", None)
        assert get_preparation_by_package_token(db, old_token) is None
        assert get_preparation_by_package_token(db, record.package_token).id == record.id

    @pytest.mark.parametrize("token", ["", "1", "12345", "A" * 17, "' OR 1=1 --....", "../" * 6])
    def test_malformed_package_token_is_rejected_without_lookup(self, db, token):
        _prepared(db)
        assert get_preparation_by_package_token(db, token) is None


class TestConstraints:
    def _insert(self, db, **values):
        base = dict(
            review_id=_review_id(db),
            state="FAILED",
            generation=1,
            input_identity=IDENTITY_A,
            attempt_count=1,
            preview_state="NONE",
        )
        base.update(values)
        db.add(Prep(**base))
        db.commit()

    @pytest.mark.parametrize(
        "values",
        [
            {"state": "QUEUED"},
            {"state": "CANCELLED"},
            {"generation": 0},
            {"attempt_count": -1},
            {"state": "PREPARING"},  # missing claim token/start
            {"state": "FAILED", "prep_claim_token": "t"},  # token outside PREPARING
            {"state": "PREPARED"},  # missing artifacts
            {"preview_state": "BOUNCED"},
            {"preview_state": "SENDING"},  # SENDING without PREPARED/token
            {"preview_state": "SENT", "preview_claim_token": "t"},
        ],
    )
    def test_invalid_rows_are_rejected_by_the_database(self, db, values):
        with pytest.raises(IntegrityError):
            self._insert(db, **values)
        db.rollback()

    def test_duplicate_review_id_is_rejected(self, db):
        review_id = _review_id(db)
        self._insert(db, review_id=review_id)
        with pytest.raises(IntegrityError):
            db.add(
                Prep(
                    review_id=review_id,
                    state="FAILED",
                    generation=1,
                    input_identity=IDENTITY_A,
                    attempt_count=0,
                    preview_state="NONE",
                )
            )
            db.commit()
        db.rollback()

    def test_deleting_the_review_cascades_to_the_ledger_only(self, db):
        record = _prepared(db)
        review_id = record.review_id

        db.execute(
            TelegramVacancyReviewRecord.__table__.delete().where(
                TelegramVacancyReviewRecord.id == review_id
            )
        )
        db.commit()

        assert db.scalar(select(func.count(Prep.id))) == 0

    def test_timestamps_round_trip(self, db):
        record = _prepared(db)
        assert record.created_at is not None and record.updated_at is not None
        db.execute(update(Prep).where(Prep.id == record.id).values(preview_message_id=2**40))
        db.commit()
        assert _reload(db, record.id).preview_message_id == 2**40


def _alembic_config(db_path: Path, buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=buffer)
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_migration_upgrade_downgrade_upgrade_with_orm_parity(tmp_path):
    db_path = tmp_path / "migration.db"
    cfg = _alembic_config(db_path)
    table = Prep.__tablename__

    upgrade(cfg, "head")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    assert set(columns) == {c.name for c in Prep.__table__.columns}
    for column in Prep.__table__.columns:
        if not column.primary_key:
            assert columns[column.name]["nullable"] == column.nullable, column.name
    uniques = [u["column_names"] for u in inspector.get_unique_constraints(table)]
    assert ["review_id"] in uniques and ["package_token"] in uniques
    checks = {c["name"] for c in inspector.get_check_constraints(table)}
    orm_checks = {
        c.name for c in Prep.__table__.constraints if c.__class__.__name__ == "CheckConstraint"
    }
    assert orm_checks <= checks
    fks = inspector.get_foreign_keys(table)
    assert fks[0]["referred_table"] == "telegram_vacancy_reviews"
    assert fks[0]["options"].get("ondelete") == "CASCADE"
    indexes = {i["name"] for i in inspector.get_indexes(table)}
    assert {
        "ix_telegram_bewerbung_preparations_state_claim",
        "ix_telegram_bewerbung_preparations_preview_claim",
    } <= indexes

    downgrade(cfg, "c4e8a1f63b9d")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert table not in inspector.get_table_names()
    assert "telegram_vacancy_reviews" in inspector.get_table_names()

    upgrade(cfg, "head")
    assert table in inspect(create_engine(f"sqlite:///{db_path}")).get_table_names()


def test_migration_postgresql_offline_ddl(tmp_path):
    buffer = io.StringIO()
    cfg = _alembic_config(tmp_path / "unused.db", buffer)
    cfg.set_main_option("sqlalchemy.url", "postgresql://user:pass@localhost/offline")

    upgrade(cfg, "c4e8a1f63b9d:d7b3e9a2c5f1", sql=True)

    sql = buffer.getvalue()
    assert "CREATE TABLE telegram_bewerbung_preparations" in sql
    assert "preview_message_id BIGINT" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "ON DELETE CASCADE" in sql
    assert "ck_telegram_bewerbung_preparations_prep_token_state" in sql
    assert "uq_telegram_bewerbung_preparations_review_id" in sql


def test_ownership_updates_compile_for_postgresql_with_fencing_predicates():
    from app.db.telegram_bewerbung_repository import _owns_preparation

    claim = PreparationClaim(1, 3, "tok")
    stmt = update(Prep).where(*_owns_preparation(claim)).values(state="FAILED")
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    for predicate in ("id =", "state =", "generation =", "prep_claim_token ="):
        assert f"telegram_bewerbung_preparations.{predicate}" in sql
