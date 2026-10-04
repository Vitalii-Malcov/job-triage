"""Stage 9B: the shared, strengthened Bewerbung "is current" identity
(`app.services.bewerbung_reuse`) used by BOTH Stage 8C automation and the
Stage 9B Telegram preparation path."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.automation_shortlist as automation_shortlist
import app.services.bewerbung_reuse as bewerbung_reuse
from app.agents.bewerbung_generator import BEWERBUNG_GENERATOR_VERSION
from app.db.base import Base
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint
from app.db.models import BewerbungDraftRecord, CandidateCVDraftRecord
from app.db.repositories import upsert_job
from app.models.job import Job, JobScore
from app.services.bewerbung_reuse import bewerbung_draft_is_current, persisted_job_context


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reuse.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _job(db, *, title="Python Developer", company="Acme GmbH"):
    record, _ = upsert_job(
        db,
        Job(source="test", title=title, company=company, url="https://example.com/jobs/1"),
        JobScore(score=90, recommendation="APPLY"),
    )
    return record


def _cv(db, job, **overrides) -> CandidateCVDraftRecord:
    data = dict(
        job_id=job.id,
        match_id=11,
        candidate_profile_version=3,
        job_snapshot_fingerprint=compute_job_snapshot_fingerprint(job),
        match_algorithm_version="m1",
        cv_adapter_version="a1",
        status="DRAFT",
        draft_json="{}",
    )
    data.update(overrides)
    record = CandidateCVDraftRecord(**data)
    db.add(record)
    db.commit()
    return record


def _letter(db, job, cv, *, context="default", **overrides) -> BewerbungDraftRecord:
    payload = {"subject": "Bewerbung"}
    if context == "default":
        payload["job_context"] = {"title": job.title, "company": job.company}
    elif context is not None:
        payload["job_context"] = context
    data = dict(
        job_id=job.id,
        cv_draft_id=cv.id,
        match_id=cv.match_id,
        candidate_profile_version=cv.candidate_profile_version,
        job_snapshot_fingerprint=cv.job_snapshot_fingerprint,
        match_algorithm_version=cv.match_algorithm_version,
        cv_adapter_version=cv.cv_adapter_version,
        bewerbung_generator_version=BEWERBUNG_GENERATOR_VERSION,
        provider="deterministic",
        status="DRAFT",
        draft_json=json.dumps(payload),
    )
    data.update(overrides)
    record = BewerbungDraftRecord(**data)
    db.add(record)
    db.commit()
    return record


def test_matching_draft_is_current(db):
    job = _job(db)
    cv = _cv(db, job)
    assert bewerbung_draft_is_current(_letter(db, job, cv), cv, job) is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("match_id", 99),
        ("candidate_profile_version", 4),
        ("job_snapshot_fingerprint", "f" * 64),
        ("match_algorithm_version", "m0"),
        ("cv_adapter_version", "a0"),
        ("bewerbung_generator_version", "v1"),
        ("provider", "some-llm"),
    ],
)
def test_any_pin_mismatch_is_not_current(db, field, value):
    job = _job(db)
    cv = _cv(db, job)
    letter = _letter(db, job, cv, **{field: value})
    assert bewerbung_draft_is_current(letter, cv, job) is False


def test_draft_for_another_cv_draft_is_not_current(db):
    job = _job(db)
    cv = _cv(db, job)
    other_cv = _cv(db, job, match_id=12)
    assert bewerbung_draft_is_current(_letter(db, job, other_cv), cv, job) is False


def test_company_only_change_invalidates(db):
    """The matching fingerprint excludes the company -- only the persisted
    display context catches an opening that names the old company."""
    job = _job(db)
    cv = _cv(db, job)
    letter = _letter(db, job, cv)
    fingerprint_before = compute_job_snapshot_fingerprint(job)

    job.company = "Acme Holding GmbH"
    db.commit()

    assert compute_job_snapshot_fingerprint(job) == fingerprint_before
    assert bewerbung_draft_is_current(letter, cv, job) is False


def test_title_case_change_invalidates(db):
    """The fingerprint casefolds the title; the letter renders it verbatim."""
    job = _job(db, title="python developer")
    cv = _cv(db, job)
    letter = _letter(db, job, cv)
    fingerprint_before = compute_job_snapshot_fingerprint(job)

    job.title = "Python Developer"
    db.commit()

    assert compute_job_snapshot_fingerprint(job) == fingerprint_before
    assert bewerbung_draft_is_current(letter, cv, job) is False


def test_job_content_change_invalidates_through_cv_fingerprint(db):
    job = _job(db)
    cv = _cv(db, job)
    letter = _letter(db, job, cv)

    job.description = "Neue Anforderungen: Rust."
    db.commit()

    assert bewerbung_draft_is_current(letter, cv, job) is False


@pytest.mark.parametrize(
    "context",
    [None, "not-an-object", {"title": "Python Developer"}, {"title": 1, "company": "Acme GmbH"}],
)
def test_legacy_or_malformed_context_is_never_current(db, context):
    job = _job(db)
    cv = _cv(db, job)
    letter = _letter(db, job, cv, context=context)
    assert persisted_job_context(letter) is None
    assert bewerbung_draft_is_current(letter, cv, job) is False


def test_unparseable_json_is_never_current(db):
    job = _job(db)
    cv = _cv(db, job)
    letter = _letter(db, job, cv, draft_json="{not json")
    assert bewerbung_draft_is_current(letter, cv, job) is False


def test_draft_or_cv_of_another_job_is_not_current(db):
    job = _job(db)
    cv = _cv(db, job)
    letter = _letter(db, job, cv, job_id=job.id + 1000)
    assert bewerbung_draft_is_current(letter, cv, job) is False
    assert bewerbung_draft_is_current(None, cv, job) is False
    assert bewerbung_draft_is_current(_letter(db, job, cv), None, job) is False


def test_stage_8c_uses_the_shared_helper_not_a_copy():
    assert automation_shortlist.bewerbung_draft_is_current is (
        bewerbung_reuse.bewerbung_draft_is_current
    )
    assert not hasattr(automation_shortlist, "_bewerbung_is_current")
