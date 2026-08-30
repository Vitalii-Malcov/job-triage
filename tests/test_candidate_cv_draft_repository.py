from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.candidate_cv_draft_repository import (
    create_draft,
    get_cached_draft,
    get_draft_by_id,
    get_latest_draft,
    to_tailored_cv_draft,
)
from app.db.models import CandidateCVDraftRecord
from app.models.cv_draft import CVHeader, CVTopLevelFact, TailoredCVDraftData


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _file_session_factory(tmp_path, name: str):
    db_path = tmp_path / name
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _data(**overrides) -> TailoredCVDraftData:
    defaults = dict(
        job_id=1,
        match_id=1,
        candidate_profile_version=1,
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        header=CVHeader(
            first_name=CVTopLevelFact(
                value="Anna", source_id=1, source_field="first_name", profile_version=1
            )
        ),
    )
    defaults.update(overrides)
    return TailoredCVDraftData(**defaults)


# --- create / cache lookup -----------------------------------------------


def test_create_draft_persists_and_round_trips():
    db = _db()
    record, created = create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data())

    assert created is True
    response = to_tailored_cv_draft(record)
    assert response.job_id == 1
    assert response.match_id == 1
    assert response.header.first_name.value == "Anna"
    assert response.id == record.id


def test_get_cached_draft_returns_none_when_no_row_exists():
    db = _db()
    assert get_cached_draft(db, match_id=1, cv_adapter_version="v1") is None


def test_get_cached_draft_exact_identity_lookup():
    db = _db()
    record, _ = create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data())
    found = get_cached_draft(db, match_id=1, cv_adapter_version="v1")
    assert found is not None
    assert found.id == record.id


def test_get_cached_draft_misses_on_different_adapter_version():
    db = _db()
    create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(cv_adapter_version="v1"))
    assert get_cached_draft(db, match_id=1, cv_adapter_version="v2") is None


def test_get_cached_draft_misses_on_different_match_id():
    db = _db()
    create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(match_id=1))
    assert get_cached_draft(db, match_id=2, cv_adapter_version="v1") is None


def test_new_match_id_produces_a_new_row_not_an_overwrite():
    db = _db()
    create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(match_id=1))
    create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(match_id=2))
    total = db.scalar(select(func.count()).select_from(CandidateCVDraftRecord))
    assert total == 2


def test_new_adapter_version_produces_a_new_row_not_an_overwrite():
    db = _db()
    create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(cv_adapter_version="v1"))
    create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(cv_adapter_version="v2"))
    total = db.scalar(select(func.count()).select_from(CandidateCVDraftRecord))
    assert total == 2


def test_get_latest_draft_returns_most_recent_row():
    db = _db()
    first, _ = create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(match_id=1))
    second, _ = create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data(match_id=2))

    latest = get_latest_draft(db, 1)
    assert latest.id == second.id
    assert latest.id != first.id


def test_get_latest_draft_returns_none_when_no_draft_exists():
    db = _db()
    assert get_latest_draft(db, 999) is None


def test_get_draft_by_id_returns_exact_snapshot():
    db = _db()
    record, _ = create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data())
    found = get_draft_by_id(db, record.id)
    assert found is not None
    assert found.id == record.id


def test_get_draft_by_id_returns_none_for_unknown_id():
    db = _db()
    assert get_draft_by_id(db, 999) is None


def test_draft_is_immutable_snapshot_of_generation_time_metadata():
    db = _db()
    record, _ = create_draft(
        db,
        job_id=1,
        job_snapshot_fingerprint="fp-1",
        data=_data(candidate_profile_version=4, match_algorithm_version="v1"),
    )
    response = to_tailored_cv_draft(record)
    assert response.candidate_profile_version == 4
    assert response.match_algorithm_version == "v1"


def test_top_level_fact_provenance_survives_json_round_trip():
    """M-01 section 11: provenance must not be a transient
    compute_cv_draft-only artifact — it must survive
    draft_json serialization -> DB -> deserialization intact."""
    db = _db()
    header = CVHeader(
        first_name=CVTopLevelFact(
            value="Anna", source_id=1, source_field="first_name", profile_version=7
        )
    )
    summary = CVTopLevelFact(
        value="Backend engineer.",
        source_id=1,
        source_field="professional_summary",
        profile_version=7,
    )
    record, _ = create_draft(
        db,
        job_id=1,
        job_snapshot_fingerprint="fp-1",
        data=_data(candidate_profile_version=7, header=header, professional_summary=summary),
    )

    response = to_tailored_cv_draft(record)
    assert response.header.first_name.value == "Anna"
    assert response.header.first_name.source_entity == "candidate_profile"
    assert response.header.first_name.source_id == 1
    assert response.header.first_name.source_field == "first_name"
    assert response.header.first_name.profile_version == 7
    assert response.professional_summary.value == "Backend engineer."
    assert response.professional_summary.source_field == "professional_summary"
    assert response.professional_summary.profile_version == 7


# --- concurrency (real independent sessions) --------------------------------


def test_concurrent_create_for_identical_cache_identity_does_not_duplicate(tmp_path):
    """Two concurrent POSTs computing the identical (match_id,
    cv_adapter_version) cache identity must not create two rows — the DB
    UNIQUE constraint plus IntegrityError/reload-winner handling must
    converge on exactly one persisted row, both callers receiving the
    canonical draft, no raw IntegrityError escaping.
    """
    factory = _file_session_factory(tmp_path, "concurrent_cv_draft.db")
    db_a = factory()
    db_b = factory()

    data = _data(match_id=1, cv_adapter_version="v1")
    record_a, created_a = create_draft(db_a, job_id=1, job_snapshot_fingerprint="fp-1", data=data)
    record_b, created_b = create_draft(db_b, job_id=1, job_snapshot_fingerprint="fp-1", data=data)

    assert created_a is True
    assert created_b is False
    assert record_a.id == record_b.id

    response_a = to_tailored_cv_draft(record_a)
    response_b = to_tailored_cv_draft(record_b)
    assert response_a.id == response_b.id

    final = factory()
    total = final.scalar(select(func.count()).select_from(CandidateCVDraftRecord))
    assert total == 1
    db_a.close()
    db_b.close()
    final.close()


def test_privacy_safe_draft_json_contains_no_extra_fields():
    db = _db()
    record, _ = create_draft(db, job_id=1, job_snapshot_fingerprint="fp-1", data=_data())
    import json

    payload = json.loads(record.draft_json)
    assert set(payload.keys()) == set(TailoredCVDraftData.model_fields.keys())
