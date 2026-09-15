"""Stage 11E regression: an APPLY/MAYBE recommendation must be supported
by at least 2 DISTINCT normalized structured skill signals, not merely
that many CATEGORY entries -- see app.agents.evidence_cardinality_classifier
and app.services.collector_runner._score_for_posting_type. Positioned
after Stage 11A/11B (their own exclusions always fire first) and before
Stage 11C (a genuinely relevant, thin-evidence posting can still be
rescued). Stage 10 posting_type, Stage 11A seniority, Stage 11B role
relevance, Stage 11C sparse-evidence rescue, and the ordinary JobScorer
math are all untouched and must keep working unchanged.
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

GENERIC_RICH_DESCRIPTION = (
    "We build great software using industry-standard tools and agile "
    "practices in a collaborative, supportive team environment. " * 30
)

RICH_TECH_DESCRIPTION = (
    "We build REST APIs with Python, FastAPI and SQLAlchemy against "
    "PostgreSQL. Git-based workflow, automated tests with Pytest, "
    "containerized with Docker. " * 20
)

FULL_SKILL_SET = ["python", "fastapi", "sqlalchemy", "postgresql", "git", "pytest", "docker"]

SOFTWARE_DEV_TARGET_ROLES = [
    "Junior Python Developer",
    "Junior Backend Developer",
]


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
        "description": GENERIC_RICH_DESCRIPTION,
        "posting_type": "ARBEIT",
        "must_have_skills": [],
        "nice_to_have_skills": [],
    }
    data.update(overrides)
    return Job(**data)


# --- A/B/C: single-must-only, UNKNOWN-relevance titles -> SKIP -------------


def test_a_solution_architect_style_single_must_is_downgraded():
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Solution Architect – Automation (w/m/d)",
        company="DB Zeitarbeit GmbH",
        must_have_skills=["Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_b_consultant_data_center_style_single_must_is_downgraded():
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Consultant (m/w/d) Data Center",
        company="univativ GmbH",
        must_have_skills=["Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_c_referent_zensus_style_single_must_is_downgraded():
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="eine Referentin / einen Referenten (w/m/d) im Bereich Zensus",
        company="Hessisches Statistisches Landesamt",
        must_have_skills=["Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


# --- D: Data Consultant Life-Science regression (critical case) ------------


def test_d_data_consultant_life_science_legacy_fallback_duplicate_is_downgraded():
    # must_have_skills=[] (raw) but skills=["Python"] (legacy) triggers
    # JobScorer's `must = {...} or legacy` fallback, resolving must
    # evidence to {"python"} -- the SAME skill already present in
    # nice_to_have_skills. Baseline (pre-11E) result was MAYBE at a high
    # score (double credit from one underlying signal). Stage 11E must
    # see this as exactly 1 unique signal and downgrade to SKIP. Stage
    # 11C must not rescue it: title relevance is UNKNOWN, not RELEVANT.
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Data Consultant (m/w/d) für die Life-Science-Industrie in Frankfurt",
        company="DAQUMA GmbH",
        skills=["Python"],
        must_have_skills=[],
        nice_to_have_skills=["Python"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


# --- E/F/G: known-good jobs, 2+ distinct signals -> unaffected -------------


def test_e_python_entwickler_two_distinct_signals_stays_maybe():
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Python Entwickler (m/w/d)",
        company="ncsolution GmbH",
        must_have_skills=["Python"],
        nice_to_have_skills=["SQL"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"
    assert result.score > 0


def test_f_ai_engineer_three_or_more_signals_stays_apply():
    db = _db()
    profile = _profile(db, ["python", "git", "rest"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="E362/B - AI Engineer / KI-Entwickler (m/w/d)",
        company="Finanz Informatik GmbH & Co. KG",
        description=(
            "AI engineering role using Python, Git, and REST APIs in a modern "
            "banking IT environment. " * 20
        ),
        must_have_skills=["Python", "Git", "REST"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"


def test_g_backend_developer_multiple_signals_stays_maybe():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Backend Developer (m/w/div.)",
        company="ALD Vacuum Technologies GmbH",
        description=RICH_TECH_DESCRIPTION,
        must_have_skills=["CI/CD", "Docker", "Git", "Kubernetes", "PostgreSQL", "Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"
    assert result.score > 0


# --- H: Stage 11C rescue behavior fully preserved ---------------------------


def test_h_stage_11c_zero_evidence_relevant_developer_still_rescued():
    # must=[] nice=[] -- Stage 11E never even applies (raw JobScorer
    # recommendation is already SKIP, not APPLY/MAYBE, since must/nice
    # both default to the neutral 0.5), so Stage 11C's existing rescue
    # logic runs completely undisturbed.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="JUNIOR SOFTWARE DEVELOPER / ENTWICKLER (M/W/D)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"


def test_h_all_four_stage_11c_rescued_titles_remain_maybe():
    titles = [
        "IT Operations AI Engineer (m/w/d)",
        "Softwareentwickler SPS (m/w/d)",
        "JUNIOR SOFTWARE DEVELOPER / ENTWICKLER (M/W/D)",
        "Software Engineer (m/w/d) – UAV / Real-Time Systems",
    ]
    for title in titles:
        db = _db()
        profile = _profile(db, FULL_SKILL_SET)
        _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
        job = _job(title=title)

        _record, result, _created = score_and_persist(db, profile, job)

        assert result.recommendation == "MAYBE", (
            f"{title} expected MAYBE, got {result.recommendation}"
        )


# --- I: real gap (already SKIP at JobScorer time) unaffected ---------------


def test_i_junior_cyber_security_developer_unaffected():
    db = _db()
    profile = _profile(db, ["python", "git"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Junior Cyber Security Developer / Anwendungsentwickler (m/w/d)",
        company="BWI GmbH",
        must_have_skills=["Python", "Git", "TypeScript"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


# --- J/K: Stage 11A/11B exclusions unaffected -------------------------------


def test_j_stage_11a_senior_exclusion_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Senior Entwickler Python (m/w/d)",
        description=RICH_TECH_DESCRIPTION,
        must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
        nice_to_have_skills=["Docker", "Pytest"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")
    assert result.score == 0


def test_k_stage_11b_irrelevant_exclusion_unaffected():
    db = _db()
    profile = _profile(db, ["python", "sap"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Personalcontroller (m/w/d)",
        company="Bankpower GmbH",
        description=("HR controlling role, Python scripting and SAP knowledge a plus. " * 15),
        must_have_skills=["Python", "SAP"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")
    assert result.score == 0


# --- Regression guards -------------------------------------------------------


def test_posting_type_exclusion_still_works_unchanged():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Programmierung mit Python",
        company="alfatraining Bildungszentrum GmbH",
        posting_type="SELBSTAENDIGKEIT",
        must_have_skills=["Python"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"
    assert result.score == 0


def test_needs_enrichment_job_is_not_touched():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Junior Python Developer", description="")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "NEEDS_ENRICHMENT"


def test_score_is_unchanged_when_downgraded():
    # Explicit check: Stage 11E only changes the recommendation label,
    # never the numeric score.
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Solution Architect – Automation (w/m/d)",
        must_have_skills=["Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"
    assert result.score > 0  # NOT zeroed -- Stage 11E leaves score AS-IS
