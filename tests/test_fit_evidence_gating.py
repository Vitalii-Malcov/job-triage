"""Fit-evidence gating (Stage 22 follow-up).

Confirmed on the 2026-11-01 TOP-30 review of production data:
- a matcher analysis with NO extracted required skill carries a neutral
  score of 50 and was shown as PARTIAL_MATCH (10 of 31 GOOD/PARTIAL rows);
- one extracted skill (e.g. "Linux" from the title "Embedded Linux ...") gave
  a STRONG / GOOD_MATCH although the posting text had no technical content;
- "Mindestens zwei bis drei Jahre Berufserfahrung" (number words) left the
  experience criterion UNKNOWN.
Nothing here excludes a vacancy: NOT_EVALUATED is a NEEDS_OPERATOR check.
"""

from datetime import UTC, datetime

import pytest

from app.agents.job_fit_signals import entry_level_signals
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.services import job_fit_assessment as fit


def _req(name: str, status: str = "MATCH", importance: str = "REQUIRED", kind: str = "SKILL"):
    return RequirementMatch(
        requirement=name,
        normalized_requirement=name.lower(),
        requirement_type=kind,
        importance=importance,
        match_status=status,
        candidate_evidence=[],
        job_evidence=[],
        reason="test fixture",
    )


def _match(
    *, score: int = 100, matched=(), partial=(), missing=(), unknown=()
) -> CandidateJobMatch:
    return CandidateJobMatch(
        id=1,
        created_at=datetime.now(UTC),
        job_id=1,
        candidate_profile_version=1,
        company_research_id=None,
        algorithm_version="v12",
        overall_score=score,
        coverage_score=score,
        required_skill_score=score,
        preferred_skill_score=100,
        experience_support_score=0,
        matched_requirements=list(matched),
        partial_requirements=list(partial),
        missing_requirements=list(missing),
        unknown_requirements=list(unknown),
        relevant_experiences=[],
        relevant_projects=[],
        relevant_education=[],
        relevant_languages=[],
        safe_candidate_claims=[],
        warnings=[],
    )


def test_no_extracted_skill_is_not_evaluated_even_with_neutral_score_50():
    criterion = fit.skills_criterion(_match(score=50))
    assert criterion.status == "NOT_EVALUATED"
    assert ("skills", "NOT_EVALUATED") in fit._VERIFY  # a check for the operator, not an exclusion


def test_non_skill_requirements_do_not_count_as_extracted_skills():
    match = _match(score=100, matched=[_req("5 years", kind="EXPERIENCE")])
    assert fit.skills_criterion(match).status == "NOT_EVALUATED"


def test_single_extracted_skill_is_not_demoted():
    """A junior posting naming only Python must stay a candidate."""
    assert fit.skills_criterion(_match(score=100, matched=[_req("python")])).status == "STRONG"


def test_two_matched_skills_are_still_strong():
    criterion = fit.skills_criterion(_match(score=100, matched=[_req("python"), _req("sql")]))
    assert criterion.status == "STRONG"


def test_missing_required_skill_keeps_existing_behaviour():
    criterion = fit.skills_criterion(
        _match(score=50, matched=[_req("python")], missing=[_req("php", "MISSING")])
    )
    assert criterion.status == "PARTIAL"
    assert "missing required: php" in criterion.reason


def test_no_analysis_is_still_not_evaluated():
    assert fit.skills_criterion(None).status == "NOT_EVALUATED"


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("* Mindestens zwei bis drei Jahre fachbezogene Berufserfahrung", 2),
        ("Mindestens drei Jahre Berufserfahrung", 3),
        ("Berufserfahrung von mindestens vier Jahren", 4),
        ("fünf Jahre Erfahrung im Betrieb", 5),
        ("Mindestens 2-3 Jahre fachbezogene Berufserfahrung", 2),
        ("mit 2 - 3 Jahren beruflicher Erfahrung", 2),
        ("5+ Jahre Erfahrung", 5),
        ("3 years of experience", 3),
        ("Erfahrung (3 Jahre)", 3),
    ],
)
def test_required_years_digits_and_number_words(text, years):
    assert entry_level_signals("Software Entwickler", text).required_years == years


@pytest.mark.parametrize(
    "text",
    [
        "Wir bieten keine Jahre Erfahrung an Hürden",
        "Einsteiger ohne Berufserfahrung willkommen",
        "Teamgeist und Erfahrung mit Python",
    ],
)
def test_no_false_years_from_unrelated_words(text):
    assert entry_level_signals("Software Entwickler", text).required_years is None


def test_two_to_three_years_written_out_is_partial_not_unknown():
    text = "Mindestens zwei bis drei Jahre fachbezogene Berufserfahrung"
    assert fit.experience_criterion(None, "Softwareentwickler", text).status == "PARTIAL"
    gap = fit.experience_criterion(
        None, "Softwareentwickler", "Mindestens fünf Jahre Berufserfahrung"
    )
    assert gap.status == "GAP"


@pytest.mark.parametrize("importance", ["REQUIRED", "PREFERRED", "UNKNOWN"])
def test_single_skill_of_any_importance_counts_as_extracted(importance):
    match = _match(score=100, matched=[_req("python", importance=importance)])
    assert fit.skills_criterion(match).status != "NOT_EVALUATED"


@pytest.mark.parametrize(
    "text",
    [
        "Wir sind ein seit vier Jahren bestehendes Unternehmen mit Erfahrung in IT",
        "Wir sind ein seit 4 Jahren bestehendes Unternehmen mit Erfahrung in IT",
        "Erfahrung: vor drei Jahren gegründet",
        "Wir sind ein seit 15 Jahren bestehendes Unternehmen mit Erfahrung",
        "Erfahrung: vor 15 Jahren gegründet",
    ],
)
def test_company_age_is_not_a_required_experience(text):
    assert entry_level_signals("Software Entwickler", text).required_years is None


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("Berufserfahrung von mindestens zwei bis drei Jahren", 2),
        ("Berufserfahrung von mindestens 2-3 Jahren", 2),
        ("Erfahrung von mindestens vier Jahren", 4),
    ],
)
def test_experience_first_wording_and_ranges(text, years):
    assert entry_level_signals("Software Entwickler", text).required_years == years
