"""Stage 10 finding regression: score_and_persist must keep non-employment
postings (training-provider courses, and apprenticeships/internships the
candidate hasn't opted into) out of the normal APPLY pipeline, while
leaving genuine job postings completely unaffected -- see
app.agents.posting_classifier and app.services.collector_runner.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.models import UserProfile
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.models.job import Job
from app.services.collector_runner import score_and_persist


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _profile(db: Session, skills: list[str]) -> UserProfile:
    import json

    profile = UserProfile(name="default", skills_json=json.dumps(skills))
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "Programmierung mit Python",
        "company": "alfatraining Bildungszentrum GmbH",
        "url": "https://example.com/job/1",
        "description": "Python course description. " * 30,
        "must_have_skills": ["Python"],
        "nice_to_have_skills": [],
    }
    data.update(overrides)
    return Job(**data)


def test_selbstaendigkeit_course_listing_is_skipped_before_normal_scoring():
    db = _db()
    profile = _profile(db, ["python", "fastapi"])
    job = _job(posting_type="SELBSTAENDIGKEIT")

    record, result, created = score_and_persist(db, profile, job)

    assert created is True
    assert result.recommendation == "SKIP"
    assert result.score == 0
    assert record.recommendation == "SKIP"


def test_duales_studium_excluded_by_default_no_candidate_preference():
    db = _db()
    profile = _profile(db, ["python"])
    job = _job(
        title="Duales Studium Informatik mit Ausrichtung Künstliche Intelligenz",
        company="ClaraNET GmbH",
        posting_type="AUSBILDUNG",
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"
    assert result.score == 0


def test_ausbildung_scores_normally_when_candidate_preference_permits_it():
    db = _db()
    profile = _profile(db, ["python", "fastapi", "sqlalchemy"])
    patch = CandidateProfilePatchRequest(
        expected_profile_version=1,
        job_preferences={"employment_types": ["APPRENTICESHIP"]},
    )
    apply_candidate_profile_patch(db, patch)

    job = _job(
        title="Ausbildung Fachinformatiker (m/w/d)",
        company="Werth Messtechnik GmbH",
        posting_type="AUSBILDUNG",
        must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    # Preference permits it -- normal scoring ran (not force-SKIP), and
    # with 3/3 must-have skills matched this reaches APPLY.
    assert result.recommendation == "APPLY"


def test_legitimate_arbeit_job_scores_normally_unaffected():
    db = _db()
    profile = _profile(db, ["python", "fastapi", "sqlalchemy", "docker", "pytest"])
    job = _job(
        title="Junior Python Backend Developer",
        company="Example IT GmbH",
        posting_type="ARBEIT",
        description=("We build REST APIs with Python, FastAPI and SQLAlchemy. " * 15),
        must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
        nice_to_have_skills=["Docker", "Pytest"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"


def test_reposting_without_posting_type_cannot_undo_a_prior_exclusion():
    # Stage 10 follow-up finding: posting_type is transient scoring input,
    # not something every caller necessarily resupplies (e.g. a manual
    # POST /jobs/score payload for the SAME fingerprint has no reason to
    # know this derived field exists). A re-score that omits it must reuse
    # the ALREADY-PERSISTED posting_type from the first upsert -- an
    # excluded course listing must never silently flip to APPLY just
    # because a later caller didn't resupply the classification signal.
    db = _db()
    profile = _profile(db, ["python", "fastapi", "flask", "sqlalchemy", "postgresql", "docker"])
    original = _job(posting_type="SELBSTAENDIGKEIT")

    record1, result1, created1 = score_and_persist(db, profile, original)
    assert created1 is True
    assert result1.recommendation == "SKIP"
    assert record1.posting_type == "SELBSTAENDIGKEIT"

    # Same fingerprint (source/company/title/url unchanged), but this
    # second submission omits posting_type entirely -- as a real manual
    # POST /jobs/score payload would, since it's a derived/internal field.
    resubmitted = _job(posting_type=None)
    record2, result2, created2 = score_and_persist(db, profile, resubmitted)

    assert created2 is False
    assert record2.id == record1.id
    assert result2.recommendation == "SKIP"
    assert record2.posting_type == "SELBSTAENDIGKEIT"  # not erased to None


def test_reposting_with_fresh_posting_type_can_update_the_stored_value():
    # The preserve-on-omit rule only applies when the NEW submission is
    # silent (None) -- a caller that DOES resupply a (possibly different)
    # posting_type, e.g. a genuine re-fetch from the source, must still be
    # able to update the stored classification going forward.
    db = _db()
    profile = _profile(db, ["python", "fastapi", "sqlalchemy"])
    original = _job(posting_type="SELBSTAENDIGKEIT")
    score_and_persist(db, profile, original)

    refetched = _job(posting_type="ARBEIT", must_have_skills=["Python", "FastAPI", "SQLAlchemy"])
    record, result, created = score_and_persist(db, profile, refetched)

    assert created is False
    assert record.posting_type == "ARBEIT"
    assert result.recommendation == "APPLY"


def test_real_job_at_company_with_educational_name_is_not_excluded():
    db = _db()
    profile = _profile(db, ["python", "fastapi", "sqlalchemy", "docker", "pytest"])
    job = _job(
        title="Python Backend Developer (m/w/d)",
        company="alfatraining Bildungszentrum GmbH",  # same company, but ARBEIT
        posting_type="ARBEIT",
        description=("We build REST APIs with Python, FastAPI and SQLAlchemy. " * 15),
        must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
        nice_to_have_skills=["Docker", "Pytest"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
