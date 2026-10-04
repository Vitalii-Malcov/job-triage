"""Stage 9C: the immutable Telegram approval link table -- constraints,
retained (non-FK) historical lineage, capability shape, conflict
classification, fresh lock helpers, and the Alembic migration."""

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import JobRecord, TelegramBewerbungApprovalRecord
from app.db.models import TelegramBewerbungPreparationRecord as Prep
from app.db.repositories import upsert_job
from app.db.telegram_bewerbung_approval_repository import (
    APPROVAL_CAPABILITY_PATTERN,
    CAPABILITY_CONFLICT,
    GENERATION_CONFLICT,
    REVIEW_CONFLICT,
    classify_link_conflict,
    get_link_by_capability,
    get_link_for_generation,
    insert_link,
    lock_job_fresh,
    new_approval_capability,
)
from app.models.job import Job, JobScore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "c" * 64
TOKEN = "P" * 16


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'approval_link.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _link(db, **overrides):
    values = dict(
        preparation_id=1,
        generation=1,
        package_token=TOKEN,
        input_identity=IDENTITY,
        review_id=10,
        approval_capability=new_approval_capability(),
    )
    values.update(overrides)
    link = insert_link(db, **values)
    db.commit()
    return link


def _conflict(db, **overrides) -> str | None:
    try:
        _link(db, **overrides)
    except IntegrityError as exc:
        db.rollback()
        return classify_link_conflict(exc)
    raise AssertionError("expected an IntegrityError")


class TestLinkTable:
    def test_insert_link_flushes_without_committing(self, db):
        insert_link(
            db,
            preparation_id=1,
            generation=1,
            package_token=TOKEN,
            input_identity=IDENTITY,
            review_id=10,
            approval_capability=new_approval_capability(),
        )
        db.rollback()
        assert db.scalar(select(func.count(TelegramBewerbungApprovalRecord.id))) == 0

    def test_bound_version_is_one_and_created_at_is_timezone_aware(self, db):
        link = _link(db)
        db.expire_all()
        stored = db.get(TelegramBewerbungApprovalRecord, link.id)
        assert stored.bound_review_version == 1
        assert stored.created_at is not None
        created = stored.created_at
        if created.tzinfo is None:  # SQLite drops tzinfo on read-back
            created = created.replace(tzinfo=UTC)
        assert abs((datetime.now(UTC) - created).total_seconds()) < 60

    def test_unique_conflicts_are_classified_exactly(self, db):
        first = _link(db)
        assert _conflict(db, review_id=11) == GENERATION_CONFLICT
        assert (
            _conflict(db, generation=2, review_id=12, approval_capability=first.approval_capability)
            == CAPABILITY_CONFLICT
        )
        assert _conflict(db, generation=3) == REVIEW_CONFLICT

    @pytest.mark.parametrize("values", [{"generation": 0}, {"bound_review_version": 2}])
    def test_check_constraints(self, db, values):
        record = TelegramBewerbungApprovalRecord(
            preparation_id=1,
            generation=1,
            package_token=TOKEN,
            input_identity=IDENTITY,
            review_id=10,
            bound_review_version=1,
            approval_capability=new_approval_capability(),
        )
        for key, value in values.items():
            setattr(record, key, value)
        db.add(record)
        with pytest.raises(IntegrityError) as exc:
            db.commit()
        db.rollback()
        assert classify_link_conflict(exc.value) is None  # never mistaken for a winner

    def test_link_survives_deletion_of_the_preparation_row(self, db):
        """preparation_id is a historical identifier, not a cascading FK."""
        link = _link(db, preparation_id=999)
        assert db.get(Prep, 999) is None
        db.expire_all()
        assert db.get(TelegramBewerbungApprovalRecord, link.id).preparation_id == 999

    def test_lookups_are_exact(self, db):
        link = _link(db)
        assert get_link_by_capability(db, link.approval_capability).id == link.id
        assert get_link_for_generation(db, 1, 1).id == link.id
        assert get_link_for_generation(db, 1, 2) is None

    @pytest.mark.parametrize(
        "bad",
        ["", "1", "10", "A" * 15, "A" * 17, "AAAAAAAAAAAAAAA=", "AAAAAAAAAAAAAAA.", " " * 16],
    )
    def test_malformed_capability_is_rejected_without_lookup(self, db, bad):
        _link(db)
        assert get_link_by_capability(db, bad) is None

    def test_capabilities_are_96_bit_base64url(self):
        tokens = {new_approval_capability() for _ in range(200)}
        assert len(tokens) == 200
        assert all(APPROVAL_CAPABILITY_PATTERN.fullmatch(token) for token in tokens)


def test_lock_queries_compile_to_for_update_on_postgresql():
    from app.db.models import ApplicationPackageReviewRecord
    from app.db.telegram_bewerbung_approval_repository import _lock

    captured = []

    class _Recorder:
        def scalar(self, stmt):
            captured.append(str(stmt.compile(dialect=postgresql.dialect())))

    for model in (JobRecord, Prep, ApplicationPackageReviewRecord):
        _lock(_Recorder(), model, 1)
    assert len(captured) == 3
    assert all(sql.rstrip().endswith("FOR UPDATE") for sql in captured)


def test_lock_job_fresh_overwrites_stale_identity_map_values(db, tmp_path):
    job, _ = upsert_job(
        db,
        Job(
            source="test",
            title="Old",
            company="Example",
            url="https://example.com/1",
            description="d",
        ),
        JobScore(score=50, recommendation="MAYBE"),
    )
    db.commit()
    stale = db.get(JobRecord, job.id)
    assert stale.title == "Old"
    other = sessionmaker(bind=db.get_bind())()
    try:
        other.get(JobRecord, job.id).title = "New"
        other.commit()
    finally:
        other.close()
    assert stale.title == "Old"  # identity map still holds the stale value

    locked = lock_job_fresh(db, job.id)

    assert locked is stale and locked.title == "New"


def _alembic_config(db_path: Path, buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=buffer)
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_migration_upgrade_downgrade_upgrade_with_orm_parity(tmp_path):
    db_path = tmp_path / "migration.db"
    cfg = _alembic_config(db_path)
    table = TelegramBewerbungApprovalRecord.__tablename__
    model_table = TelegramBewerbungApprovalRecord.__table__

    upgrade(cfg, "head")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    assert set(columns) == {
        "id",
        "preparation_id",
        "generation",
        "package_token",
        "input_identity",
        "review_id",
        "bound_review_version",
        "approval_capability",
        "created_at",
    }
    assert set(columns) == {c.name for c in model_table.columns}
    for column in model_table.columns:
        if not column.primary_key:
            assert columns[column.name]["nullable"] == column.nullable, column.name
    uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints(table)}
    assert uniques == {
        "uq_telegram_bewerbung_approvals_preparation_generation": [
            "preparation_id",
            "generation",
        ],
        "uq_telegram_bewerbung_approvals_review_id": ["review_id"],
        "uq_telegram_bewerbung_approvals_approval_capability": ["approval_capability"],
    }
    checks = {c["name"] for c in inspector.get_check_constraints(table)}
    assert checks == {
        "ck_telegram_bewerbung_approvals_generation_positive",
        "ck_telegram_bewerbung_approvals_bound_review_version",
    }
    assert inspector.get_foreign_keys(table) == []  # retained historical lineage

    downgrade(cfg, "d7b3e9a2c5f1")
    names = inspect(create_engine(f"sqlite:///{db_path}")).get_table_names()
    assert table not in names
    assert "telegram_bewerbung_preparations" in names
    assert "application_package_reviews" in names

    upgrade(cfg, "head")
    assert table in inspect(create_engine(f"sqlite:///{db_path}")).get_table_names()


def test_migration_postgresql_offline_ddl(tmp_path):
    buffer = io.StringIO()
    cfg = _alembic_config(tmp_path / "unused.db", buffer)
    cfg.set_main_option("sqlalchemy.url", "postgresql://user:pass@localhost/offline")

    upgrade(cfg, "d7b3e9a2c5f1:e5a17c3b9d42", sql=True)

    sql = buffer.getvalue()
    create = sql[sql.index("CREATE TABLE telegram_bewerbung_approvals") :]
    create = create[: create.index(";")]
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in create
    assert "REFERENCES" not in create and "CASCADE" not in create
    for name in (
        "uq_telegram_bewerbung_approvals_preparation_generation",
        "uq_telegram_bewerbung_approvals_review_id",
        "uq_telegram_bewerbung_approvals_approval_capability",
        "ck_telegram_bewerbung_approvals_generation_positive",
        "ck_telegram_bewerbung_approvals_bound_review_version",
    ):
        assert name in create
    assert "ALTER TABLE" not in sql.split("CREATE TABLE telegram_bewerbung_approvals")[1]
