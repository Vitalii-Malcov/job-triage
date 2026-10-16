"""Stage 14B final Codex v4 review reproductions (matcher v5).

P1 non-skill mixed lines, P1 domain/product residual, P1 experience
clause binding, P1 technology-prefixed group categories, P2 fresh
importance over stale stored extraction, P3 OTHER terminal-punctuation
dedup -- plus the no-silent-loss invariant, automation-threshold safety
and the v5 cache identity.
"""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.candidate_job_matcher import ALGORITHM_VERSION, JobMatchInput, compute_match
from app.agents.requirement_extractor import extract_experience_requirement
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


def _others(result):
    return [r.requirement for r in _all(result) if r.requirement_type == "OTHER"]


def _types(result):
    return {r.requirement_type for r in _all(result)}


def _by_norm(result):
    return {r.normalized_requirement: r for r in _all(result)}


def _profil(line: str) -> str:
    return f"Profil:\n- {line}"


# --- P1 non-skill mixed lines ----------------------------------------------------


@pytest.mark.parametrize(
    ("line", "typed"),
    [
        ("Deutsch B2 und Erfahrung mit SAP ABAP", "LANGUAGE"),
        ("Abgeschlossenes Studium der Informatik und Kenntnisse in SAP ABAP", "EDUCATION"),
        ("Mindestens 3 Jahre Berufserfahrung mit Python und Erfahrung mit SAP ABAP", "EXPERIENCE"),
    ],
)
def test_typed_requirement_keeps_the_unknown_secondary_clause(line, typed):
    result = _match(_profil(line))
    assert typed in _types(result)
    other = [r for r in _all(result) if r.requirement_type == "OTHER"]
    assert [(o.requirement, o.importance, o.match_status) for o in other] == [
        ("SAP ABAP", "REQUIRED", "UNKNOWN")
    ]
    assert "HUMAN_REVIEW_REQUIRED" in other[0].reason
    assert other[0].job_evidence == [line]


def test_language_line_reports_the_language_requirement():
    result = _match(_profil("Deutsch B2 und Erfahrung mit SAP ABAP"))
    assert _by_norm(result)["german-b2"].requirement_type == "LANGUAGE"


def test_experience_line_binds_years_to_python_only():
    result = _match(
        _profil("Mindestens 3 Jahre Berufserfahrung mit Python und Erfahrung mit SAP ABAP")
    )
    assert "with python" in _by_norm(result)["experience-3y"].requirement


@pytest.mark.parametrize(
    "line",
    [
        "Deutsch B2 und Erfahrung mit Docker",
        "Abgeschlossenes Studium der Informatik und Kenntnisse in Docker",
        "Mindestens 3 Jahre Berufserfahrung mit Python und Erfahrung mit Docker",
    ],
)
def test_recognized_secondary_skill_is_evaluated_without_an_other(line):
    result = _match(_profil(line))
    assert _by_norm(result)["docker"].importance == "REQUIRED"
    assert _others(result) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Deutsch B2 und Erfahrung im Bankwesen", ["Bankwesen"]),
        ("Abgeschlossenes Studium der Elektrotechnik", ["Elektrotechnik"]),
        (
            "Mindestens 3 Jahre Berufserfahrung mit Python sowie Kenntnisse im Bankwesen",
            ["Bankwesen"],
        ),
    ],
)
def test_unrecognized_secondary_domain_is_kept(line, expected):
    assert _others(_match(_profil(line))) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Erfahrung mit SAP ABAP sowie Deutsch auf C1-Niveau", ["SAP ABAP"]),
        ("Erfahrung mit SAP ABAP, Deutsch B2", ["SAP ABAP"]),
        ("Deutsch (B2); SAP ABAP-Kenntnisse", ["SAP ABAP-Kenntnisse"]),
        ("SAP ABAP, fließende Deutschkenntnisse (C1).", ["SAP ABAP"]),
        ("Kenntnisse in SAP ABAP und ein abgeschlossenes Studium der Informatik.", ["SAP ABAP"]),
        ("Erfahrung mit SAP ABAP und mindestens 3 Jahre Berufserfahrung mit Python.", ["SAP ABAP"]),
        ("3+ years of experience with Python and experience with SAP ABAP", ["SAP ABAP"]),
    ],
)
def test_punctuation_and_order_variants(line, expected):
    assert _others(_match(_profil(line))) == expected


@pytest.mark.parametrize(
    "line",
    [
        "Deutsch B2",
        "Fließende Deutschkenntnisse (mindestens C1).",
        "Abgeschlossenes Studium der Informatik, Wirtschaftsinformatik oder vergleichbare "
        "Qualifikation.",
        "Bachelor- oder Masterabschluss in Informatik oder einem verwandten Studiengang",
        "Mindestens 3 Jahre Berufserfahrung mit Python.",
        "Mindestens drei Jahre Berufserfahrung im genannten Aufgabenbereich.",
    ],
)
def test_fully_evaluated_typed_line_has_no_duplicate_other(line):
    assert _others(_match(_profil(line))) == []


# --- P1 domain/product residual --------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Erfahrung mit Python in der Softwareentwicklung", ["Softwareentwicklung"]),
        (
            "Kenntnisse in Git und professionelle Softwareentwicklung",
            ["professionelle Softwareentwicklung"],
        ),
        ("Kenntnisse in SAP ABAP (Python)", ["SAP ABAP"]),
        ("Kenntnisse in proprietärer XYZ-Plattform (Python)", ["proprietärer XYZ-Plattform"]),
        ("Experience with Python in software development", ["software development"]),
    ],
)
def test_domains_and_products_next_to_a_technology_are_kept(line, expected):
    assert _others(_match(_profil(line), _profile(["Python", "Git"]))) == expected


def test_only_a_technology_own_category_noun_is_consumed():
    # "API-Entwicklung (REST)" names REST's own category -- one requirement.
    assert _others(_match(_profil("Erfahrung mit API-Entwicklung (REST)"))) == []


def test_python_frameworks_token_is_not_the_python_technology():
    result = _match(_profil("Erfahrung mit Docker und Python-Frameworks"), _profile(["Python"]))
    assert _others(result) == ["Python-Frameworks"]


# --- P1 experience clause binding -----------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Mindestens 3 Jahre Berufserfahrung mit Python und Erfahrung mit Kubernetes",
        "Mindestens 3 Jahre Berufserfahrung mit Python sowie Erfahrung mit Kubernetes",
        "Mindestens 3 Jahre Berufserfahrung mit Python und gute Kenntnisse in Kubernetes",
        "3+ years of experience with Python and experience with Kubernetes",
        "3+ years of experience with Python plus experience with Kubernetes",
    ],
)
def test_duration_binds_only_to_its_own_clause(line):
    requirement = extract_experience_requirement(None, _profil(line))
    assert requirement is not None and requirement.years == 3
    assert requirement.subjects == ("python",)


KUBERNETES_LINE = _profil(
    "Mindestens 3 Jahre Berufserfahrung mit Python und Erfahrung mit Kubernetes"
)


def test_kubernetes_only_years_never_satisfy_python_years():
    profile = _profile(
        ["Python", "Kubernetes"],
        [_experience(1, date(2021, 1, 1), date(2025, 1, 1), ["Kubernetes"])],
    )
    result = _match(KUBERNETES_LINE, profile)
    reqs = _by_norm(result)
    assert reqs["experience-3y"].match_status == "MISSING"
    assert reqs["kubernetes"].importance == "REQUIRED"


def test_python_years_satisfy_the_python_duration():
    profile = _profile(
        ["Python", "Kubernetes"],
        [_experience(1, date(2021, 1, 1), date(2025, 1, 1), ["Python"])],
    )
    assert _by_norm(_match(KUBERNETES_LINE, profile))["experience-3y"].match_status == "MATCH"


def test_python_without_dates_is_unknown():
    profile = _profile(["Python"], [_experience(1, None, None, ["Python"])])
    assert _by_norm(_match(KUBERNETES_LINE, profile))["experience-3y"].match_status == "UNKNOWN"


def test_python_projects_are_not_professional_years():
    profile = _profile(
        ["Python"], projects=[CandidateProject(id=1, name="Tool", technologies=["Python"])]
    )
    assert _by_norm(_match(KUBERNETES_LINE, profile))["experience-3y"].match_status == "MISSING"


# --- P1 technology-prefixed group categories ------------------------------------


PYTHON_FRAMEWORKS = _profil("Kenntnisse in Python-Frameworks (z. B. Django, Flask)")
LLM_FRAMEWORKS = _profil("Erfahrung mit LLM-Frameworks (z. B. LangChain, LlamaIndex)")


def test_python_frameworks_is_one_group_with_its_examples():
    result = _match(PYTHON_FRAMEWORKS, _profile(["Python"]))
    reqs = _by_norm(result)
    group = reqs["group:python-frameworks"]
    assert group.requirement == "Python-Frameworks (examples: Django, Flask)"
    assert (group.importance, group.match_status) == ("REQUIRED", "UNKNOWN")
    assert "django" not in reqs and "flask" not in reqs
    assert _others(result) == []


def test_python_only_never_satisfies_the_framework_group():
    result = _match(PYTHON_FRAMEWORKS, _profile(["Python"]))
    assert _by_norm(result)["group:python-frameworks"].match_status != "MATCH"


def test_a_named_framework_example_satisfies_the_group():
    result = _match(PYTHON_FRAMEWORKS, _profile(["Django"]))
    assert _by_norm(result)["group:python-frameworks"].match_status == "MATCH"


def test_llm_only_never_satisfies_the_llm_framework_group():
    reqs = _by_norm(_match(LLM_FRAMEWORKS, _profile(["LLM"])))
    assert reqs["group:llm-frameworks"].match_status == "UNKNOWN"
    assert "langchain" not in reqs and "llamaindex" not in reqs


def test_langchain_is_example_evidence_for_the_llm_framework_group():
    reqs = _by_norm(_match(LLM_FRAMEWORKS, _profile(["LangChain"])))
    group = reqs["group:llm-frameworks"]
    assert group.match_status == "MATCH"
    assert [ref.value for ref in group.candidate_evidence] == ["LangChain"]


def test_a_bare_technology_category_is_still_not_a_group():
    reqs = _by_norm(_match(_profil("Kenntnisse in Python (z. B. Django)"), _profile(["Python"])))
    assert not any(norm.startswith("group:") for norm in reqs)
    assert reqs["python"].match_status == "MATCH"


# --- P2 fresh importance over stale stored extraction ----------------------------


STALE_GROUP_DESCRIPTION = (
    "Profil:\n"
    "- Kenntnisse in modernen KI-Frameworks (z. B. LangChain, LlamaIndex)\n"
    "\n"
    "Wünschenswert:\n"
    "- LangChain\n"
)


def test_stale_stored_must_have_never_promotes_a_preferred_mention():
    result = _match(STALE_GROUP_DESCRIPTION, must=["langchain", "llamaindex"])
    reqs = _by_norm(result)
    assert reqs["group:modernen ki-frameworks"].importance == "REQUIRED"
    assert reqs["langchain"].importance == "PREFERRED"
    assert "llamaindex" not in reqs


def test_current_required_explicit_mention_stays_required():
    description = (
        "Profil:\n"
        "- Erfahrung mit LangChain\n"
        "- Kenntnisse in modernen KI-Frameworks (z. B. LangChain, LlamaIndex)\n"
    )
    reqs = _by_norm(_match(description, must=["langchain", "llamaindex"]))
    assert reqs["langchain"].importance == "REQUIRED"
    assert "llamaindex" not in reqs


def test_stale_stored_must_have_yields_to_current_preferred_skill():
    reqs = _by_norm(_match("Wünschenswert:\n- Docker", must=["docker"]))
    assert reqs["docker"].importance == "PREFERRED"


def test_title_named_stored_must_have_stays_required():
    reqs = _by_norm(_match("Wünschenswert:\n- Docker", must=["docker"], title="Docker Engineer"))
    assert reqs["docker"].importance == "REQUIRED"


def test_stored_skill_absent_from_current_text_is_still_used():
    reqs = _by_norm(_match("Profil:\n- Teamfähigkeit", must=["terraform"]))
    assert reqs["terraform"].importance == "REQUIRED"


# --- P3 OTHER terminal punctuation dedup ------------------------------------------


PUNCTUATION_VARIANTS = [
    "Kenntnisse in SAP ABAP",
    "Kenntnisse in SAP ABAP.",
    "Kenntnisse in SAP ABAP!",
    "Kenntnisse in SAP ABAP?",
]


def test_terminal_punctuation_variants_are_one_other_requirement():
    description = "Profil:\n" + "\n".join(f"- {line}" for line in PUNCTUATION_VARIANTS)
    single = _match(_profil("Kenntnisse in SAP ABAP"), _profile(["Python"]))
    repeated = _match(description, _profile(["Python"]))
    other = [r for r in _all(repeated) if r.requirement_type == "OTHER"]
    assert len(other) == 1
    assert other[0].job_evidence == PUNCTUATION_VARIANTS
    assert repeated.required_skill_score == single.required_skill_score
    assert repeated.coverage_score == single.coverage_score


def test_internal_punctuation_still_distinguishes_requirements():
    description = "Profil:\n- Kenntnisse in SAP-ABAP\n- Kenntnisse in SAP ABAP"
    assert len(_others(_match(description))) == 2


# --- no-silent-loss invariant ------------------------------------------------------


TYPED_PARTS = [
    "Kenntnisse in Python",
    "Deutsch B2",
    "fließende Deutschkenntnisse (C1)",
    "abgeschlossenes Studium der Informatik",
    "mindestens 3 Jahre Berufserfahrung mit Python",
    "mindestens 3 Jahre Berufserfahrung",
    "Kenntnisse in KI-Frameworks (z. B. LangChain)",
    "Python-Frameworks (z. B. Django)",
]
UNKNOWN_PART = "Erfahrung mit Zorblax-Plattform"
CONNECTORS = [" und ", ", ", " sowie ", "; "]


def _represented(result, word: str) -> bool:
    return any(
        word in r.requirement and r.importance == "REQUIRED" and r.match_status == "UNKNOWN"
        for r in _all(result)
    )


@pytest.mark.parametrize("typed", TYPED_PARTS)
@pytest.mark.parametrize("connector", CONNECTORS)
@pytest.mark.parametrize("unknown_first", [False, True])
def test_no_material_clause_disappears_from_a_required_segment(typed, connector, unknown_first):
    """Invariant: every material clause of a required segment is either a
    typed evaluated requirement or a REQUIRED/UNKNOWN one -- recognizing
    SOME part never hides the rest."""
    parts = [UNKNOWN_PART, typed] if unknown_first else [typed, UNKNOWN_PART]
    result = _match(_profil(connector.join(parts) + "."))
    assert _represented(result, "Zorblax-Plattform")


@pytest.mark.parametrize("typed", TYPED_PARTS)
def test_fully_typed_segment_has_no_other(typed):
    assert _others(_match(_profil(typed + "."))) == []


# --- automation shortlist safety ---------------------------------------------------


def _threshold() -> int:
    return Settings.model_fields["automation_shortlist_min_match_score"].default


PYTHON_WORK = _experience(1, date(2018, 1, 1), None, ["Python"])


@pytest.mark.parametrize(
    ("complete", "mixed", "profile"),
    [
        (
            "Deutsch B2",
            "Deutsch B2 und Erfahrung mit SAP ABAP",
            _profile(languages=[CandidateLanguage(id=1, language="Deutsch", level="C1")]),
        ),
        (
            "Abgeschlossenes Studium der Informatik",
            "Abgeschlossenes Studium der Informatik und Kenntnisse in SAP ABAP",
            _profile(
                education=[
                    CandidateEducation(
                        id=1, institution="TU", field_of_study="Informatik", completed=True
                    )
                ]
            ),
        ),
        (
            # One unknown of three requirements (2/3 met, full support)
            # legitimately scores exactly 80; two unknown clauses are used.
            "Mindestens 3 Jahre Berufserfahrung mit Python",
            "Mindestens 3 Jahre Berufserfahrung mit Python und Erfahrung mit SAP ABAP "
            "sowie SAP HANA",
            _profile(["Python"], [PYTHON_WORK]),
        ),
        (
            "Erfahrung mit Python",
            "Erfahrung mit Python in der Softwareentwicklung",
            _profile(["Python"], [PYTHON_WORK]),
        ),
    ],
)
def test_vanished_secondary_requirement_cannot_cross_the_threshold(complete, mixed, profile):
    assert _match(_profil(complete), profile).overall_score >= _threshold()
    assert _match(_profil(mixed), profile).overall_score < _threshold()


# --- v5 cache identity ---------------------------------------------------------------


def test_v4_row_is_kept_and_cache_lookup_uses_v5():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v5",
            source="bundesagentur",
            title="Developer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("Deutsch B2 und Erfahrung mit SAP ABAP"),
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
        v4_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v4",
            overall_score=90,
            coverage_score=90,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=50,
        )
        create_match(db, v4_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert ALGORITHM_VERSION == "v5"
        assert result.algorithm_version == "v5"
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v4", "v5"]
        assert db.scalar(select(func.count()).select_from(CandidateJobMatchRecord)) == 2
