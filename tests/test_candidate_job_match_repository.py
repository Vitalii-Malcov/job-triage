import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    create_match,
    get_cached_match,
    get_latest_match,
    to_candidate_job_match,
)
from app.db.models import CandidateJobMatchRecord, JobRecord
from app.models.candidate_job_match import CandidateJobMatchData


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _file_session_factory(tmp_path, name: str):
    db_path = tmp_path / name
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_job(db: Session, **overrides) -> JobRecord:
    now = datetime.now(UTC)
    data = dict(
        fingerprint=overrides.pop("fingerprint", "fp-default"),
        source="bundesagentur",
        title="Python Developer",
        company="Acme",
        location="Berlin",
        url="https://example.com/jobs/1",
        description="Python required.",
        skills_json="[]",
        data_confidence=0.9,
        must_have_skills_json=json.dumps(["python"]),
        nice_to_have_skills_json="[]",
        score=80,
        recommendation="APPLY",
        status="NEW",
        first_seen_at=now,
        last_seen_at=now,
    )
    data.update(overrides)
    job = JobRecord(**data)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _data(job_id: int, **overrides) -> CandidateJobMatchData:
    defaults = dict(
        job_id=job_id,
        candidate_profile_version=1,
        company_research_id=None,
        algorithm_version="v1",
        overall_score=50,
        coverage_score=50,
        required_skill_score=50,
        preferred_skill_score=100,
        experience_support_score=50,
    )
    defaults.update(overrides)
    return CandidateJobMatchData(**defaults)


# --- fingerprint -------------------------------------------------------


def test_fingerprint_is_deterministic_for_same_content():
    db = _db()
    job = _seed_job(db)
    assert compute_job_snapshot_fingerprint(job) == compute_job_snapshot_fingerprint(job)


def test_fingerprint_changes_when_description_changes():
    db = _db()
    job = _seed_job(db)
    fp1 = compute_job_snapshot_fingerprint(job)
    job.description = "Completely different description."
    fp2 = compute_job_snapshot_fingerprint(job)
    assert fp1 != fp2


def test_fingerprint_changes_when_must_have_skills_change():
    db = _db()
    job = _seed_job(db)
    fp1 = compute_job_snapshot_fingerprint(job)
    job.must_have_skills_json = json.dumps(["python", "docker"])
    fp2 = compute_job_snapshot_fingerprint(job)
    assert fp1 != fp2


def test_fingerprint_is_case_insensitive_for_title_and_description():
    db = _db()
    job_a = _seed_job(
        db, fingerprint="fp-a", title="Python Developer", description="Python required."
    )
    job_b = _seed_job(
        db, fingerprint="fp-b", title="python developer", description="PYTHON REQUIRED."
    )
    assert compute_job_snapshot_fingerprint(job_a) == compute_job_snapshot_fingerprint(job_b)


# --- create / cache lookup -----------------------------------------------


def test_create_match_persists_and_round_trips():
    db = _db()
    job = _seed_job(db)
    fingerprint = compute_job_snapshot_fingerprint(job)
    record, created = create_match(db, _data(job.id), fingerprint)

    assert created is True
    response = to_candidate_job_match(record)
    assert response.job_id == job.id
    assert response.overall_score == 50
    assert response.id == record.id


def test_get_cached_match_returns_none_when_no_row_exists():
    db = _db()
    result = get_cached_match(
        db,
        job_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="does-not-exist",
        algorithm_version="v1",
    )
    assert result is None


def test_get_cached_match_exact_identity_lookup():
    db = _db()
    job = _seed_job(db)
    fingerprint = compute_job_snapshot_fingerprint(job)
    record, _ = create_match(db, _data(job.id), fingerprint)

    found = get_cached_match(
        db,
        job_id=job.id,
        candidate_profile_version=1,
        job_snapshot_fingerprint=fingerprint,
        algorithm_version="v1",
    )
    assert found is not None
    assert found.id == record.id


def test_get_cached_match_misses_on_different_profile_version():
    db = _db()
    job = _seed_job(db)
    fingerprint = compute_job_snapshot_fingerprint(job)
    create_match(db, _data(job.id, candidate_profile_version=1), fingerprint)

    found = get_cached_match(
        db,
        job_id=job.id,
        candidate_profile_version=2,
        job_snapshot_fingerprint=fingerprint,
        algorithm_version="v1",
    )
    assert found is None


def test_get_cached_match_misses_on_different_fingerprint():
    db = _db()
    job = _seed_job(db)
    fingerprint = compute_job_snapshot_fingerprint(job)
    create_match(db, _data(job.id), fingerprint)

    found = get_cached_match(
        db,
        job_id=job.id,
        candidate_profile_version=1,
        job_snapshot_fingerprint="different-fingerprint",
        algorithm_version="v1",
    )
    assert found is None


def test_get_cached_match_misses_on_different_algorithm_version():
    db = _db()
    job = _seed_job(db)
    fingerprint = compute_job_snapshot_fingerprint(job)
    create_match(db, _data(job.id, algorithm_version="v1"), fingerprint)

    found = get_cached_match(
        db,
        job_id=job.id,
        candidate_profile_version=1,
        job_snapshot_fingerprint=fingerprint,
        algorithm_version="v2",
    )
    assert found is None


def test_get_latest_match_returns_most_recent_row():
    db = _db()
    job = _seed_job(db)
    fingerprint1 = compute_job_snapshot_fingerprint(job)
    create_match(db, _data(job.id, candidate_profile_version=1, overall_score=10), fingerprint1)

    job.description = "Updated description."
    db.commit()
    fingerprint2 = compute_job_snapshot_fingerprint(job)
    second, _ = create_match(
        db, _data(job.id, candidate_profile_version=1, overall_score=90), fingerprint2
    )

    latest = get_latest_match(db, job.id)
    assert latest.id == second.id
    assert latest.overall_score == 90


def test_get_latest_match_returns_none_when_no_match_exists():
    db = _db()
    assert get_latest_match(db, 999) is None


def test_profile_version_change_produces_a_new_row_not_an_overwrite():
    db = _db()
    job = _seed_job(db)
    fingerprint = compute_job_snapshot_fingerprint(job)
    create_match(db, _data(job.id, candidate_profile_version=1), fingerprint)
    create_match(db, _data(job.id, candidate_profile_version=2), fingerprint)

    total = db.scalar(select(func.count()).select_from(CandidateJobMatchRecord))
    assert total == 2


# --- concurrency (real independent sessions) --------------------------------


def test_concurrent_create_for_identical_cache_identity_does_not_duplicate(tmp_path):
    """Section 34: two concurrent POSTs computing the identical cache
    identity must not create two rows — the DB UNIQUE constraint plus
    IntegrityError/reload-winner handling must converge on exactly one.
    """
    factory = _file_session_factory(tmp_path, "concurrent_match.db")
    seed_db = factory()
    job = _seed_job(seed_db)
    job_id = job.id
    fingerprint = compute_job_snapshot_fingerprint(job)
    seed_db.close()

    db_a = factory()
    db_b = factory()

    data = _data(job_id, overall_score=42)
    record_a, created_a = create_match(db_a, data, fingerprint)
    record_b, created_b = create_match(db_b, data, fingerprint)

    assert created_a is True
    assert created_b is False
    assert record_a.id == record_b.id

    final = factory()
    total = final.scalar(select(func.count()).select_from(CandidateJobMatchRecord))
    assert total == 1
    db_a.close()
    db_b.close()
    final.close()


def test_privacy_safe_analysis_json_contains_no_extra_pii_fields():
    """The persisted analysis_json is exactly the computed content — no
    accidental leakage beyond what CandidateJobMatchData defines.
    """
    db = _db()
    job = _seed_job(db)
    fingerprint = compute_job_snapshot_fingerprint(job)
    record, _ = create_match(db, _data(job.id), fingerprint)

    payload = json.loads(record.analysis_json)
    assert set(payload.keys()) == set(CandidateJobMatchData.model_fields.keys())
