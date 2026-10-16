"""Stage 14B Codex v6 review reproductions (matcher v7): one canonical
clause model.

P1 qualified experience in shared-complement constructions, P1 category-only
TITLE resurrecting a stored base skill, P1 ANY-mode durations summed across
alternatives, P1 degree boundary using a narrower head vocabulary -- plus
the head-vocabulary contract, title/description category parity, the
experience subject-mode contract and automation-threshold safety.

Module attributes are used (not `from ... import`) so this file also runs
against the previous implementation and fails per test, not at collection.
"""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.agents.candidate_job_matcher as matcher
import app.agents.requirement_extractor as rx
import app.agents.skill_extractor as sx
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
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
)
from app.services.candidate_preparation import prepare_candidate_job_match

AS_OF = date(2026, 10, 15)


def _profile(skills=(), experiences=(), education=(), projects=()):
    return CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
        skills=[CandidateSkill(id=i, name=name) for i, name in enumerate(skills, 1)],
        experiences=list(experiences),
        education=list(education),
        projects=list(projects),
    )


def _experience(id_, start, end, technologies):
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


def _match(description, profile=None, must=(), title="Developer"):
    job = matcher.JobMatchInput(
        job_id=1, title=title, description=description, must_have_skills=list(must)
    )
    return matcher.compute_match(job, profile or _profile(), company_research_id=None, as_of=AS_OF)


def _all(result):
    return [
        *result.matched_requirements,
        *result.partial_requirements,
        *result.missing_requirements,
        *result.unknown_requirements,
    ]


def _by_norm(result):
    return {r.normalized_requirement: r for r in _all(result)}


def _profil(*lines: str) -> str:
    return "Profil:\n" + "\n".join(f"- {line}" for line in lines)


def _threshold() -> int:
    return Settings.model_fields["automation_shortlist_min_match_score"].default


# --- P1 qualified experience in shared-complement constructions --------------------

PYTHON_PROJECT_ONLY = _profile(
    ["Python"], projects=[CandidateProject(id=1, name="Tool", technologies=["Python"])]
)


@pytest.mark.parametrize(
    "line",
    ["Kenntnisse und Erfahrung in Python", "Knowledge and experience with Python"],
)
def test_bare_coordinated_heads_share_one_complement(line):
    result = _match(_profil(line), PYTHON_PROJECT_ONLY)
    assert {n: r.match_status for n, r in _by_norm(result).items()} == {"python": "MATCH"}
    assert result.overall_score == 100  # a genuine complete match


QUALIFIED = [
    ("Kenntnisse und mehrjährige Erfahrung in Python", "mehrjährige Erfahrung"),
    ("Kenntnisse und praktische Erfahrung in Python", "praktische Erfahrung"),
    ("Berufserfahrung und Kenntnisse in Python", "Berufserfahrung"),
    ("Practical experience and knowledge of Python", "Practical experience"),
    ("Knowledge and practical experience with Python", "practical experience"),
    ("Professional experience and knowledge of Python", "Professional experience"),
    # reversed order
    ("Mehrjährige Erfahrung und Kenntnisse in Python", "Mehrjährige Erfahrung"),
    ("Kenntnisse in Python und Berufserfahrung", "Berufserfahrung"),
    ("Knowledge of Python and practical experience", "practical experience"),
]


@pytest.mark.parametrize(("line", "kept"), QUALIFIED)
def test_qualified_experience_never_disappears_with_a_shared_complement(line, kept):
    """Without professional work: Python MATCH from skill/project evidence,
    the experience qualification REQUIRED/UNKNOWN -- never a false 100."""
    result = _match(_profil(line), PYTHON_PROJECT_ONLY)
    reqs = _by_norm(result)
    assert set(reqs) == {"python", kept.casefold()}
    assert reqs["python"].match_status == "MATCH"
    other = reqs[kept.casefold()]
    assert (other.requirement, other.requirement_type) == (kept, "OTHER")
    assert (other.importance, other.match_status) == ("REQUIRED", "UNKNOWN")
    assert "HUMAN_REVIEW_REQUIRED" in other.reason
    assert (result.required_skill_score, result.experience_support_score) == (50, 100)
    assert result.overall_score == 70
    assert result.overall_score < _threshold()


def test_professional_work_does_not_silently_evaluate_the_qualification():
    profile = _profile(["Python"], [_experience(1, date(2018, 1, 1), None, ["Python"])])
    result = _match(_profil("Kenntnisse und mehrjährige Erfahrung in Python"), profile)
    assert _by_norm(result)["mehrjährige erfahrung"].match_status == "UNKNOWN"


def test_a_typed_duration_owns_its_qualified_experience():
    reqs = _by_norm(_match(_profil("Mindestens 3 Jahre praktische Berufserfahrung mit Python")))
    assert set(reqs) == {"python", "experience-3y"}


# --- P1 category-only TITLE ----------------------------------------------------------


def test_category_only_title_cannot_resurrect_a_stored_base_skill():
    """Codex reproduction: v6 scored 80 via a spurious Python row."""
    profile = _profile(
        ["Git", "Python"], [_experience(1, date(2018, 1, 1), None, ["Git", "Python"])]
    )
    result = _match(
        _profil("Kenntnisse in Git", "Python-Frameworks (z. B. Django, Flask)"),
        profile,
        must=["git", "python"],
        title="Python-Frameworks Engineer",
    )
    assert {n: r.match_status for n, r in _by_norm(result).items()} == {
        "git": "MATCH",
        "group:python-frameworks": "UNKNOWN",
    }
    assert (result.required_skill_score, result.experience_support_score) == (50, 100)
    assert result.overall_score == 70


@pytest.mark.parametrize(
    ("title", "skills"),
    [
        ("Python-Frameworks Engineer", []),
        ("Python Engineer", ["python"]),
        ("Senior Python / Django Engineer", ["django", "python"]),
        ("LLM-Frameworks Engineer", []),
        ("Python-Frameworks / Python Engineer", ["python"]),
    ],
)
def test_title_category_mentions_are_not_independent(title, skills):
    assert sx.extract_skills(title, None).must_have_skills == skills


CATEGORY_TOKENS = [
    ("Python-Frameworks", "python"),
    ("LLM-Frameworks", "llm"),
    ("Java-Frameworks", "java"),
    ("Python-Bibliotheken", "python"),
    ("LLM-Libraries", "llm"),
]


@pytest.mark.parametrize(("token", "base"), CATEGORY_TOKENS)
def test_title_and_description_decide_category_mentions_alike(token, base):
    in_title = base in sx.extract_skills(f"{token} Engineer", None).must_have_skills
    in_description = (
        base in sx.extract_skills(None, _profil(f"Kenntnisse in {token}")).must_have_skills
    )
    assert in_title is in_description is False
    for title, description in (
        (f"{token} Engineer", _profil("Teamfähigkeit")),
        ("Developer", _profil(f"Kenntnisse in {token}")),
    ):
        assert base not in _by_norm(_match(description, must=[base], title=title))
    # an independent mention elsewhere still counts
    independent = _match(_profil("Teamfähigkeit"), must=[base], title=f"{token} / {base} Engineer")
    assert base in _by_norm(independent)


# --- P1 ANY duration per alternative --------------------------------------------------

ALTERNATIVE = _profil("Mindestens 3 Jahre Berufserfahrung mit Python oder Kubernetes")
CONJUNCTIVE = _profil("Mindestens 3 Jahre Berufserfahrung mit Python und Kubernetes")


def _years(description, *experiences, projects=()):
    profile = _profile(["Python", "Kubernetes"], experiences, projects=projects)
    return _by_norm(_match(description, profile))["experience-3y"].match_status


@pytest.mark.parametrize(
    ("rows", "status"),
    [
        ([((2021, 1), (2025, 1), "Python")], "MATCH"),
        ([((2021, 1), (2025, 1), "Kubernetes")], "MATCH"),
        # 2 + 2 separate years of DIFFERENT alternatives never sum to 3
        ([((2019, 1), (2021, 1), "Python"), ((2021, 1), (2023, 1), "Kubernetes")], "PARTIAL"),
        ([((2021, 1), (2023, 1), "Python"), ((2022, 1), (2024, 1), "Kubernetes")], "PARTIAL"),
        # 2 Python rows of the same alternative merge
        ([((2019, 1), (2021, 1), "Python"), ((2021, 1), (2023, 1), "Python")], "MATCH"),
        ([(None, None, "Python"), ((2023, 1), (2025, 1), "Kubernetes")], "UNKNOWN"),
    ],
)
def test_any_duration_is_satisfied_by_one_alternative_alone(rows, status):
    experiences = [
        _experience(
            i,
            date(*start, 1) if start else None,
            date(*end, 1) if end else None,
            [technology],
        )
        for i, (start, end, technology) in enumerate(rows, 1)
    ]
    assert _years(ALTERNATIVE, *experiences) == status


def test_any_duration_never_counts_projects():
    project = CandidateProject(id=1, name="Tool", technologies=["Python", "Kubernetes"])
    assert _years(ALTERNATIVE, projects=[project]) == "MISSING"


def test_all_duration_still_rejects_unrelated_single_subject_roles():
    rows = (
        _experience(1, date(2021, 1, 1), date(2025, 1, 1), ["Python"]),
        _experience(2, date(2021, 1, 1), date(2025, 1, 1), ["Kubernetes"]),
    )
    assert _years(CONJUNCTIVE, *rows) == "UNKNOWN"


@pytest.mark.parametrize(
    ("line", "subjects", "mode"),
    [
        ("3 Jahre Berufserfahrung mit Python und Kubernetes", ("python", "kubernetes"), "ALL"),
        ("3 Jahre Berufserfahrung mit Python oder Kubernetes", ("python", "kubernetes"), "ANY"),
        ("3 Jahre Berufserfahrung mit Python in Kubernetes-Umgebungen", ("python", "kubernetes"),
         "AMBIGUOUS"),
        ("3 Jahre Berufserfahrung mit Python und Erfahrung mit Kubernetes", ("python",), "ALL"),
        ("3 Jahre Berufserfahrung mit Python (Erfahrung mit Kubernetes)", ("python",), "ALL"),
    ],
)  # fmt: skip
def test_experience_subject_mode_contract(line, subjects, mode):
    requirement = rx.extract_experience_requirement(None, _profil(line))
    assert (requirement.subjects, requirement.subject_mode) == (subjects, mode)


def test_split_alternative_years_cannot_match():
    """Reproduction C: 2 Python + 2 Kubernetes years, 3 years Python OR
    Kubernetes -- PARTIAL credit only (v6: MATCH, overall 100)."""
    profile = _profile(
        ["Python", "Kubernetes"],
        [
            _experience(1, date(2019, 1, 1), date(2021, 1, 1), ["Python"]),
            _experience(2, date(2021, 1, 1), date(2023, 1, 1), ["Kubernetes"]),
        ],
    )
    result = _match(ALTERNATIVE, profile)
    assert _by_norm(result)["experience-3y"].match_status == "PARTIAL"
    assert result.experience_support_score == 50
    assert result.overall_score < 100


# --- P1 degree boundary uses the full head vocabulary -----------------------------------

MATHE = "Abgeschlossenes Studium der Mathematik"


@pytest.mark.parametrize(
    "line",
    [
        f"{MATHE} und proficiency in Computer Science",
        f"{MATHE} und Kenntnisse in Informatik",  # 0 modifiers
        f"{MATHE} und gute Kenntnisse in Informatik",  # 1
        f"{MATHE} und sehr gute Kenntnisse in Informatik",  # 2
        f"{MATHE} und sehr gute praktische Kenntnisse in Informatik",  # 3
        f"{MATHE} und idealerweise sehr gute praktische Kenntnisse in Informatik",  # 4
        f"{MATHE} and strong practical knowledge of Computer Science",
        f"{MATHE} and a solid understanding of Informatik",
        f"{MATHE} and good skills in Computer Science",
        f"{MATHE} and deep knowledge of Informatik",
        f"{MATHE}, sehr gute Kenntnisse in Informatik",
        f"{MATHE}; proficiency in Computer Science",
        f"{MATHE} (sehr gute Kenntnisse in Informatik)",
        f"{MATHE} sowie ausgeprägte Fachkenntnisse in Informatik",
        "Proficiency in Computer Science und ein abgeschlossenes Studium der Mathematik",
        "Sehr gute praktische Kenntnisse in Informatik, abgeschlossenes Studium der Mathematik",
    ],
)
def test_degree_fields_stop_at_any_knowledge_clause(line):
    assert rx.extract_education_requirement(None, _profil(line)).fields == ("Mathematik",)


@pytest.mark.parametrize(
    ("line", "fields"),
    [
        ("Abgeschlossenes Studium der Informatik und Mathematik", ("Informatik", "Mathematik")),
        ("Bachelor- und Masterabschluss in Informatik", ("Informatik",)),
        ("Bachelor- oder Masterabschluss in Informatik oder Mathematik",
         ("Informatik", "Mathematik")),
    ],
)  # fmt: skip
def test_compound_degrees_do_not_split(line, fields):
    assert rx.extract_education_requirement(None, _profil(line)).fields == fields


def test_cs_degree_never_satisfies_a_mathematics_degree_with_separate_cs_proficiency():
    result = _match(
        _profil(f"{MATHE} und proficiency in Computer Science"),
        _profile(education=[_degree("Computer Science")]),
    )
    reqs = _by_norm(result)
    assert reqs["completed-degree"].match_status == "UNKNOWN"
    assert reqs["computer science"].requirement_type == "OTHER"


def test_cs_proficiency_cannot_lift_a_mathematics_degree_over_the_threshold():
    description = _profil(
        f"{MATHE} und sehr gute praktische Kenntnisse in Informatik",
        "Kenntnisse in Python",
        "Kenntnisse in Git",
        "Kenntnisse in Docker",
    )
    skills = ["Python", "Git", "Docker"]
    work = _experience(1, date(2018, 1, 1), None, skills)
    mathe = _match(description, _profile(skills, [work], education=[_degree("Mathematik")]))
    informatik = _match(description, _profile(skills, [work], education=[_degree("Informatik")]))
    assert mathe.overall_score >= _threshold()
    assert _by_norm(informatik)["completed-degree"].match_status != "MATCH"
    assert informatik.overall_score < _threshold()


# --- head-vocabulary contract -----------------------------------------------------------

HEADS = [
    "Erfahrung", "Berufserfahrung", "Praxiserfahrung", "Kenntnisse", "Fachkenntnisse",
    "Verständnis", "Studium", "Abschluss", "experience", "professional experience",
    "practical experience", "knowledge", "proficiency", "understanding", "skills", "degree",
]  # fmt: skip


@pytest.mark.parametrize("head", HEADS)
def test_every_head_is_one_concept_across_extractors_and_residual_net(head):
    """One vocabulary: what the residual net treats as a requirement head,
    the extractors treat as an independent-clause boundary -- after any
    number of modifiers."""
    assert matcher.REQUIREMENT_HEAD is rx.REQUIREMENT_HEAD
    assert matcher._is_head(head.split()[-1])
    for modifiers in ("", "sehr gute ", "sehr gute praktische ", "idealerweise sehr gute "):
        assert rx.starts_requirement_clause(f"{modifiers}{head} in X")
        degree = rx.extract_education_requirement(
            None, _profil(f"{MATHE} und {modifiers}{head} in Informatik")
        )
        assert degree.fields == ("Mathematik",)
        duration = rx.extract_experience_requirement(
            None, _profil(f"3 Jahre Berufserfahrung mit Python und {modifiers}{head} in Kubernetes")
        )
        assert duration.subjects == ("python",)


@pytest.mark.parametrize(
    "segment",
    [
        f"{MATHE} und sehr gute Kenntnisse in Informatik",
        "Deutsch B2, mehrjährige Berufserfahrung; Kenntnisse in Python (Erfahrung mit Docker)",
        "Kenntnisse und praktische Erfahrung in Python sowie proficiency in Go",
    ],
)
def test_every_independent_boundary_is_a_residual_boundary(segment):
    residual = [m.span() for m in matcher._RESIDUAL_CLAUSE_BOUNDARY.finditer(segment)]
    for start, end in rx.independent_boundaries(segment):
        assert any(r_start < end and start < r_end for r_start, r_end in residual), (start, end)


@pytest.mark.parametrize(
    "text",
    ["Mathematik", "Python", "vergleichbare Qualifikation", "in der Softwareentwicklung"],
)
def test_non_heads_never_start_a_requirement_clause(text):
    assert not rx.starts_requirement_clause(text)


# --- v7 cache identity -------------------------------------------------------------------


def test_v6_row_is_kept_and_cache_lookup_uses_v7():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v7",
            source="bundesagentur",
            title="Developer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("Kenntnisse und mehrjährige Erfahrung in Python"),
            skills_json="[]",
            data_confidence=0.9,
            must_have_skills_json=json.dumps(["python"]),
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
        v6_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v6",
            overall_score=100,
            coverage_score=100,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=100,
        )
        create_match(db, v6_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert matcher.ALGORITHM_VERSION == "v7"
        assert result.algorithm_version == "v7"
        assert "mehrjährige Erfahrung" in [r.requirement for r in result.unknown_requirements]
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v6", "v7"]
