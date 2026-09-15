"""Stage 10 finding regression: score_and_persist must keep non-employment
postings (training-provider courses, and apprenticeships/internships the
candidate hasn't opted into) out of the normal APPLY pipeline, while
leaving genuine job postings completely unaffected -- see
app.agents.posting_classifier and app.services.collector_runner.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.collector_runner as collector_runner_module
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


def test_concurrent_typed_and_untyped_upserts_converge_on_a_consistent_recommendation(
    monkeypatch,
):
    """S10-001 (Codex Stage 10 review, BLOCKING). Reproduces the exact
    interleaving that could leave a persisted JobRecord with
    posting_type=SELBSTAENDIGKEIT (excluded, no FREELANCE preference) but
    recommendation=APPLY (as if it had been scored while still believed
    untyped):

    1. Call A's OWN `existing` read (score_and_persist's first line) sees
       nothing persisted yet for this fingerprint -- job_a carries no
       posting_type, so it classifies as normal/untyped and scores APPLY.
    2. Before A's own write, call B (SAME fingerprint, posting_type=
       SELBSTAENDIGKEIT) runs to completion and persists the correctly
       excluded SKIP row.
    3. A's write finally happens: `upsert_job`'s OWN separate internal
       read now finds B's row and takes the "already exists" update path,
       applying A's STALE precomputed (job=untyped, score=APPLY) onto it.

    Without the S10-001 fix, step 3 preserves B's posting_type (via
    `_apply_job_update_fields`'s preserve-on-omit rule) but blindly
    overwrites `recommendation`/`score` with A's stale values -- exactly
    the reported inconsistent pairing. The fix detects that `record`'s
    real posting_type (SELBSTAENDIGKEIT) doesn't match what `result` was
    computed against (None) and reclassifies before returning.
    """
    db = _db()
    profile = _profile(db, ["python", "fastapi", "sqlalchemy"])

    job_a = _job(posting_type=None, must_have_skills=["Python", "FastAPI", "SQLAlchemy"])
    job_b = _job(posting_type="SELBSTAENDIGKEIT")

    real_get_job_by_fingerprint = collector_runner_module.get_job_by_fingerprint
    call_count = {"n": 0}

    def interleaving_get_job_by_fingerprint(db_, job_):
        call_count["n"] += 1
        result = real_get_job_by_fingerprint(db_, job_)
        if call_count["n"] == 1:
            # Simulate "meanwhile, call B's ENTIRE score_and_persist runs
            # to completion" -- temporarily restore the real lookup so
            # B's own internal reads aren't caught by this same hook.
            monkeypatch.setattr(
                collector_runner_module, "get_job_by_fingerprint", real_get_job_by_fingerprint
            )
            score_and_persist(db, profile, job_b)
            monkeypatch.setattr(
                collector_runner_module,
                "get_job_by_fingerprint",
                interleaving_get_job_by_fingerprint,
            )
        return result

    monkeypatch.setattr(
        collector_runner_module, "get_job_by_fingerprint", interleaving_get_job_by_fingerprint
    )

    record, result, _created = score_and_persist(db, profile, job_a)

    # The final persisted state must be internally consistent regardless
    # of interleaving: an excluded posting (SELBSTAENDIGKEIT, no FREELANCE
    # preference) must never end up recommendation=APPLY.
    assert record.posting_type == "SELBSTAENDIGKEIT"
    assert record.recommendation == "SKIP"
    assert record.score == 0
    assert result.recommendation == "SKIP"


def test_posting_type_rejects_values_longer_than_the_db_column():
    # S10-003: Job.posting_type's max_length must match
    # JobRecord.posting_type's VARCHAR(64) so an overlong value fails
    # fast at the Pydantic boundary rather than at INSERT time.
    with pytest.raises(ValidationError):
        Job(
            source="bundesagentur",
            title="Some Role",
            company="Some GmbH",
            url="https://example.com/job/1",
            posting_type="X" * 65,
        )
    # Exactly 64 chars is still accepted.
    job = Job(
        source="bundesagentur",
        title="Some Role",
        company="Some GmbH",
        url="https://example.com/job/1",
        posting_type="X" * 64,
    )
    assert job.posting_type == "X" * 64


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
