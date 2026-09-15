"""Stage 11C regression: a plausibly software-development-titled job that
scores an ordinary SKIP purely because structured skill extraction was
thin (SPARSE, per app.agents.evidence_quality_classifier) must be raised
to MAYBE, never higher, and never for a job that is IRRELEVANT (Stage
11B), a senior/junior seniority mismatch (Stage 11A), or one whose
extracted evidence shows a REAL, specific skill gap rather than an
absence of evidence. Stage 10 posting_type classification, Stage 11A
seniority, Stage 11B role relevance, and the ordinary JobScorer math are
all untouched and must keep working unchanged.
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

# Long enough to reach the RICH description-length band on its own
# (>= 2000 chars) without mentioning any profile skill, so
# data_confidence clears MINIMUM_DECISION_CONFIDENCE (avoiding
# NEEDS_ENRICHMENT) while description_score stays near zero -- isolates
# the must/nice-emptiness effect this stage targets.
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


# --- Real pilot cases (mirroring the exact stored records) ------------------


def test_dach_style_sparse_junior_developer_is_rescued_to_maybe():
    # Mirrors "JUNIOR SOFTWARE DEVELOPER / ENTWICKLER (M/W/D)" -- must=[],
    # nice=[], RELEVANT title ("software developer" phrase), rich
    # description with no profile-skill overlap. Ordinary JobScorer math
    # alone would land this at SKIP; Stage 11C must raise it to MAYBE.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="JUNIOR SOFTWARE DEVELOPER / ENTWICKLER (M/W/D)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"
    assert result.score < 60  # unchanged score, only the label moved


def test_cyber_security_style_real_gap_is_not_rescued():
    # Mirrors "Junior Cyber Security Developer / Anwendungsentwickler
    # (m/w/d)" -- must=[git, python, typescript] (3 items, NOT sparse),
    # candidate matches git+python but genuinely lacks typescript. This
    # is a real, specific gap the extractor found, not an absence of
    # evidence -- must stay SKIP.
    db = _db()
    profile = _profile(db, ["python", "git"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Junior Cyber Security Developer / Anwendungsentwickler (m/w/d)",
        company="BWI GmbH",
        # Deliberately generic (no literal python/git/typescript mentions)
        # so description_score stays near zero and this isolates the
        # must_score=2/3 effect -- must_have_skills is supplied
        # structurally below, exactly as a real extractor pass would.
        description=GENERIC_RICH_DESCRIPTION,
        must_have_skills=["Python", "Git", "TypeScript"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


# --- Rescue: other plausible RELEVANT titles with sparse evidence ----------


def test_junior_python_developer_sparse_is_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Junior Python Developer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"


def test_backend_developer_sparse_is_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Backend Developer (m/w/div.)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"


def test_python_entwickler_sparse_is_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Python Entwickler")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"


def test_software_engineer_backend_sparse_is_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Software Engineer Backend")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"


# --- Adversarial: must NOT be rescued despite sparse evidence --------------


def test_personalcontroller_sparse_is_not_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Personalcontroller (m/w/d)", company="Bankpower GmbH")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_mongodb_administrator_sparse_is_not_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="MongoDB Administrator (m/w/d)", company="ANG GmbH")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_qgis_expert_sparse_is_not_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="QGIS Expertin / Experte", company="AGIS GmbH")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_presales_consultant_sparse_is_not_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Presales Consultant (m/w/d) - Datacenter")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_senior_python_developer_sparse_is_not_rescued_for_junior_candidate():
    # Stage 11A's own seniority gate only fires on an already-APPLY/MAYBE
    # result -- a SKIP-scored senior-titled job never reaches that gate,
    # so Stage 11C must independently refuse to rescue it.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Senior Python Developer")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_ambiguous_unknown_relevance_title_sparse_is_not_rescued():
    # UNKNOWN title relevance must fail open to "leave it alone", not to
    # "assume it's a dev role".
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Data Consultant (m/w/d)")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


# --- Regression guards: other outcomes / earlier gates untouched -----------


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


def test_seniority_exclusion_still_works_unchanged():
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


def test_relevance_exclusion_still_works_unchanged():
    db = _db()
    profile = _profile(db, ["python", "sap"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Personalcontroller (m/w/d)",
        description=("HR controlling role, Python scripting and SAP knowledge a plus. " * 15),
        must_have_skills=["Python", "SAP"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation not in ("MAYBE", "APPLY")


def test_legitimate_apply_job_is_completely_unaffected():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Python Backend Developer",
        description=RICH_TECH_DESCRIPTION,
        must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
        nice_to_have_skills=["Docker", "Pytest"],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "APPLY"


def test_needs_enrichment_job_is_not_touched_by_sparse_evidence_gate():
    # Low data_confidence (short/empty description) already yields
    # NEEDS_ENRICHMENT, which takes priority over SKIP in JobScorer's own
    # precedence -- Stage 11C only ever fires on recommendation == SKIP,
    # so this must stay NEEDS_ENRICHMENT, not become MAYBE.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Junior Python Developer", description="")

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "NEEDS_ENRICHMENT"


# --- Stage 11C safety-hardening guard tests (A-G) ---------------------------


def test_guard_a_relevant_title_zero_skills_software_dev_target_may_be_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Junior Python Developer", must_have_skills=[], nice_to_have_skills=[])

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"


def test_guard_b_one_matched_must_no_missing_may_be_rescued():
    # A single matched must-have (no missing) with an empty nice-to-have
    # set already clears the pre-existing Stage 10 "len(must) < 2 ->
    # MAYBE, never automatic APPLY" cap on its own (must_score=1.0 alone
    # contributes the full 70-point weight) -- this scenario never
    # actually reaches genuine SKIP, so it is a regression guard on the
    # OUTCOME (never wrongly suppressed to SKIP), not proof that Stage
    # 11C's own floor specifically fired.
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Junior Python Developer",
        must_have_skills=["Python"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "MAYBE"


def test_guard_c_one_missing_must_must_remain_skip():
    # Guard 1: a single CONCRETE missing must-have ("Java" the candidate
    # doesn't have) is a real, identified mismatch -- not sparse
    # evidence -- even though the total signal count (1) would otherwise
    # clear SPARSE_EVIDENCE_THRESHOLD. Must NOT be rescued.
    db = _db()
    profile = _profile(db, ["python"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Junior Python Developer",
        must_have_skills=["Java"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_guard_c_variant_three_must_one_missing_stays_skip_or_unaffected():
    # A larger, well-specified must-have set (3 items) with one genuine
    # gap -- already SUFFICIENT by count alone (>= 2), so the sparse
    # floor was never eligible regardless of guard 1; documents that
    # this class of "real partial mismatch" case is untouched either way
    # (mirrors the real Junior Cyber Security Developer pilot case).
    db = _db()
    profile = _profile(db, ["python", "git"])
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Junior Cyber Security Developer / Anwendungsentwickler (m/w/d)",
        must_have_skills=["Python", "Git", "TypeScript"],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_guard_d_missing_candidate_profile_must_remain_original_skip():
    # Guard 2: no CandidateProfile has ever been created (no
    # _set_target_roles call at all) -- _candidate_target_domain must
    # resolve to UNKNOWN (pure get, no create), so the rescue floor must
    # never fire.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    job = _job(title="Junior Python Developer", must_have_skills=[], nice_to_have_skills=[])

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_guard_e_ambiguous_target_roles_must_remain_original_skip():
    # Guard 2: target_roles mixes a relevant and an ambiguous target --
    # derive_candidate_target_domain resolves to UNKNOWN (not unanimous),
    # so the rescue floor must never fire.
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, ["Junior Python Developer", "Data Engineer Trainee"])
    job = _job(title="Junior Python Developer", must_have_skills=[], nice_to_have_skills=[])

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_guard_f_senior_mismatch_never_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(title="Senior Python Developer", must_have_skills=[], nice_to_have_skills=[])

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"


def test_guard_g_irrelevant_title_never_rescued():
    db = _db()
    profile = _profile(db, FULL_SKILL_SET)
    _set_target_roles(db, SOFTWARE_DEV_TARGET_ROLES)
    job = _job(
        title="Personalcontroller (m/w/d)",
        must_have_skills=[],
        nice_to_have_skills=[],
    )

    _record, result, _created = score_and_persist(db, profile, job)

    assert result.recommendation == "SKIP"
