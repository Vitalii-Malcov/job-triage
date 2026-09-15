"""Stage 11A regression: an explicit senior/lead-level job TITLE must not
reach MAYBE/APPLY for a candidate who has explicitly (and unambiguously)
targeted junior roles -- see app.agents.seniority_classifier and
app.services.collector_runner._score_for_posting_type. posting_type
classification (Stage 10) is untouched and must keep working unchanged.
"""

import json

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.models import UserProfile
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.models.job import Job
from app.services.collector_runner import score_and_persist

RICH_TECH_DESCRIPTION = (
    "We build REST APIs with Python, FastAPI and SQLAlchemy against "
    "PostgreSQL. Git-based workflow, automated tests with Pytest, "
    "containerized with Docker. " * 6
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _profile(db: Session, skills: list[str]) -> UserProfile:
    profile = UserProfile(name="default", skills_json=json.dumps(skills))
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _set_target_roles(db: Session, target_roles: list[str]) -> None:
    patch = CandidateProfilePatchRequest(
        expected_profile_version=1,
        target_roles=target_roles,
    )
    apply_candidate_profile_patch(db, patch)


def _job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "Python Backend Developer",
        "company": "Example GmbH",
        "url": "https://example.com/job/1",
        "description": RICH_TECH_DESCRIPTION,
        "posting_type": "ARBEIT",
        "must_have_skills": ["Python", "FastAPI", "SQLAlchemy"],
        "nice_to_have_skills": ["Docker", "Pytest"],
    }
    data.update(overrides)
    return Job(**data)


# S11A-002: candidate target seniority is only JUNIOR if EVERY target role
# explicitly says "junior" -- both roles below do, so this list is a valid
# JUNIOR-only target.
JUNIOR_TARGET_ROLES = [
    "Junior Python Developer",
    "Junior Backend Developer",
]

FULL_SKILL_SET = ["python", "fastapi", "sqlalchemy", "postgresql", "git", "pytest", "docker"]


def test_senior_entwickler_python_cannot_maybe_or_apply_for_junior_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(title="Senior Entwickler Python (m/w/d)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")
    assert result.recommendation == "SKIP"
    assert result.score == 0


def test_senior_referent_ratingvalidierung_cannot_maybe_or_apply_for_junior_candidate():
    db = _db()
    profile = _profile(db, ["python", "git"])
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(
        title="Senior Referent Ratingvalidierung / Risikocontrolling m/w/d",
        company="Some Bank AG",
        description="Some banking risk work with Python and Git scripting. " * 10,
        must_have_skills=["Python", "Git"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_senior_quant_engineer_cannot_maybe_or_apply_for_junior_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(
        title="Senior Quant Engineer Investment Platform (f/m/d)",
        company="Some Investment GmbH",
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_lead_python_developer_cannot_maybe_or_apply_for_junior_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(title="Lead Python Developer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_principal_backend_engineer_cannot_maybe_or_apply_for_junior_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(title="Principal Backend Engineer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_junior_python_developer_title_is_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(title="Junior Python Developer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_python_entwickler_title_is_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(title="Python Entwickler")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_backend_developer_title_is_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(title="Backend Developer (m/w/div.)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_normal_title_with_no_seniority_marker_is_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(title="Python Backend Developer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_senior_job_not_rejected_when_candidate_target_seniority_unknown_no_target_roles():
    # No target_roles set at all -- candidate target seniority cannot be
    # determined, so the seniority rule must never fire.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    job = _job(title="Senior Entwickler Python (m/w/d)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_senior_job_not_rejected_when_candidate_target_seniority_ambiguous():
    # target_roles mixes a junior and a senior/lead target -- ambiguous,
    # so this must not be treated as a JUNIOR-only target.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, ["Junior Python Developer", "Lead Python Developer"])
    job = _job(title="Senior Entwickler Python (m/w/d)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_posting_type_exclusion_still_works_unchanged():
    # Stage 10's posting_type gate must be completely unaffected by this
    # change -- a course listing stays excluded regardless of title.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(
        title="Programmierung mit Python",
        company="alfatraining Bildungszentrum GmbH",
        posting_type="SELBSTAENDIGKEIT",
        must_have_skills=["Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"
    assert result.score == 0


def test_senior_job_that_would_have_skipped_anyway_is_unaffected():
    # A senior-titled job that doesn't even clear ordinary skill matching
    # must not be turned into some OTHER outcome by the seniority gate --
    # the gate only ever fires on an already-APPLY/MAYBE result, so a job
    # that scores SKIP/NEEDS_ENRICHMENT on its own merits stays exactly
    # that (never MAYBE/APPLY either way).
    db = _db()
    profile = _profile(db, ["cobol"])
    _set_target_roles(db, JUNIOR_TARGET_ROLES)
    job = _job(
        title="Senior Java Architect",
        must_have_skills=["Java", "Kubernetes", "AWS"],
        nice_to_have_skills=[],
        description="Enterprise Java architecture role. " * 10,
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


# --- S11A-002: a mixed junior + unlabeled target list is now ambiguous -----


def test_mixed_junior_and_unlabeled_target_roles_no_longer_excludes_senior_jobs():
    # S11A-002: a target_roles list that mixes an explicit junior role
    # with an unlabeled one ("Python Backend Developer" says nothing
    # about seniority) is no longer treated as a JUNIOR-only target -- the
    # ambiguity must fail OPEN (gate never fires), not fail toward
    # excluding jobs.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(
        db,
        ["Junior Python Developer", "Junior Backend Developer", "Python Backend Developer"],
    )
    job = _job(title="Senior Entwickler Python (m/w/d)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


# --- S11A-003: seniority lookup must never create a CandidateProfile ------


def test_seniority_lookup_does_not_create_candidate_profile_when_none_exists():
    from app.db.candidate_profile_repository import count_candidate_profiles

    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    assert count_candidate_profiles(db) == 0

    # No target_roles/CandidateProfile ever set -- a normal job should
    # still score APPLY/MAYBE on skill match alone (candidate target
    # seniority resolves to UNKNOWN, so the gate never fires), and doing
    # so must NOT have created the singleton CandidateProfile row as a
    # side effect of the seniority lookup.
    job = _job(title="Python Backend Developer")
    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert count_candidate_profiles(db) == 0


def test_seniority_lookup_uses_pure_get_not_get_or_create(monkeypatch):
    # S11A-003: directly proves the seniority path calls the PURE
    # get_candidate_profile lookup, never get_or_create_candidate_profile
    # -- stronger than an outcome-only assertion, since it fails loudly if
    # a future change reintroduces the create-on-read side effect.
    import app.services.collector_runner as collector_runner_module

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError(
            "get_or_create_candidate_profile must not be called by the seniority lookup"
        )

    monkeypatch.setattr(
        collector_runner_module, "get_or_create_candidate_profile", _must_not_be_called
    )

    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    job = _job(title="Python Backend Developer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"


# --- S11A-004: the seniority lookup must never autoflush an unrelated,
# still-pending ORM object on this SAME Session ------------------------------


def test_candidate_target_seniority_lookup_never_autoflushes_no_profile():
    # Codex reproduced: unrelated pending ORM state -> _candidate_target_
    # seniority() -> one autoflush occurred (Session.get() autoflushes by
    # default). Uses a PLAIN Session (autoflush=True, SQLAlchemy's
    # default) -- unlike production's SessionLocal (autoflush=False) --
    # specifically so this test exercises the exact risk the fix guards
    # against, not the app's own separately-safe default configuration.
    import app.services.collector_runner as collector_runner_module
    from app.db.candidate_profile_repository import count_candidate_profiles

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)

    flush_count = {"n": 0}
    event.listen(
        db, "before_flush", lambda *_a, **_k: flush_count.__setitem__("n", flush_count["n"] + 1)
    )

    unrelated = UserProfile(name="unrelated-pending-1", skills_json="[]")
    db.add(unrelated)

    result = collector_runner_module._candidate_target_seniority(db)

    assert result == "UNKNOWN"
    assert flush_count["n"] == 0
    assert unrelated.id is None
    assert unrelated in db.new
    assert count_candidate_profiles(db) == 0


def test_candidate_target_seniority_lookup_never_autoflushes_with_existing_profile():
    # Same probe, but with a REAL CandidateProfile already persisted --
    # proves the fix covers to_candidate_profile_response's own
    # relationship access (skills/experiences/etc.), not just the
    # Session.get() call, and that the correct JUNIOR result is still
    # derived from the existing target_roles despite the unrelated
    # pending object never being flushed.
    import app.services.collector_runner as collector_runner_module

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    _set_target_roles(db, ["Junior Python Developer", "Junior Backend Developer"])

    flush_count = {"n": 0}
    event.listen(
        db, "before_flush", lambda *_a, **_k: flush_count.__setitem__("n", flush_count["n"] + 1)
    )

    unrelated = UserProfile(name="unrelated-pending-2", skills_json="[]")
    db.add(unrelated)

    result = collector_runner_module._candidate_target_seniority(db)

    assert result == "JUNIOR"
    assert flush_count["n"] == 0
    assert unrelated.id is None
    assert unrelated in db.new
