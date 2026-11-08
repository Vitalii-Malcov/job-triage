"""A thin skill list is missing evidence, not a mismatch.

The Stage 11E cardinality guard turns an APPLY/MAYBE with fewer than two distinct skill
signals into SKIP; Stage 11C then lifts it back to MAYBE (human review) when the title is a
plausible software role. "Junior Engineer (m/w/d) Schwerpunkt Python" has a Python signal and
a Python title but "Engineer" alone is not a software word, so the title read UNKNOWN and the
vacancy stayed SKIP. These tests pin the widened rescue and everything it must not reach.
"""

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.role_relevance_classifier import is_review_rescue_title
from app.agents.seniority_classifier import classify_title_seniority
from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.models import ApplicationRecord, JobRecord
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.models.job import Job
from app.services.collector_runner import score_and_persist
from app.services.job_fit_assessment import (
    MISSING_REQUIRED_MARKER,
    Criterion,
    FitAssessment,
    language_criterion,
)
from app.services.shortlist_gate import automatic_block_reason
from app.services.triage_category import THIN_SKILL_EVIDENCE, classify

SKILLS = frozenset(["python", "fastapi", "sqlalchemy", "postgresql", "git", "pytest", "docker"])
TARGET_ROLES = ["Junior Python Developer", "Junior Backend Developer"]

# Rich enough for data_confidence to clear the NEEDS_ENRICHMENT floor, no profile skill in it.
RICH_DESCRIPTION = (
    "Wir entwickeln Fachanwendungen fuer unsere Kunden in einem agilen, kollaborativen Team "
    "mit klaren Verantwortlichkeiten und guter Dokumentation. " * 30
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=1, target_roles=TARGET_ROLES)
    )
    return db


def _job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "Junior Engineer (m/w/d) Schwerpunkt Python",
        "company": "Example GmbH",
        "url": "https://example.com/job/1",
        "description": RICH_DESCRIPTION + " Python",
        "posting_type": "ARBEIT",
        "must_have_skills": ["Python"],
        "nice_to_have_skills": [],
    }
    data.update(overrides)
    return Job(**data)


def _score(job: Job, db: Session | None = None):
    db = db or _db()
    _record, result, _created = score_and_persist(db, SKILLS, job)
    return result


# --- the vacancy that was lost ------------------------------------------------------------


def test_junior_engineer_python_with_one_signal_goes_to_review_not_skip():
    result = _score(_job())
    assert result.recommendation == "MAYBE"


def test_the_rescue_never_reaches_apply():
    assert _score(_job()).recommendation != "APPLY"


def test_junior_python_with_a_confirmed_missing_requirement_stays_skip():
    result = _score(_job(must_have_skills=["Python", "TypeScript"]))
    assert result.recommendation == "SKIP"


# --- missing data is enrichment, not rejection -------------------------------------------


def test_short_description_is_needs_enrichment_not_skip():
    result = _score(_job(description="Junior Engineer Python", must_have_skills=[]))
    assert result.recommendation == "NEEDS_ENRICHMENT"


def test_title_only_stub_is_needs_enrichment():
    result = _score(_job(title="(Junior) Python Entwickler", description="", must_have_skills=[]))
    assert result.recommendation == "NEEDS_ENRICHMENT"


# --- what must not become suitable ------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Python Engineer (m/w/d)",
        "Lead Engineer Python",
        "Python Systemadministrator (m/w/d)",
        "IT Support Engineer Python (m/w/d)",
        "Helpdesk Engineer Python",
        "2nd Level Support Engineer Python",
        "Windows Administrator Python Scripting",
        "Senior Java Developer",
        "DevOps Engineer (m/w/d)",
        "Engineer Maschinenbau",
        "DevOps Engineer - Python",
        "IT System Engineer - Python",
        "Mechanical Engineer - Python scripting",
        "Sr. Engineer Python",
        "Sr Engineer Python",
    ],
)
def test_unsuitable_titles_are_not_rescued(title):
    assert _score(_job(title=title)).recommendation == "SKIP"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Junior Engineer (m/w/d) Schwerpunkt Python", True),
        ("Python Engineer", True),
        ("Backend Engineer (m/w/d)", True),
        ("Junior Python Developer", True),
        ("IT System Engineer - Backend (m/w/d)", False),
        ("Engineer - Python", True),
        ("Python-Engineer", True),
        ("DevOps Engineer - Python", False),
        ("IT System Engineer - Python", False),
        ("Mechanical Engineer - Python scripting", False),
        ("Sr. Engineer Python", False),
        ("Engineer", False),
        ("DevOps Engineer", False),
        ("Python Systemadministrator", False),
        ("Python Presales Engineer", False),
        ("Elektroingenieur Python", False),
        ("", False),
    ],
)
def test_review_rescue_title_truth_table(title, expected):
    assert is_review_rescue_title(title) is expected


# --- idempotence and persistence ---------------------------------------------------------


def test_scoring_twice_gives_the_same_result_and_one_row():
    db = _db()
    first = _score(_job(), db)
    second = _score(_job(), db)
    assert (first.score, first.recommendation) == (second.score, second.recommendation)
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 1


def test_rescoring_creates_no_application_and_keeps_the_job_status():
    db = _db()
    _score(_job(), db)
    record = db.scalars(select(JobRecord)).one()
    status = record.status
    _score(_job(), db)
    assert db.scalar(select(func.count()).select_from(ApplicationRecord)) == 0
    assert db.scalars(select(JobRecord)).one().status == status


# --- language policy (unchanged; pinned so a later edit is visible) ---------------------


@pytest.mark.parametrize(
    "text,status",
    [
        ("Deutsch B1 erforderlich", "OK"),
        ("Deutschkenntnisse B2 erforderlich", "GAP"),
        ("Sehr gutes Deutsch in Wort und Schrift (min. B2-C1)", "GAP"),
        ("Deutsch B2 wünschenswert", "MINOR_GAP"),
        ("Deutsch auf C1-Niveau zwingend erforderlich", "MAJOR_GAP"),
        ("Python developer, English-speaking team", "NO_REQUIREMENT"),
    ],
)
def test_language_policy_at_confirmed_b1(text, status):
    assert language_criterion(text, "B1").status == status


# --- triage: thin evidence is a question for the operator, never a prepared application ----


def _assessment(
    title,
    *,
    role="OTHER",
    skills="WEAK",
    language="NO_REQUIREMENT",
    skills_reason="s",
    language_reason="g",
):
    a = FitAssessment(job_id=1, company="Example GmbH", title=title)
    a.criteria = [
        Criterion("role", role, "r"),
        Criterion("skills", skills, skills_reason),
        Criterion("location", "REMOTE_OK", "l"),
        Criterion("remote", "COMPATIBLE", "r"),
        Criterion("entry_level", "ENTRY_LEVEL", "e"),
        Criterion("language", language, language_reason),
        Criterion("salary", "UNKNOWN", "p"),
        Criterion("start_date", "UNKNOWN", "d"),
        Criterion("experience", "UNKNOWN", "x"),
    ]
    return a


def test_python_engineer_with_thin_skills_is_manual_review_not_excluded():
    a = _assessment("Junior Engineer (m/w/d) Schwerpunkt Python", language="GAP")
    result = classify(a)
    assert (result.category, result.code) == ("MANUAL_REVIEW", THIN_SKILL_EVIDENCE)
    assert automatic_block_reason(a) is not None  # never prepared on its own


@pytest.mark.parametrize(
    "title",
    [
        "Datenanalyst (m/w/d)",
        "DevOps Engineer (m/w/d)",
        "IT Support Engineer Python (m/w/d)",
        "Python Systemadministrator",
        "Engineer Maschinenbau",
    ],
)
def test_thin_skills_do_not_rescue_other_titles(title):
    assert classify(_assessment(title)).category == "EXCLUDED"


def test_weak_skills_with_a_named_missing_requirement_are_not_thin_evidence():
    a = _assessment(
        "Junior Engineer Schwerpunkt Python",
        skills_reason="required skill score 10/100 (v12)"
        + MISSING_REQUIRED_MARKER
        + "Java, Spring",
    )
    assert classify(a).category == "EXCLUDED"


def test_thin_evidence_review_keeps_the_stated_gaps_visible():
    a = _assessment(
        "Junior Engineer Schwerpunkt Python", language="GAP", language_reason="German B2 required"
    )
    result = classify(a)
    assert result.code == THIN_SKILL_EVIDENCE
    assert "German B2 required" in result.reason


@pytest.mark.parametrize("title", ["Sr. Engineer Python", "Sr Python Developer", "SR. Engineer"])
def test_sr_abbreviation_is_a_senior_marker(title):
    assert classify_title_seniority(title).level == "SENIOR"


def test_sr_is_not_matched_inside_words():
    assert classify_title_seniority("Srinivasan Python Developer").level != "SENIOR"
