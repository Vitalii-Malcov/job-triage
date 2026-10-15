"""Stage 14A send-readiness gate over a REAL persisted package.

Seeds profile -> match -> CV -> Bewerbung -> review -> approval through the
existing services (SQLite file, no network), then proves the read-only
loader: package currency fails closed on every stale or mismatched pin,
a dirty caller session is refused without autoflush, and the CLI accepts
only a local read-only SQLite URI before creating any engine.
"""

import asyncio
import hashlib
import socket
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

import scripts.stage14a_send_readiness as cli
from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
)
from app.db.models import (
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    CandidateJobMatchRecord,
    JobRecord,
)
from app.models.application_destination import ApplicationDestination
from app.models.candidate_profile import (
    CandidateExperience,
    CandidateProfilePatchRequest,
    CandidateSkill,
)
from app.services import send_readiness
from app.services.candidate_preparation import (
    prepare_bewerbung_draft,
    prepare_candidate_cv_draft,
    prepare_candidate_job_match,
)
from app.services.review_package import ReviewPackageService
from app.services.send_readiness import (
    CHECK_APPROVED_IS_CURRENT,
    NO_APPROVED_REVIEW,
    PINS_INCONSISTENT,
    REVISION_NOT_CURRENT,
    REVISION_NOT_OWNED_BY_REVIEW,
    STALE_CV_ADAPTER_VERSION,
    STALE_GENERATOR_VERSION,
    STALE_JOB_FINGERPRINT,
    STALE_LETTER,
    STALE_MATCHER_VERSION,
    STALE_PROFILE,
    SUPERSEDED_BY_NEWER_REVIEW,
    LocationEvidence,
    SessionNotCleanError,
    evaluate_send_readiness,
    load_send_readiness_inputs,
    render_send_readiness,
)

_NOW = datetime(2026, 10, 15, tzinfo=UTC)
_AD = "https://careers.example.com/jobs/python"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "stage14a_gate.db"


@pytest.fixture()
def factory(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def _seed_profile(db: Session) -> None:
    record = get_or_create_candidate_profile(db)
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=record.profile_version,
            first_name="Anna",
            last_name="Example",
            professional_title="Python Developer",
            skills=[CandidateSkill(name="Python")],
            experiences=[
                CandidateExperience(
                    company="Example Dev GmbH", job_title="Developer", technologies=["Python"]
                )
            ],
        ),
    )


def _seed_job(db: Session) -> int:
    job = JobRecord(
        fingerprint="fp-14a",
        source="bundesagentur",
        title="Python Developer (m/w/d)",
        company="Example GmbH",
        location="Hannover",
        url="https://example.com/jobs/1",
        description="Wir suchen Python Entwicklung.",
        skills_json="[]",
        must_have_skills_json='["python"]',
        nice_to_have_skills_json="[]",
        score=80,
        recommendation="APPLY",
        status="NEW",
        first_seen_at=_NOW,
        last_seen_at=_NOW,
    )
    db.add(job)
    db.commit()
    return job.id


def _prepare_and_approve(db: Session, job_id: int) -> int:
    match = prepare_candidate_job_match(db, job_id, force_recompute=False)
    cv = prepare_candidate_cv_draft(db, job_id, match.id, force_recompute=False)
    letter = asyncio.run(prepare_bewerbung_draft(db, job_id, cv.id))
    job = db.get(JobRecord, job_id)
    service = ReviewPackageService()
    review = service.create(db, job, cv.id, letter.id)
    service.approve(
        db,
        review.id,
        review.review_version,
        acknowledge_manual_overrides=False,
        decision_note=None,
    )
    return review.id


@pytest.fixture()
def seeded(factory):
    with factory() as db:
        _seed_profile(db)
        job_id = _seed_job(db)
        review_id = _prepare_and_approve(db, job_id)
    return job_id, review_id


def _destination(job_id: int) -> ApplicationDestination:
    return ApplicationDestination(
        job_id=job_id,
        kind="APPLICATION_PORTAL",
        value=_AD + "#apply",
        source_url=_AD,
        source_type="EMPLOYER_JOB_AD",
        verification_status="VERIFIED",
        confidence="HIGH",
        verified_by="OPERATOR",
        vacancy_specific=True,
        checked_at=_NOW,
    )


_EVIDENCE = [
    LocationEvidence(
        source_type="EMPLOYER_JOB_AD",
        source_url=_AD,
        locations=["Hannover"],
        authoritative=True,
        checked_at=_NOW,
    )
]


def _evaluate(factory, job_id: int):
    with factory() as db:
        job = db.get(JobRecord, job_id)
        inputs = load_send_readiness_inputs(
            db,
            job,
            settings=Settings(_env_file=None),
            destination=_destination(job_id),
            location_evidence=_EVIDENCE,
        )
        db.rollback()
    return inputs, evaluate_send_readiness(inputs)


def _execute(factory, stmt) -> None:
    with factory() as db:
        db.execute(stmt)
        db.commit()


def _review(factory, review_id: int) -> ApplicationPackageReviewRecord:
    with factory() as db:
        return db.get(ApplicationPackageReviewRecord, review_id)


# --- real package: positive baseline ---------------------------------------


def test_current_operator_verified_package_is_ready(factory, seeded):
    job_id, _ = seeded
    inputs, result = _evaluate(factory, job_id)
    assert inputs.package_problems == ()
    assert result.ready, render_send_readiness(result)


def test_no_approved_review_fails_closed(factory):
    with factory() as db:
        _seed_profile(db)
        job_id = _seed_job(db)
    inputs, result = _evaluate(factory, job_id)
    assert inputs.package_problems == (NO_APPROVED_REVIEW,)
    assert not result.ready


# --- package currency / pinning --------------------------------------------


def _problems(factory, job_id: int) -> tuple[str, ...]:
    inputs, result = _evaluate(factory, job_id)
    assert not result.ready
    assert CHECK_APPROVED_IS_CURRENT in {c.code for c in result.blockers}
    return inputs.package_problems


def test_stale_job_fingerprint(factory, seeded):
    job_id, _ = seeded
    _execute(
        factory,
        update(JobRecord).where(JobRecord.id == job_id).values(description="Neue Beschreibung."),
    )
    problems = _problems(factory, job_id)
    assert STALE_JOB_FINGERPRINT in problems
    assert STALE_LETTER in problems


def test_stale_job_display_context(factory, seeded):
    """Company rename: fingerprint unchanged, letter still names the old one."""
    job_id, _ = seeded
    _execute(factory, update(JobRecord).where(JobRecord.id == job_id).values(company="Renamed AG"))
    assert _problems(factory, job_id) == (STALE_LETTER,)


def test_stale_profile(factory, seeded):
    job_id, _ = seeded
    with factory() as db:
        record = get_or_create_candidate_profile(db)
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=record.profile_version, location_city="Hannover"
            ),
        )
    assert STALE_PROFILE in _problems(factory, job_id)


@pytest.mark.parametrize(
    "column_sets,expected",
    [
        (
            {
                ApplicationPackageReviewRecord: {"match_algorithm_version": "v0"},
                CandidateCVDraftRecord: {"match_algorithm_version": "v0"},
                CandidateJobMatchRecord: {"algorithm_version": "v0"},
                BewerbungDraftRecord: {"match_algorithm_version": "v0"},
            },
            STALE_MATCHER_VERSION,
        ),
        (
            {
                ApplicationPackageReviewRecord: {"cv_adapter_version": "v1"},
                CandidateCVDraftRecord: {"cv_adapter_version": "v1"},
                BewerbungDraftRecord: {"cv_adapter_version": "v1"},
            },
            STALE_CV_ADAPTER_VERSION,
        ),
        (
            {
                ApplicationPackageReviewRecord: {"bewerbung_generator_version": "v2"},
                BewerbungDraftRecord: {"bewerbung_generator_version": "v2"},
            },
            STALE_GENERATOR_VERSION,
        ),
    ],
)
def test_old_semantic_versions_fail_closed(factory, seeded, column_sets, expected):
    """A package generated (consistently) under an older matcher / CV
    adapter / generator version is never current."""
    job_id, _ = seeded
    for table, values in column_sets.items():
        _execute(factory, update(table).values(**values))
    problems = _problems(factory, job_id)
    assert expected in problems
    assert PINS_INCONSISTENT not in problems


def test_wrong_revision_review_relation(factory, seeded):
    job_id, review_id = seeded
    review = _review(factory, review_id)
    with factory() as db:
        source = db.get(ApplicationPackageReviewRevisionRecord, review.approved_revision_id)
        new_id = db.execute(
            insert(ApplicationPackageReviewRevisionRecord)
            .values(
                review_id=review_id + 100,
                revision_number=source.revision_number,
                reviewed_cv_json=source.reviewed_cv_json,
                reviewed_bewerbung_json=source.reviewed_bewerbung_json,
                manual_override_paths_json=source.manual_override_paths_json,
            )
            .returning(ApplicationPackageReviewRevisionRecord.id)
        ).scalar_one()
        db.commit()
    _execute(
        factory,
        update(ApplicationPackageReviewRecord)
        .where(ApplicationPackageReviewRecord.id == review_id)
        .values(approved_revision_id=new_id),
    )
    inputs, result = _evaluate(factory, job_id)
    assert REVISION_NOT_OWNED_BY_REVIEW in inputs.package_problems
    assert inputs.approved_package is None  # foreign revision content never loaded
    assert not result.ready


def test_approved_revision_not_current(factory, seeded):
    job_id, review_id = seeded
    review = _review(factory, review_id)
    with factory() as db:
        source = db.get(ApplicationPackageReviewRevisionRecord, review.approved_revision_id)
        db.execute(
            insert(ApplicationPackageReviewRevisionRecord).values(
                review_id=review_id,
                revision_number=source.revision_number + 1,
                reviewed_cv_json=source.reviewed_cv_json,
                reviewed_bewerbung_json=source.reviewed_bewerbung_json,
                manual_override_paths_json=source.manual_override_paths_json,
            )
        )
        db.commit()
    assert REVISION_NOT_CURRENT in _problems(factory, job_id)


def test_approved_revision_number_differs_from_review_version(factory, seeded):
    job_id, review_id = seeded
    _execute(
        factory,
        update(ApplicationPackageReviewRecord)
        .where(ApplicationPackageReviewRecord.id == review_id)
        .values(review_version=ApplicationPackageReviewRecord.review_version + 1),
    )
    assert REVISION_NOT_CURRENT in _problems(factory, job_id)


@pytest.mark.parametrize(
    "stmt_factory",
    [
        lambda: update(BewerbungDraftRecord).values(cv_draft_id=9999),
        lambda: update(ApplicationPackageReviewRecord).values(match_id=9999),
        lambda: update(BewerbungDraftRecord).values(match_id=9999),
    ],
)
def test_wrong_pinned_draft_pair(factory, seeded, stmt_factory):
    job_id, _ = seeded
    _execute(factory, stmt_factory())
    problems = _problems(factory, job_id)
    assert PINS_INCONSISTENT in problems or "PINNED_DRAFT_MISSING" in problems


def test_newer_review_supersedes_the_approved_one(factory, seeded):
    job_id, review_id = seeded
    review = _review(factory, review_id)
    with factory() as db:
        job = db.get(JobRecord, job_id)
        ReviewPackageService().create(db, job, review.cv_draft_id, review.bewerbung_draft_id)
    assert SUPERSEDED_BY_NEWER_REVIEW in _problems(factory, job_id)


# --- strict read-only loader -------------------------------------------------


def _snapshot(db_path) -> str:
    return hashlib.sha256(db_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ["dirty", "new", "deleted"])
def test_dirty_session_is_refused_without_autoflush(factory, seeded, db_path, mutation):
    job_id, _ = seeded
    before = _snapshot(db_path)
    with factory() as db:
        db.autoflush = True  # the worst case: a caller session that autoflushes
        flushes: list[int] = []
        event.listen(db, "before_flush", lambda *a: flushes.append(1))
        job = db.get(JobRecord, job_id)
        if mutation == "dirty":
            job.title = "CALLER PENDING EDIT"
        elif mutation == "new":
            db.add(JobRecord(fingerprint="pending", source="x", title="t", company="c", url="u"))
        else:
            db.delete(db.get(ApplicationPackageReviewRecord, seeded[1]))
        with pytest.raises(SessionNotCleanError):
            load_send_readiness_inputs(
                db,
                job,
                settings=Settings(_env_file=None),
                destination=None,
                location_evidence=[],
            )
        assert flushes == []
        db.rollback()
    assert _snapshot(db_path) == before
    with factory() as db:
        assert db.get(JobRecord, job_id).title == "Python Developer (m/w/d)"
        assert db.get(ApplicationPackageReviewRecord, seeded[1]) is not None
        assert db.scalar(select(JobRecord).where(JobRecord.fingerprint == "pending")) is None


def test_clean_load_reads_only_under_no_autoflush(factory, seeded, db_path, monkeypatch):
    job_id, _ = seeded

    def _no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    seen_autoflush: list[bool] = []
    original = send_readiness.get_latest_approved_review_for_job

    def _spy(db, job_id):
        seen_autoflush.append(db.autoflush)
        return original(db, job_id)

    monkeypatch.setattr(send_readiness, "get_latest_approved_review_for_job", _spy)
    before = _snapshot(db_path)
    with factory() as db:
        db.autoflush = True
        statements: list[str] = []
        event.listen(
            db.get_bind(),
            "before_cursor_execute",
            lambda conn, cursor, stmt, *a: statements.append(stmt),
        )
        flushes: list[int] = []
        event.listen(db, "before_flush", lambda *a: flushes.append(1))
        job = db.get(JobRecord, job_id)
        inputs = load_send_readiness_inputs(
            db,
            job,
            settings=Settings(_env_file=None),
            destination=None,
            location_evidence=[],
        )
        assert db.autoflush is True  # restored after the reads
        assert not db.new and not db.dirty and not db.deleted
        db.rollback()
    assert seen_autoflush == [False]
    assert flushes == []
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert _snapshot(db_path) == before
    assert inputs.package_problems == ()


def test_loader_rejects_destination_of_other_job(factory, seeded):
    job_id, _ = seeded
    with factory() as db:
        with pytest.raises(ValueError, match="different job"):
            load_send_readiness_inputs(
                db,
                db.get(JobRecord, job_id),
                settings=Settings(_env_file=None),
                destination=_destination(job_id + 1),
                location_evidence=[],
            )


# --- CLI: local read-only SQLite only ------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pw@db.example.com/jobs",
        "postgresql+psycopg://user:pw@127.0.0.1:5432/jobs",
        "mysql://user:pw@db.example.com/jobs",
        "sqlite:///stage11_pilot.db",
        "sqlite:///file:stage11_pilot.db",
        "sqlite:///file:stage11_pilot.db?mode=rw&uri=true",
        "sqlite:///file:stage11_pilot.db?mode=ro",
        "sqlite:///file:stage11_pilot.db?mode=ro&uri=true&vfs=unix",
        "sqlite:///file:stage11_pilot.db?mode=ro&uri=true&cache=shared",
        "sqlite+pysqlite:///file:stage11_pilot.db?mode=ro&uri=true",
        "sqlite://user@host/file:stage11_pilot.db?mode=ro&uri=true",
        "sqlite:///file://fileserver/share/pilot.db?mode=ro&uri=true",
        "sqlite:///file:\\\\fileserver\\share\\pilot.db?mode=ro&uri=true",
        "sqlite:///file::memory:?mode=ro&uri=true",
        "sqlite:///file:pilot%2Edb?mode=ro&uri=true",
        "sqlite:///file:?mode=ro&uri=true",
        "sqlite://",
        "",
    ],
)
def test_cli_rejects_network_capable_or_writable_urls_before_engine(monkeypatch, url, capsys):
    def _forbidden(*args, **kwargs):
        raise AssertionError("engine created for an unvalidated URL")

    monkeypatch.setattr(cli, "create_engine", _forbidden)
    assert cli.main(["--database-url", url, "--job-id", "1"]) == 2
    assert "refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///file:stage11_pilot.db?mode=ro&uri=true",
        "sqlite:///file:C:/data/stage11_pilot.db?mode=ro&uri=true",
        "sqlite:///file:/srv/data/stage11_pilot.db?mode=ro&uri=true",
    ],
)
def test_cli_accepts_local_readonly_sqlite_uri(url):
    assert cli.validate_readonly_sqlite_url(url) == url


def test_cli_runs_read_only_against_a_real_db(factory, seeded, db_path, capsys, monkeypatch):
    job_id, _ = seeded

    def _no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setenv("OUTBOUND_SENDING_ENABLED", "false")
    before = _snapshot(db_path)
    url = f"sqlite:///file:{db_path.as_posix()}?mode=ro&uri=true"
    code = cli.main(["--database-url", url, "--job-id", str(job_id)])
    out = capsys.readouterr().out
    assert code == 1  # no destination / location evidence given -> NOT ready
    assert "READY_FOR_SEND_PACKAGE = NO" in out
    assert "[PASS] APPROVED_PACKAGE_MATCHES_CURRENT_PROFILE" in out
    assert _snapshot(db_path) == before


def test_cli_connection_is_query_only(db_path, factory):
    engine = create_engine(f"sqlite:///file:{db_path.as_posix()}?mode=ro&uri=true")
    event.listen(engine, "connect", cli._query_only)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar() == 1
    finally:
        engine.dispose()
