"""Production V1 quality patch: vacancy ranking must be dominated by role
relevance, not by keyword overlap.

Reproduces the first real Bundesagentur run, where several clearly
non-target roles reached APPLY 81-84 purely because the posting mentioned
Python and SQL (Aktuar, Director, Konstrukteur, Business Consultant,
Berater, Expert ...). Generic semantic fixtures, no company names.
"""

import pytest

from app.agents.job_score_evaluator import EvaluationTrace, evaluate_job_score
from app.agents.role_relevance_classifier import classify_title_relevance
from app.agents.seniority_classifier import classify_title_seniority
from app.models.job import Job

# The real candidate: a junior, software-development-targeting profile.
CANDIDATE_SKILLS = frozenset(
    {"python", "sql", "docker", "git", "postgresql", "flask", "fastapi", "linux", "rest api"}
)

DESCRIPTION = (
    "Sie arbeiten mit Python und SQL, werten Daten aus und erstellen Reports, "
    "Dashboards und KPIs. Sie analysieren und visualisieren Daten, betreuen das "
    "Data-Warehouse und sichern die Datenqualität im Reporting. "
) * 4


def _job(title: str, must: list[str] | None = None, nice: list[str] | None = None) -> Job:
    return Job(
        source="bundesagentur",
        title=title,
        company="Beispiel GmbH",
        url="https://example.com/job",
        description=DESCRIPTION,
        posting_type="ARBEIT",
        must_have_skills=must if must is not None else ["Python", "SQL"],
        nice_to_have_skills=nice or [],
    )


def _evaluate(job: Job, *, seniority: str = "JUNIOR", domain: str = "SOFTWARE_DEVELOPMENT"):
    trace = EvaluationTrace()
    result = evaluate_job_score(
        job,
        "ARBEIT",
        candidate_skills=CANDIDATE_SKILLS,
        allowed_employment_types=("ARBEIT",),
        get_candidate_target_seniority=lambda: seniority,
        get_candidate_target_domain=lambda: domain,
        trace=trace,
    )
    return result, trace


def test_baseline_python_sql_overlap_alone_scores_apply_range():
    # Precondition for every NOT-APPLY case below: the raw keyword score
    # really is in the APPLY range, so the gate -- not the math -- decides.
    result, trace = _evaluate(_job("Junior Python Developer (m/w/d)"))
    assert trace.base_score is not None and trace.base_score >= 80
    assert trace.base_recommendation == "APPLY"
    assert result.recommendation == "APPLY"


@pytest.mark.parametrize(
    "title",
    [
        "Aktuar (m/w/d)",
        "Director Market Intelligence & Data Analytics (m/f/d)",
        "Entwicklungsingenieur / Konstrukteur Mechanik (m/w/d)",
        "Business Consultant (m/w/d) Datenmanagement Facharchitektur Versicherung",
        "Berater QPPM Ressort (m/w/d)",
        "Expert DevSecOps Process Automation (m/w/d)",
        "IT Application Support Engineer (Linux) (m/w/d)",
        "Mitarbeit im Forschungsbereich Cybercrime (w/m/d)",
    ],
)
def test_python_sql_overlap_cannot_make_non_target_role_apply(title):
    result, _ = _evaluate(_job(title))
    assert result.recommendation != "APPLY", title


def test_aktuar_is_excluded_as_irrelevant_role():
    assert classify_title_relevance("Aktuar (m/w/d)").level == "IRRELEVANT"
    result, _ = _evaluate(_job("Aktuar (m/w/d)"))
    assert result.recommendation == "SKIP"


def test_konstrukteur_mechanik_is_excluded_as_irrelevant_role():
    title = "Entwicklungsingenieur / Konstrukteur Mechanik (m/w/d)"
    assert classify_title_relevance(title).level == "IRRELEVANT"
    result, _ = _evaluate(_job(title))
    assert result.recommendation == "SKIP"


@pytest.mark.parametrize(
    "title",
    [
        "Director Market Intelligence & Data Analytics (m/f/d)",
        "Direktor Datenmanagement (m/w/d)",
        "Expert DevSecOps Process Automation (m/w/d)",
        "Experte Datenbanken (m/w/d)",
        "Expertin Datenanalyse (m/w/d)",
    ],
)
def test_director_and_expert_titles_are_senior_and_excluded_for_junior(title):
    assert classify_title_seniority(title).level == "SENIOR"
    result, trace = _evaluate(_job(title))
    assert result.recommendation == "SKIP"
    assert any(event.startswith("seniority_excluded:") for event in trace.events)


def test_junior_marker_still_neutralizes_expert_title():
    # Same M3 rule as for "senior": an explicit junior track is not excluded.
    assert classify_title_seniority("Junior Expert Data (m/w/d)").level == "UNKNOWN"


def test_expert_title_not_excluded_when_candidate_target_seniority_unknown():
    result, _ = _evaluate(_job("Expert DevSecOps Process Automation (m/w/d)"), seniority="UNKNOWN")
    assert result.recommendation != "SKIP"


def test_business_consultant_without_dev_core_is_not_apply():
    result, trace = _evaluate(
        _job("Business Consultant (m/w/d) Datenmanagement Facharchitektur Versicherung")
    )
    assert result.recommendation == "MAYBE"
    assert "role_apply_cap:UNKNOWN" in trace.events


@pytest.mark.parametrize(
    "title",
    [
        "Junior Python Backend Developer (m/w/d)",
        "Python Backend Developer (m/w/d)",
        "Softwareentwickler Python (m/w/d)",
        "Junior Software Engineer (m/w/d)",
    ],
)
def test_valid_junior_python_backend_roles_remain_apply(title):
    result, _ = _evaluate(_job(title))
    assert result.recommendation == "APPLY", title


@pytest.mark.parametrize(
    "title",
    [
        "Datenanalyst (m/w/d) in Wiesbaden",
        "Datenanalystin (m/w/d)",
        "Data Analyst (m/w/d)",
        "Junior Data Analyst (m/w/d)",
    ],
)
def test_data_analyst_roles_with_python_sql_remain_apply(title):
    result, _ = _evaluate(_job(title))
    assert result.recommendation == "APPLY", title


def test_data_engineer_role_is_not_rejected_for_not_being_backend():
    result, _ = _evaluate(_job("Data Engineer (m/w/d)", must=["Python", "SQL", "Docker"]))
    assert result.recommendation == "APPLY"


def test_specialised_cloud_data_infrastructure_role_is_review_not_apply_and_not_rejected():
    result, _ = _evaluate(
        _job(
            "Cloud Data Infrastructure Engineer (m/f/d)",
            must=["Docker", "Git", "Python"],
            nice=["AWS", "PostgreSQL"],
        )
    )
    assert result.recommendation == "MAYBE"


def test_role_apply_cap_does_not_apply_without_software_target_domain():
    # Fail-open stays fail-open: no confident candidate domain, no new gate.
    result, _ = _evaluate(_job("Berater QPPM Ressort (m/w/d)"), domain="UNKNOWN")
    assert result.recommendation == "APPLY"


def test_role_apply_cap_never_raises_and_never_touches_skip_or_maybe():
    result, _ = _evaluate(_job("Berater QPPM Ressort (m/w/d)", must=["Kubernetes", "Java"]))
    assert result.recommendation == "SKIP"


def test_role_apply_cap_preserves_numeric_score():
    result, trace = _evaluate(_job("Berater QPPM Ressort (m/w/d)"))
    assert result.score == trace.base_score
