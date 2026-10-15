"""Stage 14B: a CandidateJobMatch must not report a perfect fit while
material vacancy requirements were never represented or evaluated.

Reproduces the E362/B finding: the stored extraction kept only
git/python/rest (the bare "Profil:" header was not a recognized
requirement section and LLM-framework names were unknown), and the v1
matcher scored 100 although the vacancy also demands 3+ years of
experience, LangChain/LlamaIndex, LLM/GenAI, CI/CD, testing and an
Informatik-type degree.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.candidate_job_matcher import ALGORITHM_VERSION, JobMatchInput, compute_match
from app.agents.requirement_extractor import (
    extract_education_requirement,
    extract_experience_requirement,
    must_section_segments,
)
from app.agents.skill_extractor import extract_skills
from app.db.base import Base
from app.db.repositories import upsert_job
from app.models.candidate_profile import (
    CandidateEducation,
    CandidateExperience,
    CandidateJobPreferences,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
)
from app.models.job import Job, JobScore

AS_OF = date(2026, 10, 15)

E362_STYLE = """Wir suchen für den Standort Hannover einen AI Engineer.

Aufgaben:
- Sie entwickeln KI-Lösungen inklusive Testing und Monitoring.
- Sie konzipieren Retrieval-Augmented-Generation (RAG)-Systeme.

Profil:
- Abgeschlossenes Studium der Informatik, Wirtschaftsinformatik oder vergleichbare Qualifikation.
- Mindestens drei Jahre Berufserfahrung im genannten Aufgabenbereich und in der Softwareentwicklung.
- Kenntnisse in Python und modernen KI-Frameworks (z. B. LangChain, LlamaIndex).
- Praxiserfahrung im Umgang mit LLMs und Generative AI (Prompting, Fine-Tuning).
- Erfahrung mit API-Entwicklung (REST) und Git-basierter Softwareentwicklung.
- Grundverständnis von Software-Engineering-Prinzipien (Testing, CI/CD).
- Ausgeprägte kommunikative Stärken sowie die Bereitschaft zur Dokumentation.

Klingt interessant?
Wir freuen uns über alle Bewerbungen unter Angabe der Kennziffer E362/B.
"""


def _profile(**overrides) -> CandidateProfile:
    data = dict(
        id=1,
        profile_version=3,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
    )
    data.update(overrides)
    return CandidateProfile(**data)


def _v3_like_profile() -> CandidateProfile:
    """Shape of the real profile v3: no LLM/CI facts, IT-support and
    leadership experience without technologies, Python projects, a
    completed engineering degree without a field of study."""
    return _profile(
        skills=[
            CandidateSkill(id=1, name="Python"),
            CandidateSkill(id=2, name="Git"),
            CandidateSkill(id=3, name="REST API"),
            CandidateSkill(id=4, name="Pytest"),
            CandidateSkill(id=5, name="Unit testing"),
        ],
        experiences=[
            CandidateExperience(id=1, company="AS-IT", job_title="IT-Mitarbeiter", is_current=True),
            CandidateExperience(id=2, company="Grenzpolizei", job_title="Leitende Positionen"),
        ],
        projects=[
            CandidateProject(id=1, name="Movie Search App", technologies=["Python", "Git"]),
            CandidateProject(id=2, name="Stock Momentum Screener", technologies=["Python"]),
        ],
        education=[
            CandidateEducation(
                id=1, institution="Akademie", degree="Ingenieurabschluss", completed=True
            )
        ],
    )


def _job(description: str, must=(), nice=(), title="AI Engineer") -> JobMatchInput:
    return JobMatchInput(
        job_id=1,
        title=title,
        description=description,
        must_have_skills=list(must),
        nice_to_have_skills=list(nice),
    )


def _by_requirement(result) -> dict:
    return {
        r.normalized_requirement: r
        for r in (
            *result.matched_requirements,
            *result.partial_requirements,
            *result.missing_requirements,
            *result.unknown_requirements,
        )
    }


# --- extraction ---------------------------------------------------------------


def test_profil_header_and_ai_requirements_are_extracted():
    extraction = extract_skills("AI Engineer", E362_STYLE)
    assert set(extraction.must_have_skills) >= {
        "python", "git", "rest", "langchain", "llamaindex", "llm", "generative ai",
        "prompting", "fine-tuning", "ci/cd", "testing",
    }  # fmt: skip
    # Task-section mentions (RAG, Testing und Monitoring) carry no requirement context.
    assert "rag" not in extraction.must_have_skills + extraction.nice_to_have_skills


def test_experience_and_degree_field_extraction():
    experience = extract_experience_requirement("AI Engineer", E362_STYLE)
    assert experience is not None and experience.years == 3
    assert experience.importance == "REQUIRED"
    education = extract_education_requirement("AI Engineer", E362_STYLE)
    assert education.fields == ("Wirtschaftsinformatik", "Informatik")
    assert education.importance == "REQUIRED"
    assert (
        extract_experience_requirement(None, "Ihr Profil:\n- 5+ years of Python experience").years
        == 5
    )
    assert extract_experience_requirement(None, "Wir bestehen seit 30 Jahren.") is None


def test_must_section_segments_only_cover_requirement_sections():
    segments = must_section_segments(E362_STYLE)
    assert len(segments) == 7
    assert not any("Monitoring" in segment for segment in segments)


# --- E362/B regression ----------------------------------------------------------


def test_e362b_stale_extraction_no_longer_scores_perfect():
    result = compute_match(
        _job(E362_STYLE, must=["git", "python", "rest"]),
        _v3_like_profile(),
        company_research_id=None,
        as_of=AS_OF,
    )
    reqs = _by_requirement(result)

    assert result.algorithm_version == ALGORITHM_VERSION == "v3"
    assert result.overall_score < 100
    assert result.required_skill_score < 100
    for supported in ("git", "python", "rest"):
        assert reqs[supported].match_status == "MATCH"
    for missing in ("langchain", "llamaindex", "llm", "generative-ai", "cicd"):
        assert reqs[missing].match_status == "MISSING"
        assert reqs[missing].importance == "REQUIRED"
    assert any("absent from the stored extraction" in w for w in result.warnings)


def test_e362b_three_years_not_satisfied_by_projects():
    result = compute_match(
        _job(E362_STYLE, must=["git", "python", "rest"]),
        _v3_like_profile(),
        company_research_id=None,
        as_of=AS_OF,
    )
    experience = _by_requirement(result)["experience-3y"]
    assert experience.requirement_type == "EXPERIENCE"
    assert experience.match_status == "MISSING"
    assert result.relevant_experiences == []
    assert result.relevant_projects  # projects are still reported as project evidence
    assert result.experience_support_score == 0


def test_e362b_generic_degree_is_not_an_informatik_degree():
    result = compute_match(
        _job(E362_STYLE), _v3_like_profile(), company_research_id=None, as_of=AS_OF
    )
    education = _by_requirement(result)["completed-degree"]
    assert education.match_status == "UNKNOWN"
    assert "HUMAN_REVIEW_REQUIRED" in education.reason
    assert [ref.entity_id for ref in education.candidate_evidence] == [1]


def test_e362b_unevaluable_required_text_is_exposed():
    result = compute_match(
        _job(E362_STYLE), _v3_like_profile(), company_research_id=None, as_of=AS_OF
    )
    other = [r for r in result.unknown_requirements if r.requirement_type == "OTHER"]
    assert len(other) == 1
    assert other[0].importance == "REQUIRED"
    assert "kommunikative" in other[0].requirement
    assert "NOT_EVALUATED" in other[0].reason


# --- generic fail-closed rules --------------------------------------------------


def test_unextracted_required_text_cannot_disappear():
    description = "Ihr Profil:\n- Kenntnisse in SAP ABAP und SAP HANA\n- Kenntnisse in Python"
    result = compute_match(
        _job(description, must=["python"]),
        _profile(skills=[CandidateSkill(id=1, name="Python")]),
        company_research_id=None,
        as_of=AS_OF,
    )
    other = [r for r in result.unknown_requirements if r.requirement_type == "OTHER"]
    assert [r.requirement for r in other] == ["Kenntnisse in SAP ABAP und SAP HANA"]
    assert result.required_skill_score == 50
    assert any("status unknown" in w for w in result.warnings)


def test_missing_framework_blocks_required_fit():
    description = "Ihr Profil:\n- Kenntnisse in Python und LangChain"
    result = compute_match(
        _job(description),
        _profile(skills=[CandidateSkill(id=1, name="Python")]),
        company_research_id=None,
        as_of=AS_OF,
    )
    assert _by_requirement(result)["langchain"].match_status == "MISSING"
    assert result.required_skill_score == 50


@pytest.mark.parametrize(
    "skills,expected",
    [
        (["Pytest"], "UNKNOWN"),
        (["Unit testing"], "UNKNOWN"),
        (["Testing"], "MATCH"),
        (["Docker"], "MISSING"),
    ],
)
def test_generic_testing_is_satisfied_only_by_an_exact_skill(skills, expected):
    description = "Ihr Profil:\n- Grundverständnis von Testing"
    profile = _profile(skills=[CandidateSkill(id=i, name=n) for i, n in enumerate(skills, 1)])
    result = compute_match(_job(description), profile, company_research_id=None, as_of=AS_OF)
    assert _by_requirement(result)["testing"].match_status == expected


def _experience_profile(*periods) -> CandidateProfile:
    return _profile(
        skills=[CandidateSkill(id=1, name="Python")],
        experiences=[
            CandidateExperience(
                id=i,
                company=f"Company {i}",
                job_title="Python Developer",
                start_date=start,
                end_date=end,
                is_current=end is None,
                technologies=["Python"],
            )
            for i, (start, end) in enumerate(periods, 1)
        ],
    )


_THREE_YEARS = "Ihr Profil:\n- Mindestens 3 Jahre Berufserfahrung mit Python"


@pytest.mark.parametrize(
    "periods,expected",
    [
        ([(date(2020, 1, 1), date(2023, 6, 30))], "MATCH"),
        ([(date(2025, 1, 1), None)], "PARTIAL"),
        ([(date(2023, 1, 1), None)], "MATCH"),
        # Overlapping periods are merged, never double counted (2 years, not 4).
        ([(date(2021, 1, 1), date(2023, 1, 1)), (date(2021, 1, 1), date(2023, 1, 1))], "PARTIAL"),
        ([(None, None)], "UNKNOWN"),
    ],
)
def test_years_of_experience_uses_relevant_dated_experience(periods, expected):
    result = compute_match(
        _job(_THREE_YEARS), _experience_profile(*periods), company_research_id=None, as_of=AS_OF
    )
    assert _by_requirement(result)["experience-3y"].match_status == expected
    if expected != "MATCH":
        assert result.experience_support_score < 100


def test_projects_alone_never_satisfy_years_of_experience():
    profile = _profile(
        skills=[CandidateSkill(id=1, name="Python")],
        projects=[CandidateProject(id=1, name="Tool", technologies=["Python"])],
    )
    result = compute_match(_job(_THREE_YEARS), profile, company_research_id=None, as_of=AS_OF)
    assert _by_requirement(result)["experience-3y"].match_status == "MISSING"
    assert len(result.relevant_projects) == 1
    assert result.experience_support_score == 0


@pytest.mark.parametrize(
    "education,expected",
    [
        ({"field_of_study": "Informatik", "completed": True}, "MATCH"),
        ({"program": "Wirtschaftsinformatik", "completed": True}, "MATCH"),
        ({"degree": "Ingenieurabschluss", "completed": True}, "UNKNOWN"),
        ({"field_of_study": "Informatik", "completed": False}, "MISSING"),
    ],
)
def test_field_specific_degree(education, expected):
    description = "Ihr Profil:\n- Abgeschlossenes Studium der Informatik oder Wirtschaftsinformatik"
    record = CandidateEducation(id=1, institution="TU", **education)
    result = compute_match(
        _job(description), _profile(education=[record]), company_research_id=None, as_of=AS_OF
    )
    assert _by_requirement(result)["completed-degree"].match_status == expected


def test_informatik_does_not_match_wirtschaftsinformatik_only_requirement():
    description = "Ihr Profil:\n- Abgeschlossenes Studium der Wirtschaftsinformatik"
    education = CandidateEducation(
        id=1, institution="TU", field_of_study="Informatik", completed=True
    )
    result = compute_match(
        _job(description), _profile(education=[education]), company_research_id=None, as_of=AS_OF
    )
    assert _by_requirement(result)["completed-degree"].match_status == "UNKNOWN"


def test_generic_degree_requirement_still_matches_any_completed_degree():
    description = "Ihr Profil:\n- Abgeschlossenes Studium"
    education = CandidateEducation(id=1, institution="TU", degree="Ingenieur", completed=True)
    result = compute_match(
        _job(description), _profile(education=[education]), company_research_id=None, as_of=AS_OF
    )
    assert _by_requirement(result)["completed-degree"].match_status == "MATCH"


def test_genuine_complete_match_still_reaches_top_score():
    description = (
        "Ihr Profil:\n"
        "- Abgeschlossenes Studium der Informatik\n"
        "- Mindestens 3 Jahre Berufserfahrung mit Python\n"
        "- Kenntnisse in LangChain und Git\n"
    )
    profile = _profile(
        skills=[
            CandidateSkill(id=1, name="Python"),
            CandidateSkill(id=2, name="LangChain"),
            CandidateSkill(id=3, name="Git"),
        ],
        experiences=[
            CandidateExperience(
                id=1,
                company="Acme",
                job_title="Python Developer",
                start_date=date(2020, 1, 1),
                end_date=date(2024, 1, 1),
                technologies=["Python", "LangChain", "Git"],
            )
        ],
        education=[
            CandidateEducation(id=1, institution="TU", field_of_study="Informatik", completed=True)
        ],
    )
    result = compute_match(_job(description), profile, company_research_id=None, as_of=AS_OF)
    assert result.unknown_requirements == []
    assert result.missing_requirements == []
    assert result.overall_score == 100


# --- historical observation timestamps on transfer -------------------------------


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _posting(url="https://example.com/jobs/1") -> Job:
    return Job(source="test", title="AI Engineer", company="Example GmbH", url=url)


_SCORE = JobScore(score=83, recommendation="APPLY", data_confidence=0.85)
_FIRST = datetime(2026, 9, 15, 13, 31, 5)
_LAST = datetime(2026, 9, 15, 19, 40, 44)


def test_upsert_job_preserves_historical_observation_times(db):
    record, created = upsert_job(db, _posting(), _SCORE, first_seen_at=_FIRST, last_seen_at=_LAST)
    assert created
    assert record.first_seen_at.replace(tzinfo=None) == _FIRST
    assert record.last_seen_at.replace(tzinfo=None) == _LAST


def test_upsert_job_without_timestamps_still_uses_now(db):
    before = datetime.now(UTC).replace(tzinfo=None)
    record, _ = upsert_job(db, _posting(), _SCORE)
    assert record.first_seen_at.replace(tzinfo=None) >= before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"first_seen_at": _FIRST},
        {"last_seen_at": _LAST},
        {"first_seen_at": _LAST, "last_seen_at": _FIRST},
    ],
)
def test_upsert_job_rejects_inconsistent_timestamps(db, kwargs):
    with pytest.raises(ValueError):
        upsert_job(db, _posting(), _SCORE, **kwargs)


def test_upsert_job_never_rewrites_existing_history(db):
    record, _ = upsert_job(db, _posting(), _SCORE)
    original = (record.first_seen_at, record.last_seen_at)
    with pytest.raises(ValueError, match="new job record"):
        upsert_job(db, _posting(), _SCORE, first_seen_at=_FIRST, last_seen_at=_LAST)
    db.rollback()
    db.refresh(record)
    assert (record.first_seen_at, record.last_seen_at) == original
