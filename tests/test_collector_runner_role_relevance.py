"""Stage 11B regression: a confidently IRRELEVANT-titled job (a role
family clearly outside software development) must not reach MAYBE/APPLY
for a candidate whose target roles unanimously name a software-
development role family -- see app.agents.role_relevance_classifier and
app.services.collector_runner._score_for_posting_type. Stage 11A's
seniority logic, posting_type classification (Stage 10), and the
ordinary JobScorer math are all untouched and must keep working
unchanged.
"""

import json

from sqlalchemy import create_engine
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


# Candidate targets a software-development role family -- unanimous, so
# derive_candidate_target_domain resolves to SOFTWARE_DEVELOPMENT.
SOFTWARE_DEV_TARGET_ROLES = [
    "Junior Python Developer",
    "Junior Backend Developer",
]

FULL_SKILL_SET = ["python", "fastapi", "sqlalchemy", "postgresql", "git", "pytest", "docker"]


def test_personalcontroller_cannot_maybe_or_apply_for_software_dev_candidate():
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Personalcontroller (m/w/d)",
        company="Some Bankpower GmbH",
        description="HR controlling role, Python scripting knowledge a plus. " * 10,
        must_have_skills=["Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")
    assert result.recommendation == "SKIP"
    assert result.score == 0


def test_mongodb_administrator_cannot_maybe_or_apply_for_software_dev_candidate():
    db = _db()
    profile = _profile(db, ["mongodb"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="MongoDB Administrator (m/w/d)",
        company="ANG GmbH",
        description="Manage and administer MongoDB database clusters. " * 10,
        must_have_skills=["MongoDB"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_systemadministrator_cannot_maybe_or_apply_for_software_dev_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Systemadministrator (m/w/d)",
        company="Some IT GmbH",
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_qgis_expert_cannot_maybe_or_apply_for_software_dev_candidate():
    db = _db()
    profile = _profile(db, ["postgresql"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="QGIS Expertin / Experte",
        company="AGIS GmbH",
        description="GIS data management with QGIS and PostgreSQL. " * 10,
        must_have_skills=["PostgreSQL"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_presales_consultant_cannot_maybe_or_apply_for_software_dev_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Presales Consultant (m/w/d) - Datacenter",
        company="Pan Dacom Networking AG",
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_berater_projektmanagement_cannot_maybe_or_apply_for_software_dev_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Berater im Projektmanagement im öffentlichen Sektor (w/m/d)",
        company="EY Consulting GmbH",
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_ingenieur_elektrotechnik_cannot_maybe_or_apply_for_software_dev_candidate():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Ingenieur Elektrotechnik (m/w/d) Automatisierung",
        company="Brueggen Engineering GmbH",
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_junior_python_developer_title_is_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Junior Python Developer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_python_entwickler_title_is_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Python Entwickler")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_backend_developer_title_is_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Backend Developer (m/w/div.)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_ai_engineer_ki_entwickler_title_is_unaffected():
    # The real Stage 11 posting (Finanz Informatik) -- must not be
    # rejected merely because it isn't literally "Python Developer".
    db = _db()
    profile = _profile(db, ["python", "git"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="E362/B - AI Engineer / KI-Entwickler (m/w/d)",
        company="Finanz Informatik GmbH & Co. KG",
        description="AI engineering role using Python and Git. " * 15,
        must_have_skills=["Python", "Git"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_role_relevance_gate_not_rejected_when_candidate_target_domain_unknown():
    # No target_roles set at all -- candidate target domain cannot be
    # determined, so the relevance rule must never fire.
    db = _db()
    profile = _profile(db, ["python", "sap"])
    job = _job(
        title="Personalcontroller (m/w/d)",
        description="HR controlling role, Python scripting and SAP knowledge a plus. " * 15,
        must_have_skills=["Python", "SAP"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_role_relevance_gate_not_rejected_when_candidate_target_domain_ambiguous():
    # target_roles mixes a relevant and an ambiguous target -- must not
    # be treated as a unanimous software-development target.
    db = _db()
    profile = _profile(db, ["python", "sap"])
    _set_target_roles(db, ["Junior Python Developer", "Data Engineer Trainee"])
    job = _job(
        title="Personalcontroller (m/w/d)",
        description="HR controlling role, Python scripting and SAP knowledge a plus. " * 15,
        must_have_skills=["Python", "SAP"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"
    assert result.score > 0


def test_posting_type_exclusion_still_works_unchanged():
    # Stage 10's posting_type gate must be completely unaffected by this
    # change -- a course listing stays excluded regardless of title.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
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


def test_seniority_exclusion_still_works_unchanged():
    # Stage 11A's own gate must be completely unaffected -- a senior
    # RELEVANT-titled job still gets excluded by seniority, independent
    # of role relevance.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Senior Entwickler Python (m/w/d)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_irrelevant_job_that_would_have_skipped_anyway_is_unaffected():
    # An irrelevant-titled job that doesn't even clear ordinary skill
    # matching must not be turned into some OTHER outcome by the relevance
    # gate -- the gate only ever fires on an already-APPLY/MAYBE result.
    db = _db()
    profile = _profile(db, ["cobol"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Personalcontroller (m/w/d)",
        must_have_skills=["Excel", "SAP"],
        nice_to_have_skills=[],
        description="HR controlling role using Excel and SAP. " * 10,
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")
