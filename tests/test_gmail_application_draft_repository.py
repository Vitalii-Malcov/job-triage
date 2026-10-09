"""Stage 9D: the Gmail draft handoff ledger -- CHECK constraints, exact
CAS transitions, conflict classification, and the Alembic migration
(ORM parity, every constraint, S9D-CONF-001's 2048 wire bound).

SQLite here is sequential evidence only; real overlapping transactions are
in tests/integration/test_gmail_application_draft_postgres.py."""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.db.gmail_application_draft_repository as repo
from app.db.base import Base
from app.db.models import GMAIL_DRAFT_CHECKS, GmailApplicationDraftRecord

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
MARKER = "<s9d." + "a" * 32 + "@ai-job-search.invalid>"
MARKER_2 = "<s9d." + "b" * 32 + "@ai-job-search.invalid>"
HANDOFF = repo.FrozenHandoff(
    review_id=11,
    approved_revision_id=12,
    preparation_id=13,
    generation=1,
    match_id=14,
    cv_draft_id=15,
    bewerbung_draft_id=16,
    package_token="P" * 16,
    input_identity="i" * 64,
)


def _bundle(marker=MARKER, mailbox="[Gmail]/Drafts", budget=60) -> repo.AttemptBundle:
    return repo.AttemptBundle(
        marker_message_id=marker,
        content_sha256="h" * 64,
        renderer_version="9d-v1",
        drafts_mailbox=mailbox,
        drafts_mailbox_wire=f'"{mailbox}"',
        attempt_budget_seconds=budget,
    )


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _claim(db, link_id=1) -> repo.Claim:
    claim = repo.insert_claim(
        db, link_id=link_id, account_key="me@example.com", handoff=HANDOFF, now=NOW
    )
    db.commit()
    return claim


def _armed(db, link_id=1, marker=MARKER) -> repo.Claim:
    claim = _claim(db, link_id)
    assert repo.begin_append(
        db, claim, account_key="me@example.com", bundle=_bundle(marker), now=NOW
    )
    db.commit()
    return claim


def _snap(db, link_id=1) -> repo.LedgerSnapshot:
    db.rollback()
    return repo.get_snapshot_by_link(db, link_id)


class TestClaims:
    def test_insert_flushes_without_committing(self, db):
        claim = repo.insert_claim(
            db, link_id=1, account_key="me@example.com", handoff=HANDOFF, now=NOW
        )
        db.rollback()
        assert repo.get_snapshot_by_id(db, claim.ledger_id) is None

    def test_first_claim_shape(self, db):
        claim = _claim(db)
        snap = _snap(db)
        assert (snap.state, snap.attempt_count, snap.claim_token) == (
            "CREATING",
            1,
            claim.claim_token,
        )
        assert repo.CLAIM_TOKEN_PATTERN.fullmatch(claim.claim_token)
        assert snap.handoff == HANDOFF and not snap.armed

    def test_duplicate_link_is_classified_as_the_link_conflict(self, db):
        _claim(db)
        with pytest.raises(IntegrityError) as exc:
            _claim(db)
        db.rollback()
        assert repo.classify_ledger_conflict(exc.value) == repo.LINK_CONFLICT

    def test_marker_collision_is_classified_as_the_marker_conflict(self, db):
        _armed(db, link_id=1)
        claim = _claim(db, link_id=2)
        with pytest.raises(IntegrityError) as exc:
            repo.begin_append(
                db, claim, account_key="me@example.com", bundle=_bundle(MARKER), now=NOW
            )
        db.rollback()
        assert repo.classify_ledger_conflict(exc.value) == repo.MARKER_CONFLICT
        assert not _snap(db, 2).armed  # definite rollback before any external call

    def test_unknown_integrity_error_is_not_classified(self, db):
        _claim(db)
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                update(GmailApplicationDraftRecord).values(attempt_count=0)
            )  # CHECK, not UNIQUE
        db.rollback()
        assert repo.classify_ledger_conflict(exc.value) is None

    def test_null_markers_never_collide(self, db):
        _claim(db, 1)
        _claim(db, 2)
        assert _snap(db, 1).marker_message_id is None is _snap(db, 2).marker_message_id

    def test_takeover_requires_expired_lease_unarmed_and_exact_attempt(self, db):
        old = _claim(db)
        snap = _snap(db)
        assert repo.takeover_pre_fence(db, snap, lease_cutoff=NOW, now=NOW) is None  # not expired
        db.rollback()
        new = repo.takeover_pre_fence(
            db, snap, lease_cutoff=NOW + timedelta(seconds=1), now=NOW + timedelta(seconds=200)
        )
        db.commit()
        assert new.attempt_count == 2 and new.claim_token != old.claim_token
        # The superseded worker can neither arm nor finalize.
        assert not repo.begin_append(
            db, old, account_key="me@example.com", bundle=_bundle(), now=NOW
        )
        assert not repo.finalize_failed(db, old, marker=None, error_code="X", now=NOW)
        # The observed snapshot is now stale: a second takeover from it loses.
        assert (
            repo.takeover_pre_fence(db, snap, lease_cutoff=NOW + timedelta(days=1), now=NOW) is None
        )

    def test_armed_claim_is_never_taken_over(self, db):
        _armed(db)
        snap = _snap(db)
        later = NOW + timedelta(days=1)
        assert repo.takeover_pre_fence(db, snap, lease_cutoff=later, now=later) is None


class TestBeginAppendAndFinalize:
    def test_begin_append_freezes_the_bundle_once(self, db):
        claim = _armed(db)
        snap = _snap(db)
        assert snap.armed and snap.marker_message_id == MARKER
        assert snap.drafts_mailbox_wire == '"[Gmail]/Drafts"' and snap.attempt_budget_seconds == 60
        assert snap.attempt_deadline_at == NOW + timedelta(seconds=60)
        # The bundle is immutable within an armed attempt.
        assert not repo.begin_append(
            db, claim, account_key="x@example.com", bundle=_bundle(MARKER_2), now=NOW
        )

    def test_wrong_token_or_attempt_cannot_arm_or_finalize(self, db):
        claim = _claim(db)
        forged = repo.Claim(claim.ledger_id, 1, "Z" * 22, claim.attempt_count)
        stale = repo.Claim(claim.ledger_id, 1, claim.claim_token, claim.attempt_count + 1)
        for bad in (forged, stale):
            assert not repo.begin_append(
                db, bad, account_key="me@example.com", bundle=_bundle(), now=NOW
            )
        repo.begin_append(db, claim, account_key="me@example.com", bundle=_bundle(), now=NOW)
        db.commit()
        for bad in (forged, stale):
            assert not repo.finalize_created(
                db, bad, marker=MARKER, uid_validity=None, draft_uid=None, now=NOW
            )
            assert not repo.finalize_uncertain(db, bad, marker=MARKER, error_code="X", now=NOW)
        assert not repo.finalize_created(
            db, claim, marker=MARKER_2, uid_validity=None, draft_uid=None, now=NOW
        )

    def test_created_is_terminal(self, db):
        claim = _armed(db)
        assert repo.finalize_created(
            db, claim, marker=MARKER, uid_validity=7, draft_uid=42, now=NOW
        )
        db.commit()
        snap = _snap(db)
        assert (snap.state, snap.uid_validity, snap.draft_uid, snap.reconciled) == (
            "CREATED",
            7,
            42,
            False,
        )
        assert snap.claim_token is None and snap.claim_started_at is None
        later = NOW + timedelta(days=1)
        assert not repo.finalize_failed(db, claim, marker=MARKER, error_code="X", now=NOW)
        assert not repo.finalize_uncertain(db, claim, marker=MARKER, error_code="X", now=NOW)
        assert repo.retry_failed(db, snap, now=later) is None
        assert not repo.classify_stale_armed(db, snap, stale_margin_seconds=0, now=later)
        assert not repo.reconcile_created(
            db,
            ledger_id=snap.id,
            attempt_count=1,
            marker=MARKER,
            uid_validity=1,
            draft_uid=1,
            now=later,
        )
        assert _snap(db).state == "CREATED"

    def test_uncertain_is_sticky_and_never_failed_or_retried(self, db):
        claim = _armed(db)
        assert repo.finalize_uncertain(db, claim, marker=MARKER, error_code="TIMEOUT", now=NOW)
        db.commit()
        snap = _snap(db)
        later = NOW + timedelta(days=30)
        assert not repo.finalize_failed(db, claim, marker=MARKER, error_code="X", now=later)
        assert repo.retry_failed(db, snap, now=later) is None
        assert repo.takeover_pre_fence(db, snap, lease_cutoff=later, now=later) is None
        assert _snap(db).state == "UNCERTAIN"

    def test_failed_retry_clears_the_entire_previous_attempt(self, db):
        claim = _armed(db)
        assert repo.finalize_failed(db, claim, marker=MARKER, error_code="REJECTED", now=NOW)
        db.commit()
        failed = _snap(db)
        assert failed.armed and failed.last_error == "REJECTED"  # evidence kept until retry
        new = repo.retry_failed(db, failed, now=NOW + timedelta(seconds=5))
        db.commit()
        snap = _snap(db)
        assert (snap.state, snap.attempt_count, snap.claim_token) == (
            "CREATING",
            2,
            new.claim_token,
        )
        for name in (
            "append_started_at",
            "marker_message_id",
            "content_sha256",
            "renderer_version",
            "drafts_mailbox",
            "drafts_mailbox_wire",
            "attempt_budget_seconds",
            "uid_validity",
            "draft_uid",
            "created_in_gmail_at",
            "last_error",
        ):
            assert getattr(snap, name) is None, name
        assert snap.reconciled is False
        # A second retry from the same observed FAILED snapshot loses.
        assert repo.retry_failed(db, failed, now=NOW) is None
        # The new attempt freezes a NEW marker; the old one never returns.
        assert repo.begin_append(
            db, new, account_key="me@example.com", bundle=_bundle(MARKER_2), now=NOW
        )
        db.commit()
        assert _snap(db).marker_message_id == MARKER_2

    def test_unarmed_release_to_failed(self, db):
        claim = _claim(db)
        assert not repo.finalize_failed(db, claim, marker=MARKER, error_code="X", now=NOW)
        assert repo.finalize_failed(db, claim, marker=None, error_code="PACKAGE_STALE", now=NOW)
        db.commit()
        assert _snap(db).state == "FAILED" and not _snap(db).armed

    def test_unarmed_claim_can_never_become_uncertain_or_created(self, db):
        claim = _claim(db)
        assert not repo.finalize_uncertain(db, claim, marker=MARKER, error_code="X", now=NOW)
        assert not repo.finalize_created(
            db, claim, marker=MARKER, uid_validity=None, draft_uid=None, now=NOW
        )


class TestStaleAndPositiveEvidence:
    def test_stale_armed_needs_frozen_deadline_plus_margin(self, db):
        _armed(db)
        snap = _snap(db)
        at_deadline = NOW + timedelta(seconds=60 + 30)
        assert not repo.classify_stale_armed(db, snap, stale_margin_seconds=30, now=at_deadline)
        later = at_deadline + timedelta(seconds=1)
        assert repo.classify_stale_armed(db, snap, stale_margin_seconds=30, now=later)
        db.commit()
        result = _snap(db)
        assert result.state == "UNCERTAIN" and result.claim_token is None
        assert result.marker_message_id == MARKER  # bundle preserved

    def test_reconcile_binds_id_state_attempt_and_marker(self, db):
        claim = _armed(db)
        repo.finalize_uncertain(db, claim, marker=MARKER, error_code="X", now=NOW)
        db.commit()
        snap = _snap(db)
        common = dict(uid_validity=7, draft_uid=42, now=NOW)
        assert not repo.reconcile_created(
            db, ledger_id=snap.id, attempt_count=2, marker=MARKER, **common
        )
        assert not repo.reconcile_created(
            db, ledger_id=snap.id, attempt_count=1, marker=MARKER_2, **common
        )
        assert not repo.reconcile_created(
            db, ledger_id=snap.id + 1, attempt_count=1, marker=MARKER, **common
        )
        assert repo.reconcile_created(
            db, ledger_id=snap.id, attempt_count=1, marker=MARKER, **common
        )
        db.commit()
        result = _snap(db)
        assert (result.state, result.reconciled, result.uid_validity, result.draft_uid) == (
            "CREATED",
            True,
            7,
            42,
        )

    def test_retained_ok_never_overwrites_an_earlier_reconciliation(self, db):
        claim = _armed(db)
        repo.finalize_uncertain(db, claim, marker=MARKER, error_code="X", now=NOW)
        db.commit()
        snap = _snap(db)
        repo.reconcile_created(
            db,
            ledger_id=snap.id,
            attempt_count=1,
            marker=MARKER,
            uid_validity=7,
            draft_uid=42,
            now=NOW,
        )
        db.commit()
        assert not repo.retained_ok_created(
            db,
            ledger_id=snap.id,
            attempt_count=1,
            marker=MARKER,
            uid_validity=None,
            draft_uid=None,
            now=NOW + timedelta(seconds=1),
        )
        assert _snap(db).reconciled is True and _snap(db).draft_uid == 42

    def test_retained_ok_sets_reconciled_false_and_nullable_uids(self, db):
        claim = _armed(db)
        repo.finalize_uncertain(db, claim, marker=MARKER, error_code="X", now=NOW)
        db.commit()
        snap = _snap(db)
        assert repo.retained_ok_created(
            db,
            ledger_id=snap.id,
            attempt_count=1,
            marker=MARKER,
            uid_validity=None,
            draft_uid=None,
            now=NOW,
        )
        db.commit()
        result = _snap(db)
        assert (result.state, result.reconciled, result.uid_validity) == ("CREATED", False, None)
        assert result.claim_token is None and result.created_in_gmail_at is not None


def _insert_raw(db, **overrides):
    values = dict(
        link_id=1,
        account_key="me@example.com",
        review_id=1,
        approved_revision_id=1,
        preparation_id=1,
        generation=1,
        match_id=1,
        cv_draft_id=1,
        bewerbung_draft_id=1,
        package_token="P" * 16,
        input_identity="i" * 64,
        state="FAILED",
        attempt_count=1,
        reconciled=False,
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(overrides)
    db.add(GmailApplicationDraftRecord(**values))
    db.commit()


_ARMED = dict(
    append_started_at=NOW,
    marker_message_id=MARKER,
    content_sha256="h" * 64,
    renderer_version="9d-v1",
    drafts_mailbox="[Gmail]/Drafts",
    drafts_mailbox_wire='"[Gmail]/Drafts"',
    attempt_budget_seconds=60,
)


@pytest.mark.parametrize(
    "values",
    [
        {"state": "SENT"},
        {"attempt_count": 0},
        {"generation": 0},
        {"state": "CREATING"},  # CREATING without a claim
        {"state": "CREATING", "claim_token": "t" * 22},  # half claim
        {"state": "FAILED", "claim_token": "t" * 22},  # claim outside CREATING
        {"state": "FAILED", "claim_started_at": NOW},  # half claim outside CREATING
        {"state": "UNCERTAIN"},  # outcome without a fence
        {"state": "CREATED", "created_in_gmail_at": NOW},  # outcome without a fence
        {**_ARMED, "marker_message_id": None},  # partial bundle
        {**_ARMED, "drafts_mailbox_wire": None},
        {**_ARMED, "attempt_budget_seconds": None},
        {"marker_message_id": MARKER},  # bundle without fence
        {**_ARMED, "state": "CREATED"},  # CREATED without confirmation time
        {**_ARMED, "state": "CREATED", "created_in_gmail_at": NOW, "uid_validity": 7},
        {
            **_ARMED,
            "state": "CREATED",
            "created_in_gmail_at": NOW,
            "uid_validity": 0,
            "draft_uid": 1,
        },
        {
            **_ARMED,
            "state": "CREATED",
            "created_in_gmail_at": NOW,
            "uid_validity": 1,
            "draft_uid": 4294967296,
        },
        {**_ARMED, "state": "CREATED", "created_in_gmail_at": NOW, "reconciled": True},
        {**_ARMED, "state": "UNCERTAIN", "uid_validity": 1, "draft_uid": 1},
        {**_ARMED, "attempt_budget_seconds": 0},
        {**_ARMED, "marker_message_id": "<short@x>"},
    ],
)
def test_check_constraints_reject(db, values):
    with pytest.raises(IntegrityError):
        _insert_raw(db, **values)


@pytest.mark.parametrize(
    "values",
    [
        {"state": "FAILED"},
        {"state": "CREATING", "claim_token": "t" * 22, "claim_started_at": NOW},
        {**_ARMED, "state": "FAILED"},
        {**_ARMED, "state": "UNCERTAIN"},
        {**_ARMED, "state": "CREATED", "created_in_gmail_at": NOW},
        {
            **_ARMED,
            "state": "CREATED",
            "created_in_gmail_at": NOW,
            "uid_validity": 4294967295,
            "draft_uid": 1,
            "reconciled": True,
        },
    ],
)
def test_check_constraints_accept(db, values):
    _insert_raw(db, **values)


def test_snapshot_repr_leaks_nothing_sensitive(db):
    _armed(db)
    text_ = repr(_snap(db))
    for secret in (MARKER, "me@example.com", "[Gmail]/Drafts", "h" * 64, "P" * 16):
        assert secret not in text_


# --- migration ------------------------------------------------------------------


def _alembic_config(db_path: Path, buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=buffer)
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _normalize(sql: str) -> str:
    return " ".join(sql.replace("(", " ( ").replace(")", " ) ").split()).lower()


def test_migration_upgrade_downgrade_upgrade_with_orm_parity(tmp_path):
    db_path = tmp_path / "migration.db"
    cfg = _alembic_config(db_path)
    table = GmailApplicationDraftRecord.__tablename__
    model_table = GmailApplicationDraftRecord.__table__

    upgrade(cfg, "head")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    assert set(columns) == {c.name for c in model_table.columns}
    for column in model_table.columns:
        if not column.primary_key:
            assert columns[column.name]["nullable"] == column.nullable, column.name
        length = getattr(column.type, "length", None)
        assert getattr(columns[column.name]["type"], "length", None) == length, column.name
    # S9D-CONF-001: the exact approved wire bound, in BOTH ORM and migration.
    assert model_table.c.drafts_mailbox_wire.type.length == 2048
    assert columns["drafts_mailbox_wire"]["type"].length == 2048

    uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints(table)}
    assert uniques == {
        "uq_gmail_application_drafts_link_id": ["link_id"],
        "uq_gmail_application_drafts_marker_message_id": ["marker_message_id"],
    }
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table)}
    assert set(checks) == set(GMAIL_DRAFT_CHECKS)
    for name, body in GMAIL_DRAFT_CHECKS.items():
        assert _normalize(checks[name]) == _normalize(body), name
    indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes(table)}
    assert indexes["ix_gmail_application_drafts_state_claim"] == ["state", "claim_started_at"]
    assert inspector.get_foreign_keys(table) == []  # historical, never cascades

    downgrade(cfg, "e5a17c3b9d42")
    names = inspect(create_engine(f"sqlite:///{db_path}")).get_table_names()
    assert table not in names and "telegram_bewerbung_approvals" in names

    upgrade(cfg, "head")
    assert table in inspect(create_engine(f"sqlite:///{db_path}")).get_table_names()


def test_migrated_schema_enforces_the_same_constraints(tmp_path):
    db_path = tmp_path / "migration_checks.db"
    upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    session = sessionmaker(bind=engine)()
    try:
        _insert_raw(session, **_ARMED, state="UNCERTAIN")
        with pytest.raises(IntegrityError):
            _insert_raw(session, link_id=2, state="UNCERTAIN")  # no fence
        session.rollback()
        with pytest.raises(IntegrityError):
            _insert_raw(session, link_id=1)  # UNIQUE(link_id)
        session.rollback()
        assert session.scalar(select(GmailApplicationDraftRecord.reconciled)) in (False, 0)
        assert session.execute(text("SELECT count(*) FROM gmail_application_drafts")).scalar() == 1
    finally:
        session.close()


def test_migration_postgresql_offline_ddl(tmp_path):
    buffer = io.StringIO()
    cfg = _alembic_config(tmp_path / "unused.db", buffer)
    cfg.set_main_option("sqlalchemy.url", "postgresql://user:pass@localhost/offline")

    upgrade(cfg, "e5a17c3b9d42:f3b8d2a6c9e1", sql=True)

    sql = buffer.getvalue()
    create = sql[sql.index("CREATE TABLE gmail_application_drafts") :]
    create = create[: create.index(";")]
    assert "drafts_mailbox_wire VARCHAR(2048)" in create
    assert "uid_validity BIGINT" in create and "draft_uid BIGINT" in create
    assert "REFERENCES" not in create and "CASCADE" not in create
    for name in (*GMAIL_DRAFT_CHECKS, "uq_gmail_application_drafts_link_id"):
        assert name in create
    assert "ix_gmail_application_drafts_state_claim" in sql
    # The existing tables are untouched.
    assert "ALTER TABLE" not in sql.split("CREATE TABLE gmail_application_drafts")[1]


def test_migration_parent_is_the_stage_9c_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config(Path("unused.db")))
    revision = script.get_revision("f3b8d2a6c9e1")
    assert revision.down_revision == "e5a17c3b9d42"
    assert script.get_heads() == ["f3b8d2a6c9e1"]
