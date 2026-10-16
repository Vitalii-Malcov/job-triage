"""Stage 14B Codex v7 review reproductions (matcher v8): head-anchored
clauses.

P1 qualified experience depending on a qualifier whitelist, P1 an
unfamiliar modifier hiding a knowledge clause from the degree boundary, P1
a title-only category suppressed as a base skill without a replacement
requirement -- plus the head-vocabulary contract with modifiers that no
list contains, compound-degree safety, title/description category dedup
and automation-threshold safety.

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


def _work(technologies):
    return CandidateExperience(
        id=1,
        company="Company",
        job_title="Developer",
        start_date=date(2018, 1, 1),
        is_current=True,
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


def _statuses(result):
    return {n: r.match_status for n, r in _by_norm(result).items()}


def _profil(*lines: str) -> str:
    return "Profil:\n" + "\n".join(f"- {line}" for line in lines)


def _threshold() -> int:
    return Settings.model_fields["automation_shortlist_min_match_score"].default


def _not_whitelisted(phrase: str) -> None:
    """The test modifier is deliberately absent from any modifier list the
    implementation might keep (v7 had `CLAUSE_MODIFIER`)."""
    modifier_list = getattr(rx, "CLAUSE_MODIFIER", None)
    if modifier_list is not None:
        assert not all(modifier_list.fullmatch(word) for word in phrase.split()), phrase


# --- P1 qualified experience without a qualifier whitelist ----------------------------

PYTHON_PROJECT_ONLY = _profile(
    ["Python"], projects=[CandidateProject(id=1, name="Tool", technologies=["Python"])]
)

QUALIFIED = [
    ("Kenntnisse und fundierte Erfahrung in Python", "fundierte Erfahrung"),
    ("Kenntnisse und solide Erfahrung in Python", "solide Erfahrung"),
    ("Kenntnisse und gute Erfahrung in Python", "gute Erfahrung"),
    ("Kenntnisse und relevante Erfahrung in Python", "relevante Erfahrung"),
    ("Kenntnisse und umfangreiche Erfahrung in Python", "umfangreiche Erfahrung"),
    ("Kenntnisse und blorpige Erfahrung in Python", "blorpige Erfahrung"),  # no list has it
    ("Knowledge and strong experience with Python", "strong experience"),
    ("Knowledge and solid experience with Python", "solid experience"),
    ("Knowledge and significant experience with Python", "significant experience"),
    ("Fundierte Erfahrung und Kenntnisse in Python", "Fundierte Erfahrung"),  # reversed
]


@pytest.mark.parametrize(("line", "kept"), QUALIFIED)
def test_any_attached_modifier_qualifies_experience(line, kept):
    """Python MATCH from skill/project evidence, the qualified experience a
    separate REQUIRED/UNKNOWN requirement -- never a false 100."""
    result = _match(_profil(line), PYTHON_PROJECT_ONLY)
    reqs = _by_norm(result)
    assert set(reqs) == {"python", kept.casefold()}
    assert reqs["python"].match_status == "MATCH"
    other = reqs[kept.casefold()]
    assert (other.requirement, other.requirement_type) == (kept, "OTHER")
    assert (other.importance, other.match_status) == ("REQUIRED", "UNKNOWN")
    assert "HUMAN_REVIEW_REQUIRED" in other.reason
    assert result.overall_score == 70
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("Kenntnisse und langjährige Erfahrung in Python", "langjährige Erfahrung"),
        ("Kenntnisse und sehr gute praktische Erfahrung in Python",
         "sehr gute praktische Erfahrung"),
        ("Professional experience and knowledge of Python", "Professional experience"),
        ("Berufserfahrung und Kenntnisse in Python", "Berufserfahrung"),
        ("Praxiserfahrung und Kenntnisse in Python", "Praxiserfahrung"),
        ("Knowledge of Python and practical experience", "practical experience"),
    ],
)  # fmt: skip
def test_previously_qualified_and_lexicalized_experience_stays_material(line, kept):
    reqs = _by_norm(_match(_profil(line), PYTHON_PROJECT_ONLY))
    assert set(reqs) == {"python", kept.casefold()}


@pytest.mark.parametrize(
    "line",
    [
        "Kenntnisse und Erfahrung in Python",
        "Kenntnisse und Erfahrung mit Python",
        "Knowledge and experience with Python",
        "Gute Kenntnisse und Erfahrung in Python",  # the modifier qualifies KNOWLEDGE
        "Kenntnisse und eine Erfahrung in Python",  # an article is not a qualifier
    ],
)
def test_bare_experience_still_shares_the_complement(line):
    result = _match(_profil(line), PYTHON_PROJECT_ONLY)
    assert _statuses(result) == {"python": "MATCH"}
    assert result.overall_score == 100


def test_qualified_experience_threshold_safety():
    """A: Python skill + project, no professional work, never 100."""
    result = _match(_profil("Kenntnisse und fundierte Erfahrung in Python"), PYTHON_PROJECT_ONLY)
    assert result.overall_score != 100
    assert result.overall_score < _threshold()
    assert result.relevant_experiences == []


def test_a_typed_duration_still_owns_its_qualified_experience():
    reqs = _by_norm(_match(_profil("Mindestens 3 Jahre Berufserfahrung mit Python")))
    assert set(reqs) == {"python", "experience-3y"}


# --- P1 degree boundary with unfamiliar modifiers ---------------------------------------

MATHE = "Abgeschlossenes Studium der Mathematik"

UNFAMILIAR = [
    "und umfangreiche Kenntnisse in Informatik",
    "und exzellente Kenntnisse in Informatik",
    "und fortgeschrittene Kenntnisse in Informatik",
    "und außerordentlich gute Kenntnisse in Informatik",
    "und umfangreiche praktische Kenntnisse in Informatik",
    "und fortgeschrittene technische Kenntnisse in Informatik",
    "und blorpige zwirbelige Kenntnisse in Informatik",
    "and excellent practical knowledge of Computer Science",
    "and advanced working proficiency in Computer Science",
    "and strong domain knowledge of Computer Science",
    "and advanced professional knowledge of Computer Science",
    ", umfangreiche Kenntnisse in Informatik",
    "; exzellente Kenntnisse in Informatik",
    "(umfangreiche Kenntnisse in Informatik)",
    "sowie fortgeschrittene Fachkenntnisse in Informatik",
]


@pytest.mark.parametrize("tail", UNFAMILIAR)
def test_degree_fields_stop_at_a_knowledge_clause_with_unfamiliar_modifiers(tail):
    line = f"{MATHE}{'' if tail[0] in ',;' else ' '}{tail}"
    assert rx.extract_education_requirement(None, _profil(line)).fields == ("Mathematik",)


@pytest.mark.parametrize(
    "tail",
    [
        "und umfangreiche Kenntnisse in Informatik",
        "und exzellente Kenntnisse in Informatik",
        "und fortgeschrittene Kenntnisse in Informatik",
    ],
)
def test_informatik_degree_never_matches_a_mathematics_degree(tail):
    """B: the knowledge clause is its own REQUIRED/UNKNOWN requirement and
    the Informatik degree is not a definite MATCH for Mathematik."""
    description = _profil(f"{MATHE} {tail}", "Kenntnisse in Python", "Kenntnisse in Git")
    skills = ["Python", "Git"]
    result = _match(description, _profile(skills, [_work(skills)], [_degree("Informatik")]))
    reqs = _by_norm(result)
    degree = reqs["completed-degree"]
    assert degree.requirement == "Completed degree (Mathematik or comparable)"
    assert degree.match_status == "UNKNOWN"
    knowledge = [r for r in reqs.values() if r.requirement_type == "OTHER"]
    assert len(knowledge) == 1 and "Informatik" in knowledge[0].requirement
    assert (knowledge[0].importance, knowledge[0].match_status) == ("REQUIRED", "UNKNOWN")
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    ("line", "fields"),
    [
        ("Bachelor- und Masterabschluss in Informatik", ("Informatik",)),
        ("Abgeschlossenes Studium der Informatik und Mathematik", ("Informatik", "Mathematik")),
        ("Bachelor oder Master in Informatik", ("Informatik",)),
        ("Abgeschlossenes Studium der Informatik oder Wirtschaftsinformatik",
         ("Wirtschaftsinformatik", "Informatik")),
        ("Abgeschlossenes Studium der Informatik, Mathematik oder vergleichbarer Abschluss",
         ("Informatik", "Mathematik")),
    ],
)  # fmt: skip
def test_compound_degrees_do_not_split(line, fields):
    assert rx.extract_education_requirement(None, _profil(line)).fields == fields


# --- head-vocabulary contract: unfamiliar modifiers -------------------------------------

HEADS = [
    "Erfahrung", "Berufserfahrung", "Praxiserfahrung", "Kenntnisse", "Fachkenntnisse",
    "Verständnis", "experience", "knowledge", "proficiency", "understanding", "skills",
    "Studium", "Abschluss", "degree",
]  # fmt: skip
UNLISTED_MODIFIERS = [
    "umfangreiche",
    "außerordentlich gute",
    "fortgeschrittene technische",
    "excellent practical",
    "advanced working",
    "strong domain",
    "blorpige zwirbelige",
]


@pytest.mark.parametrize("head", HEADS)
def test_a_known_head_anchors_its_clause_whatever_modifies_it(head):
    for modifiers in UNLISTED_MODIFIERS:
        _not_whitelisted(modifiers)
        assert rx.starts_requirement_clause(f"{modifiers} {head} in X"), modifiers
        degree = rx.extract_education_requirement(
            None, _profil(f"{MATHE} und {modifiers} {head} in Informatik")
        )
        assert degree.fields == ("Mathematik",), modifiers
        line = f"3 Jahre Berufserfahrung mit Python und {modifiers} {head} in Kubernetes"
        duration = rx.extract_experience_requirement(None, _profil(line))
        assert duration.subjects == ("python",), modifiers


@pytest.mark.parametrize(
    "text",
    [
        "in der Softwareentwicklung und Kenntnisse",  # a preposition: someone's complement
        "Mathematik oder Kenntnisse",  # a conjunction ends the scan
        "Mathematik, Kenntnisse",  # a hard separator ends the scan
        "Mathematik",
        "vergleichbare Qualifikation",
    ],
)
def test_head_scan_is_bounded_and_fails_closed(text):
    assert not rx.starts_requirement_clause(text)


# --- P1 title-only category ----------------------------------------------------------

GIT_AND_PYTHON_WORK = _profile(["Git", "Python"], [_work(["Git", "Python"])])


def test_title_only_category_becomes_a_required_requirement():
    """C: Codex reproduction -- v7 returned Git only, overall 100."""
    result = _match(
        _profil("Kenntnisse in Git"),
        GIT_AND_PYTHON_WORK,
        must=["python", "git"],
        title="Python-Frameworks Engineer",
    )
    reqs = _by_norm(result)
    assert _statuses(result) == {"git": "MATCH", "group:python-frameworks": "UNKNOWN"}
    group = reqs["group:python-frameworks"]
    assert (group.requirement, group.requirement_type, group.importance) == (
        "Python-Frameworks",
        "SKILL",
        "REQUIRED",
    )
    assert group.job_evidence == ["Python-Frameworks Engineer"]
    assert "HUMAN_REVIEW_REQUIRED" in group.reason
    assert result.overall_score != 100
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    ("token", "base"),
    [
        ("Python-Frameworks", "Python"),
        ("LLM-Frameworks", "LLM"),
        ("Java-Frameworks", "Java"),
        ("Python-Bibliotheken", "Python"),
        ("LLM-Libraries", "LLM"),
    ],
)
def test_category_titles_without_examples_are_never_satisfied_by_the_base(token, base):
    result = _match(
        _profil("Teamfähigkeit"),
        _profile([base], [_work([base])]),
        must=[base.casefold()],
        title=f"{token} Engineer",
    )
    reqs = _by_norm(result)
    key = f"group:{token.casefold()}"
    assert base.casefold() not in reqs
    assert (reqs[key].importance, reqs[key].match_status) == ("REQUIRED", "UNKNOWN")


def test_title_category_without_examples_has_no_safe_rule_for_a_framework_skill():
    result = _match(
        _profil("Teamfähigkeit"), _profile(["Django"]), title="Python-Frameworks Engineer"
    )
    assert _by_norm(result)["group:python-frameworks"].match_status == "UNKNOWN"


@pytest.mark.parametrize(
    "category_line",
    [
        "Python-Frameworks (z. B. Django, Flask)",
        "Kenntnisse in modernen Python-Frameworks (z. B. Django, Flask)",
    ],
)
def test_title_and_description_category_deduplicate(category_line):
    title = "Python-Frameworks Engineer"
    description = _profil("Kenntnisse in Git", category_line)
    result = _match(description, GIT_AND_PYTHON_WORK, must=["python", "git"], title=title)
    groups = [r for r in _all(result) if r.normalized_requirement.startswith("group:")]
    assert len(groups) == 1
    group = groups[0]
    assert group.requirement.endswith("(examples: Django, Flask)")
    assert group.importance == "REQUIRED"
    assert set(group.job_evidence) == {title, category_line}
    assert set(_by_norm(result)) == {"git", group.normalized_requirement}
    # the description's examples still define a safe MATCH
    with_django = _profile(["Git", "Django"], [_work(["Git", "Django"])])
    assert _match(description, with_django, title=title).overall_score == 100


def test_title_category_makes_a_preferred_description_category_required():
    result = _match(
        _profil("Kenntnisse in Git", "Python-Frameworks (z. B. Django) sind von Vorteil"),
        GIT_AND_PYTHON_WORK,
        title="Python-Frameworks Engineer",
    )
    assert _by_norm(result)["group:python-frameworks"].importance == "REQUIRED"


def test_bare_description_category_and_title_are_one_requirement():
    result = _match(
        _profil("Kenntnisse in Git", "Kenntnisse in Python-Frameworks"),
        GIT_AND_PYTHON_WORK,
        must=["python", "git"],
        title="Python-Frameworks Engineer",
    )
    assert _statuses(result) == {"git": "MATCH", "group:python-frameworks": "UNKNOWN"}


@pytest.mark.parametrize(
    "title",
    ["Python-Frameworks / Python Engineer", "Python and Python-Frameworks Engineer"],
)
def test_title_category_never_erases_an_independent_base(title):
    # stored lists as collection extracts them (title + description)
    result = _match(
        _profil("Kenntnisse in Git"), GIT_AND_PYTHON_WORK, must=["git", "python"], title=title
    )
    assert _statuses(result) == {
        "python": "MATCH",
        "git": "MATCH",
        "group:python-frameworks": "UNKNOWN",
    }


def test_python_title_is_still_a_python_requirement():
    result = _match(
        _profil("Kenntnisse in Git"),
        GIT_AND_PYTHON_WORK,
        must=["git", "python"],
        title="Python Engineer",
    )
    assert _statuses(result) == {"python": "MATCH", "git": "MATCH"}


# --- v8 cache identity -------------------------------------------------------------------


def test_v7_row_is_kept_and_cache_lookup_uses_the_current_version():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v8",
            source="bundesagentur",
            title="Python-Frameworks Engineer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("Kenntnisse in Git"),
            skills_json="[]",
            data_confidence=0.9,
            must_have_skills_json=json.dumps(["git", "python"]),
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
        v7_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v7",
            overall_score=100,
            coverage_score=100,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=100,
        )
        create_match(db, v7_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert result.algorithm_version == matcher.ALGORITHM_VERSION
        assert "Python-Frameworks" in [r.requirement for r in result.unknown_requirements]
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v7", matcher.ALGORITHM_VERSION]
