"""Stage 14B final four-P1 Codex review reproductions (matcher v6).

P1 independent generic-experience clause, P1 prefixed group base-skill
leak, P1 multi-subject experience scope, P1 degree-field clause boundary --
plus the shared clause-ownership invariant, automation-threshold safety and
the v6 cache identity.
"""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.candidate_job_matcher import ALGORITHM_VERSION, JobMatchInput, compute_match
from app.agents.requirement_extractor import (
    extract_education_requirement,
    extract_experience_requirement,
)
from app.agents.skill_extractor import example_or_category_skills, extract_skills
from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint, create_match
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import CandidateJobMatchRecord, JobRecord
from app.models.candidate_job_match import CandidateJobMatchData
from app.models.candidate_profile import (
    CandidateEducation,
    CandidateExperience,
    CandidateJobPreferences,
    CandidateLanguage,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
)
from app.services.candidate_preparation import prepare_candidate_job_match

AS_OF = date(2026, 10, 15)
FOUR_YEARS = (date(2021, 1, 1), date(2025, 1, 1))
TWO_YEARS = (date(2023, 1, 1), date(2025, 1, 1))


def _profile(skills=(), experiences=(), languages=(), education=(), projects=()):
    return CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
        skills=[CandidateSkill(id=i, name=name) for i, name in enumerate(skills, 1)],
        experiences=list(experiences),
        languages=list(languages),
        education=list(education),
        projects=list(projects),
    )


def _experience(id_, period, technologies):
    start, end = period
    return CandidateExperience(
        id=id_,
        company=f"Company {id_}",
        job_title="Developer",
        start_date=start,
        end_date=end,
        is_current=end is None and start is not None,
        technologies=list(technologies),
    )


def _degree(field):
    return CandidateEducation(id=1, institution="Uni", field_of_study=field, completed=True)


def _match(description, profile=None, must=(), nice=(), title="Developer"):
    job = JobMatchInput(
        job_id=1,
        title=title,
        description=description,
        must_have_skills=list(must),
        nice_to_have_skills=list(nice),
    )
    return compute_match(job, profile or _profile(), company_research_id=None, as_of=AS_OF)


def _all(result):
    return [
        *result.matched_requirements,
        *result.partial_requirements,
        *result.missing_requirements,
        *result.unknown_requirements,
    ]


def _by_norm(result):
    return {r.normalized_requirement: r for r in _all(result)}


def _others(result):
    return [r for r in _all(result) if r.requirement_type == "OTHER"]


def _profil(*lines: str) -> str:
    return "Profil:\n" + "\n".join(f"- {line}" for line in lines)


def _threshold() -> int:
    return Settings.model_fields["automation_shortlist_min_match_score"].default


# --- P1 independent generic-experience clause -------------------------------------


@pytest.mark.parametrize(
    ("line", "typed", "kept"),
    [
        ("Deutsch B2 und mehrjährige Berufserfahrung", "german-b2", "mehrjährige Berufserfahrung"),
        ("Deutsch B2 und Berufserfahrung", "german-b2", "Berufserfahrung"),
        ("Kenntnisse in Python sowie praktische Erfahrung", "python", "praktische Erfahrung"),
        ("English C1 and practical experience", "english-c1", "practical experience"),
        # reversed order
        ("Mehrjährige Berufserfahrung und Deutsch B2", "german-b2", "Mehrjährige Berufserfahrung"),
        ("Berufserfahrung sowie Deutsch B2", "german-b2", "Berufserfahrung"),
        ("Praktische Erfahrung sowie Python", "python", "Praktische Erfahrung"),
        ("Practical experience and English C1", "english-c1", "Practical experience"),
        # comma / semicolon variants
        ("Deutsch B2, mehrjährige Berufserfahrung.", "german-b2", "mehrjährige Berufserfahrung"),
        ("Deutsch B2; Berufserfahrung", "german-b2", "Berufserfahrung"),
        ("Kenntnisse in Python, praktische Erfahrung", "python", "praktische Erfahrung"),
        ("English C1; practical experience", "english-c1", "practical experience"),
        ("Berufserfahrung, Kenntnisse in Python", "python", "Berufserfahrung"),
    ],
)
def test_independent_generic_experience_clause_survives(line, typed, kept):
    result = _match(_profil(line))
    reqs = _by_norm(result)
    assert set(reqs) == {typed, kept.casefold()}
    other = reqs[kept.casefold()]
    # ";" also ends a segment, so that clause is its own (whole) segment
    assert (other.requirement.rstrip(".;"), other.requirement_type) == (kept, "OTHER")
    assert (other.importance, other.match_status) == ("REQUIRED", "UNKNOWN")
    assert "HUMAN_REVIEW_REQUIRED" in other.reason
    assert len(other.job_evidence) == 1 and other.job_evidence[0] in line + "."


@pytest.mark.parametrize(
    "line",
    [
        "Mindestens 3 Jahre Berufserfahrung",
        "Mindestens 3 Jahre Berufserfahrung mit Python",
        "3+ years of professional experience with Python",
        "Mindestens drei Jahre Berufserfahrung im genannten Aufgabenbereich.",
        "Erfahrung mit Python",
        # shared complement: one head noun coordinated with an evaluated one
        "Kenntnisse und Erfahrung in Python",
        "Fundierte Kenntnisse und Erfahrung im Umgang mit Python",
        "Knowledge of and experience with Python",
    ],
)
def test_fully_evaluated_experience_line_has_no_duplicate_other(line):
    assert _others(_match(_profil(line))) == []


def test_a_wholly_generic_line_is_one_other():
    others = _others(_match(_profil("Mehrjährige Berufserfahrung")))
    assert [o.requirement for o in others] == ["Mehrjährige Berufserfahrung"]


def test_language_plus_experience_cannot_score_like_language_alone():
    profile = _profile(languages=[CandidateLanguage(id=1, language="Deutsch", level="C1")])
    alone = _match(_profil("Deutsch B2"), profile)
    mixed = _match(_profil("Deutsch B2 und mehrjährige Berufserfahrung"), profile)
    assert alone.overall_score >= _threshold()
    assert (mixed.required_skill_score, mixed.overall_score) == (50, 60)
    assert mixed.overall_score < _threshold()


# --- P1 prefixed group base-skill leak ----------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Python-Frameworks (z. B. Django, Flask)", {"group:python-frameworks"}),
        (
            "Kenntnisse in Python und Python-Frameworks (z. B. Django, Flask)",
            {"python", "group:python-frameworks"},
        ),
        ("LLM-Frameworks (z. B. LangChain, LlamaIndex)", {"group:llm-frameworks"}),
        (
            "Erfahrung mit LLM und LLM-Frameworks (z. B. LangChain, LlamaIndex)",
            {"llm", "group:llm-frameworks"},
        ),
        ("Java-Frameworks (z. B. Spring Boot)", {"group:java-frameworks"}),
        ("Erfahrung mit Docker und Python-Frameworks", {"docker", "python-frameworks"}),
    ],
)
def test_category_name_never_creates_a_base_skill_requirement(line, expected):
    assert set(_by_norm(_match(_profil(line), _profile(["Python", "LLM"])))) == expected


def test_category_embedded_technology_is_not_extracted():
    extraction = extract_skills(None, _profil("Python-Frameworks (z. B. Django, Flask)"))
    assert extraction.must_have_skills == []
    both = extract_skills(None, _profil("Python und Python-Frameworks (z. B. Django)"))
    assert both.must_have_skills == ["python"]
    assert example_or_category_skills(None, _profil("Python-Frameworks (z. B. Django)")) == {
        "python",
        "django",
    }


def test_stale_stored_base_skill_is_not_resurrected_from_a_category():
    reqs = _by_norm(_match(_profil("Python-Frameworks (z. B. Django)"), must=["python", "django"]))
    assert set(reqs) == {"group:python-frameworks"}


def test_stale_stored_base_skill_stays_when_named_independently():
    reqs = _by_norm(
        _match(_profil("Python und Python-Frameworks (z. B. Django)"), must=["python", "django"])
    )
    assert set(reqs) == {"python", "group:python-frameworks"}


def test_git_plus_python_framework_group_gets_no_free_python_credit():
    """Codex reproduction: required Git + Python-Framework group, candidate
    with Git and Python but no Django/Flask must not reach 80."""
    profile = _profile(
        ["Git", "Python"], [_experience(1, (date(2018, 1, 1), None), ["Git", "Python"])]
    )
    description = _profil("Kenntnisse in Git", "Python-Frameworks (z. B. Django, Flask)")
    result = _match(description, profile, must=["git", "python"])
    reqs = _by_norm(result)
    assert {norm: r.match_status for norm, r in reqs.items()} == {
        "git": "MATCH",
        "group:python-frameworks": "UNKNOWN",
    }
    assert result.required_skill_score == 50
    assert result.preferred_skill_score == 100
    assert result.experience_support_score == 100
    assert [e.matched_skills for e in result.relevant_experiences] == [["Git"]]
    assert result.overall_score == 70
    assert result.overall_score < _threshold()


# --- P1 multi-subject experience scope ------------------------------------------------


PARENTHETICAL = _profil("Mindestens 3 Jahre Berufserfahrung mit Python (Erfahrung mit Kubernetes)")
CONJUNCTIVE = _profil("Mindestens 3 Jahre Berufserfahrung mit Python und Kubernetes")
ALTERNATIVE = _profil("Mindestens 3 Jahre Berufserfahrung mit Python oder Kubernetes")
SEPARATE = _profil("3 Jahre Berufserfahrung mit Python und Erfahrung mit Kubernetes")


def _years_status(description, *experiences, projects=()):
    profile = _profile(["Python", "Kubernetes"], experiences, projects=projects)
    return _by_norm(_match(description, profile))["experience-3y"].match_status


@pytest.mark.parametrize(
    ("description", "subjects", "mode"),
    [
        (PARENTHETICAL, ("python",), "ALL"),
        (CONJUNCTIVE, ("python", "kubernetes"), "ALL"),
        (ALTERNATIVE, ("python", "kubernetes"), "ANY"),
        (SEPARATE, ("python",), "ALL"),
        (_profil("3+ years of experience with Python or Kubernetes"), ("python", "kubernetes"),
         "ANY"),
        (_profil("3 Jahre Berufserfahrung mit Python in Kubernetes-Umgebungen"),
         ("python", "kubernetes"), "AMBIGUOUS"),
    ],
)  # fmt: skip
def test_duration_subjects_and_their_relation(description, subjects, mode):
    requirement = extract_experience_requirement(None, description)
    assert (requirement.subjects, requirement.subject_mode) == (subjects, mode)


def test_parenthetical_kubernetes_years_never_satisfy_python_duration():
    assert _years_status(PARENTHETICAL, _experience(1, FOUR_YEARS, ["Kubernetes"])) == "MISSING"
    reqs = _by_norm(_match(PARENTHETICAL))
    assert reqs["kubernetes"].importance == "REQUIRED"
    assert "kubernetes" not in reqs["experience-3y"].requirement


@pytest.mark.parametrize("technology", ["Kubernetes", "Python"])
def test_conjunctive_duration_rejects_one_subject_history(technology):
    assert _years_status(CONJUNCTIVE, _experience(1, FOUR_YEARS, [technology])) == "MISSING"


def test_conjunctive_duration_from_separate_histories_is_unknown():
    status = _years_status(
        CONJUNCTIVE,
        _experience(1, FOUR_YEARS, ["Python"]),
        _experience(2, FOUR_YEARS, ["Kubernetes"]),
    )
    assert status == "UNKNOWN"


def test_conjunctive_duration_matches_one_history_using_both():
    both = _experience(1, FOUR_YEARS, ["Python", "Kubernetes"])
    assert _years_status(CONJUNCTIVE, both) == "MATCH"
    short = _experience(1, TWO_YEARS, ["Python", "Kubernetes"])
    assert _years_status(CONJUNCTIVE, short) == "PARTIAL"


def test_conjunctive_duration_with_incomplete_dates_is_unknown():
    undated = _experience(1, (None, None), ["Python", "Kubernetes"])
    assert _years_status(CONJUNCTIVE, undated) == "UNKNOWN"


def test_projects_are_never_professional_years():
    project = CandidateProject(id=1, name="Tool", technologies=["Python", "Kubernetes"])
    assert _years_status(CONJUNCTIVE, projects=[project]) == "MISSING"
    assert _years_status(PARENTHETICAL, projects=[project]) == "MISSING"


def test_alternative_duration_accepts_either_subject():
    assert _years_status(ALTERNATIVE, _experience(1, FOUR_YEARS, ["Kubernetes"])) == "MATCH"


def test_ambiguous_relation_is_never_a_definite_match():
    description = _profil("3 Jahre Berufserfahrung mit Python in Kubernetes-Umgebungen")
    both = _experience(1, FOUR_YEARS, ["Python", "Kubernetes"])
    assert _years_status(description, both) == "UNKNOWN"
    assert _years_status(description) == "MISSING"


def test_separate_second_clause_leaves_python_in_control():
    assert _years_status(SEPARATE, _experience(1, FOUR_YEARS, ["Python"])) == "MATCH"
    assert _years_status(SEPARATE, _experience(1, FOUR_YEARS, ["Kubernetes"])) == "MISSING"


def test_simple_python_duration_is_unchanged():
    description = _profil("Mindestens 3 Jahre Berufserfahrung mit Python")
    assert _years_status(description, _experience(1, FOUR_YEARS, ["Python"])) == "MATCH"
    assert _years_status(description, _experience(1, TWO_YEARS, ["Python"])) == "PARTIAL"


@pytest.mark.parametrize("description", [PARENTHETICAL, CONJUNCTIVE])
def test_kubernetes_only_years_cannot_reach_the_threshold(description):
    profile = _profile(["Python", "Kubernetes"], [_experience(1, FOUR_YEARS, ["Kubernetes"])])
    result = _match(description, profile)
    assert _by_norm(result)["experience-3y"].match_status == "MISSING"
    assert result.experience_support_score == 0
    assert result.overall_score < _threshold()


# --- P1 degree-field clause boundary --------------------------------------------------


@pytest.mark.parametrize(
    ("line", "fields"),
    [
        ("Abgeschlossenes Studium der Mathematik und Kenntnisse in Informatik", ("Mathematik",)),
        ("Abgeschlossenes Studium der Informatik und Kenntnisse in Mathematik", ("Informatik",)),
        (
            "Abgeschlossenes Studium der Wirtschaftsinformatik und Kenntnisse in Python",
            ("Wirtschaftsinformatik",),
        ),
        (
            "Kenntnisse in Informatik und ein abgeschlossenes Studium der Mathematik",
            ("Mathematik",),
        ),
        ("Abgeschlossenes Studium der Mathematik, Kenntnisse in Informatik", ("Mathematik",)),
        ("Abgeschlossenes Studium der Mathematik; Kenntnisse in Informatik", ("Mathematik",)),
        ("Abgeschlossenes Studium der Mathematik sowie Kenntnisse in Informatik", ("Mathematik",)),
        ("Abgeschlossenes Studium der Mathematik (Kenntnisse in Informatik)", ("Mathematik",)),
        ("Kenntnisse in Informatik, abgeschlossenes Studium der Mathematik", ("Mathematik",)),
        # the degree's own field list is unchanged
        (
            "Abgeschlossenes Studium der Informatik, Wirtschaftsinformatik oder vergleichbare "
            "Qualifikation",
            ("Wirtschaftsinformatik", "Informatik"),
        ),
        ("Bachelor- und Masterabschluss in Informatik", ("Informatik",)),
    ],
)
def test_degree_fields_come_from_the_degree_clause_only(line, fields):
    assert extract_education_requirement(None, _profil(line)).fields == fields


@pytest.mark.parametrize(
    ("line", "degree", "status"),
    [
        ("Abgeschlossenes Studium der Mathematik und Kenntnisse in Informatik", "Informatik",
         "UNKNOWN"),
        ("Abgeschlossenes Studium der Mathematik und Kenntnisse in Informatik", "Mathematik",
         "MATCH"),
        ("Abgeschlossenes Studium der Informatik und Kenntnisse in Mathematik", "Mathematik",
         "UNKNOWN"),
        ("Kenntnisse in Informatik und ein abgeschlossenes Studium der Mathematik", "Informatik",
         "UNKNOWN"),
        ("Abgeschlossenes Studium der Wirtschaftsinformatik und Kenntnisse in Python",
         "Wirtschaftsinformatik", "MATCH"),
    ],
)  # fmt: skip
def test_separate_knowledge_field_never_satisfies_the_degree(line, degree, status):
    result = _match(_profil(line), _profile(["Python"], education=[_degree(degree)]))
    assert _by_norm(result)["completed-degree"].match_status == status


def test_separate_knowledge_clause_stays_its_own_requirement():
    result = _match(_profil("Abgeschlossenes Studium der Mathematik und Kenntnisse in Informatik"))
    reqs = _by_norm(result)
    assert set(reqs) == {"completed-degree", "informatik"}
    assert reqs["informatik"].requirement_type == "OTHER"
    python = _by_norm(
        _match(
            _profil("Abgeschlossenes Studium der Wirtschaftsinformatik und Kenntnisse in Python")
        )
    )
    assert set(python) == {"completed-degree", "python"}


def test_informatik_knowledge_cannot_lift_a_mathematik_degree_over_the_threshold():
    description = _profil(
        "Abgeschlossenes Studium der Mathematik und Kenntnisse in Informatik",
        "Kenntnisse in Python",
        "Kenntnisse in Git",
        "Kenntnisse in Docker",
    )
    work = _experience(1, (date(2018, 1, 1), None), ["Python", "Git", "Docker"])
    skills = ["Python", "Git", "Docker"]
    mathematik = _match(description, _profile(skills, [work], education=[_degree("Mathematik")]))
    informatik = _match(description, _profile(skills, [work], education=[_degree("Informatik")]))
    assert mathematik.overall_score >= _threshold()
    assert _by_norm(informatik)["completed-degree"].match_status != "MATCH"
    assert informatik.overall_score < _threshold()


# --- common clause-ownership invariant -----------------------------------------------


TYPED_PARTS = {
    "Deutsch B2": "german-b2",
    "English C1": "english-c1",
    "Kenntnisse in Python": "python",
    "abgeschlossenes Studium der Informatik": "completed-degree",
    "mindestens 3 Jahre Berufserfahrung mit Python": "experience-3y",
    "Python-Frameworks (z. B. Django)": "group:python-frameworks",
    "Kenntnisse in KI-Frameworks (z. B. LangChain)": "group:ki-frameworks",
}
GENERIC_PARTS = [
    "mehrjährige Berufserfahrung",
    "Berufserfahrung",
    "praktische Erfahrung",
    "practical experience",
]
CONNECTORS = [" und ", ", ", " sowie ", "; "]


@pytest.mark.parametrize("typed", TYPED_PARTS)
@pytest.mark.parametrize("generic", GENERIC_PARTS)
@pytest.mark.parametrize("connector", CONNECTORS)
@pytest.mark.parametrize("generic_first", [False, True])
def test_every_independent_clause_is_typed_or_kept(typed, generic, connector, generic_first):
    """Invariant: each clause of a required line is either owned by a typed
    requirement of exactly its own syntax or survives as REQUIRED/UNKNOWN --
    no clause disappears because its words are generic, and no typed
    requirement reads a neighbouring clause."""
    parts = [generic, typed] if generic_first else [typed, generic]
    line = connector.join(parts)
    result = _match(_profil(line + "."))
    reqs = _by_norm(result)
    assert TYPED_PARTS[typed] in reqs
    others = [r for r in _all(result) if r.requirement_type == "OTHER"]
    # v7: every generic part is QUALIFIED experience, which is never shared
    # away with a neighbouring head (see test_stage14b_clause_model_v7.py).
    assert [(o.normalized_requirement, o.importance, o.match_status) for o in others] == [
        (generic.casefold(), "REQUIRED", "UNKNOWN")
    ]
    if "experience-3y" in reqs:
        assert extract_experience_requirement(None, _profil(line)).subjects == ("python",)
    if "completed-degree" in reqs:
        assert extract_education_requirement(None, _profil(line)).fields == ("Informatik",)
    # a category token never yields its base technology
    if typed.startswith("Python-Frameworks"):
        assert "python" not in reqs


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("abgeschlossenes Studium der Mathematik", "Kenntnisse in Informatik"),
        ("3 Jahre Berufserfahrung mit Python", "Erfahrung mit Kubernetes"),
    ],
)
@pytest.mark.parametrize("connector", [" und ", ", ", " sowie ", "; ", " ("])
@pytest.mark.parametrize("reverse", [False, True])
def test_no_typed_requirement_consumes_a_neighbouring_clause(first, second, connector, reverse):
    parts = [second, first] if reverse else [first, second]
    line = connector.join(parts) + (")" if connector == " (" else "")
    if "Studium" in first:
        assert extract_education_requirement(None, _profil(line)).fields == ("Mathematik",)
    else:
        assert extract_experience_requirement(None, _profil(line)).subjects == ("python",)


# --- v6 cache identity -----------------------------------------------------------------


def test_v5_row_is_kept_and_cache_lookup_uses_the_current_version():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v6",
            source="bundesagentur",
            title="Developer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("Deutsch B2 und mehrjährige Berufserfahrung"),
            skills_json="[]",
            data_confidence=0.9,
            must_have_skills_json=json.dumps([]),
            nice_to_have_skills_json="[]",
            score=80,
            recommendation="APPLY",
            status="NEW",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(job)
        db.commit()
        profile_version = get_or_create_candidate_profile(db).profile_version
        v5_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v5",
            overall_score=90,
            coverage_score=90,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=50,
        )
        create_match(db, v5_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert result.algorithm_version == ALGORITHM_VERSION
        assert "mehrjährige Berufserfahrung" in [r.requirement for r in result.unknown_requirements]
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v5", ALGORITHM_VERSION]
