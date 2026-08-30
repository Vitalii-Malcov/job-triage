from datetime import UTC, datetime

import pytest

from app.agents.candidate_job_matcher import ALGORITHM_VERSION, JobMatchInput, compute_match
from app.models.candidate_profile import (
    CandidateEducation,
    CandidateExperience,
    CandidateJobPreferences,
    CandidateLanguage,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
)


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
    )
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _job(**overrides) -> JobMatchInput:
    defaults = dict(job_id=1, title="Python Developer", description="")
    defaults.update(overrides)
    return JobMatchInput(**defaults)


# --- skill matching -------------------------------------------------------


def test_exact_confirmed_skill_matches():
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.matched_requirements) == 1
    match = result.matched_requirements[0]
    assert match.requirement_type == "SKILL"
    assert match.importance == "REQUIRED"
    assert match.candidate_evidence[0].entity_id == 1


def test_skill_matching_is_case_and_whitespace_normalized():
    profile = _profile(skills=[CandidateSkill(id=1, name="  PostgreSQL  ")])
    job = _job(must_have_skills=["postgres"])
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.matched_requirements) == 1
    assert result.matched_requirements[0].requirement == "postgres"


def test_untrusted_skill_is_not_matched():
    profile = _profile(
        skills=[CandidateSkill(id=1, name="Docker", source="INFERRED", confidence="CONFIRMED")]
    )
    job = _job(must_have_skills=["docker"])
    result = compute_match(job, profile, company_research_id=None)

    assert result.matched_requirements == []
    assert len(result.missing_requirements) == 1
    assert result.missing_requirements[0].candidate_evidence == []


def test_required_skill_missing():
    profile = _profile(skills=[])
    job = _job(must_have_skills=["docker"])
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.missing_requirements) == 1
    assert result.missing_requirements[0].importance == "REQUIRED"


def test_preferred_skill_missing():
    profile = _profile(skills=[])
    job = _job(nice_to_have_skills=["docker"])
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.missing_requirements) == 1
    assert result.missing_requirements[0].importance == "PREFERRED"


def test_no_job_skill_requirements_produces_no_skill_rows():
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    job = _job()
    result = compute_match(job, profile, company_research_id=None)

    skill_rows = [
        r
        for bucket in (
            result.matched_requirements,
            result.partial_requirements,
            result.missing_requirements,
            result.unknown_requirements,
        )
        for r in bucket
        if r.requirement_type == "SKILL"
    ]
    assert skill_rows == []


def test_alias_postgres_postgresql_matches():
    profile = _profile(skills=[CandidateSkill(id=1, name="Postgres")])
    job = _job(must_have_skills=["postgresql"])
    result = compute_match(job, profile, company_research_id=None)
    assert len(result.matched_requirements) == 1


def test_unrelated_skill_does_not_fuzzy_match():
    """Spec section 6/7: Flask requirement vs. a candidate who only has
    Python must be MISSING, never PARTIAL/MATCH — no fuzzy relatedness.
    """
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    job = _job(must_have_skills=["flask"])
    result = compute_match(job, profile, company_research_id=None)

    assert result.matched_requirements == []
    assert result.partial_requirements == []
    assert len(result.missing_requirements) == 1


# --- trust matrix ----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "confidence"),
    [
        ("INFERRED", "CONFIRMED"),
        ("IMPORTED", "CONFIRMED"),
        ("UNKNOWN", "CONFIRMED"),
        ("MANUAL_ENTRY", "UNCONFIRMED"),
    ],
)
def test_untrusted_skill_combinations_are_ignored(source, confidence):
    profile = _profile(
        skills=[CandidateSkill(id=1, name="Python", source=source, confidence=confidence)]
    )
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)
    assert result.matched_requirements == []


@pytest.mark.parametrize("source", ["USER_CONFIRMED", "USER_PROVIDED_DOCUMENT", "MANUAL_ENTRY"])
def test_trusted_confirmed_skill_is_accepted(source):
    profile = _profile(
        skills=[CandidateSkill(id=1, name="Python", source=source, confidence="CONFIRMED")]
    )
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)
    assert len(result.matched_requirements) == 1


# --- language matching ------------------------------------------------------


def test_language_higher_level_matches_lower_requirement():
    profile = _profile(languages=[CandidateLanguage(id=1, language="German", level="B2")])
    job = _job(description="Deutsch B1 ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.matched_requirements) == 1
    assert result.matched_requirements[0].requirement_type == "LANGUAGE"


def test_language_lower_level_does_not_match_higher_requirement():
    profile = _profile(languages=[CandidateLanguage(id=1, language="German", level="B1")])
    job = _job(description="Deutsch B2 ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert result.matched_requirements == []
    assert len(result.partial_requirements) == 1
    assert result.partial_requirements[0].match_status == "PARTIAL"


def test_language_c1_matches_b2_requirement():
    profile = _profile(languages=[CandidateLanguage(id=1, language="German", level="C1")])
    job = _job(description="Deutsch B2 ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.matched_requirements) == 1


def test_language_unknown_level_does_not_match_explicit_requirement():
    profile = _profile(languages=[CandidateLanguage(id=1, language="German", level="UNKNOWN")])
    job = _job(description="Deutsch B2 ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert result.matched_requirements == []
    assert len(result.unknown_requirements) == 1


def test_language_no_candidate_record_is_missing():
    profile = _profile(languages=[])
    job = _job(description="Deutsch B2 ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.missing_requirements) == 1
    assert result.missing_requirements[0].requirement_type == "LANGUAGE"


def test_untrusted_language_is_treated_as_absent():
    profile = _profile(
        languages=[
            CandidateLanguage(
                id=1, language="German", level="C2", source="INFERRED", confidence="CONFIRMED"
            )
        ]
    )
    job = _job(description="Deutsch B2 ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert result.matched_requirements == []
    assert len(result.missing_requirements) == 1


# --- education matching -----------------------------------------------------


def test_completed_education_matches_requirement():
    profile = _profile(
        education=[CandidateEducation(id=1, institution="TU Berlin", completed=True)]
    )
    job = _job(description="Ein abgeschlossenes Studium ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.matched_requirements) == 1
    assert result.matched_requirements[0].requirement_type == "EDUCATION"


def test_incomplete_education_does_not_falsely_match_completed_requirement():
    profile = _profile(
        education=[CandidateEducation(id=1, institution="TU Berlin", completed=False)]
    )
    job = _job(description="Ein abgeschlossenes Studium ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)

    assert result.matched_requirements == []
    assert len(result.missing_requirements) == 1
    assert result.missing_requirements[0].requirement_type == "EDUCATION"


def test_no_education_requirement_in_job_produces_no_education_row():
    profile = _profile(
        education=[CandidateEducation(id=1, institution="TU Berlin", completed=True)]
    )
    job = _job(description="We use Python and Docker.")
    result = compute_match(job, profile, company_research_id=None)

    education_rows = [
        r
        for bucket in (
            result.matched_requirements,
            result.partial_requirements,
            result.missing_requirements,
            result.unknown_requirements,
        )
        for r in bucket
        if r.requirement_type == "EDUCATION"
    ]
    assert education_rows == []
    assert result.relevant_education == []


# --- project/experience relevance -------------------------------------------


def test_project_technology_supports_requirement():
    profile = _profile(
        projects=[CandidateProject(id=1, name="Job Triage", technologies=["Python", "Flask"])]
    )
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.relevant_projects) == 1
    assert result.relevant_projects[0].project_id == 1
    assert "Python" in result.relevant_projects[0].matched_skills


def test_unrelated_project_excluded():
    profile = _profile(
        projects=[CandidateProject(id=1, name="Painting App", technologies=["Java"])]
    )
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)

    assert result.relevant_projects == []


def test_experience_explicit_technology_supports_match():
    profile = _profile(
        experiences=[
            CandidateExperience(
                id=1, company="Acme", job_title="Dev", technologies=["Python", "Flask"]
            )
        ]
    )
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.relevant_experiences) == 1
    assert result.relevant_experiences[0].experience_id == 1


def test_skill_elsewhere_does_not_get_attached_to_experience():
    """A skill listed only in `skills` (not in the experience's own
    `technologies`) must never be attributed to that experience."""
    profile = _profile(
        skills=[CandidateSkill(id=1, name="Docker")],
        experiences=[
            CandidateExperience(id=1, company="Acme", job_title="Dev", technologies=["Python"])
        ],
    )
    job = _job(must_have_skills=["python", "docker"])
    result = compute_match(job, profile, company_research_id=None)

    assert result.relevant_experiences[0].matched_skills == ["Python"]


def test_untrusted_experience_is_excluded_from_relevant_experiences():
    profile = _profile(
        experiences=[
            CandidateExperience(
                id=1,
                company="Acme",
                job_title="Dev",
                technologies=["Python"],
                source="INFERRED",
                confidence="CONFIRMED",
            )
        ]
    )
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)
    assert result.relevant_experiences == []


# --- score formula -----------------------------------------------------------


def test_all_required_and_preferred_matched_yields_high_score():
    profile = _profile(
        skills=[CandidateSkill(id=1, name="Python"), CandidateSkill(id=2, name="Docker")]
    )
    job = _job(must_have_skills=["python"], nice_to_have_skills=["docker"])
    result = compute_match(job, profile, company_research_id=None)

    assert result.required_skill_score == 100
    assert result.preferred_skill_score == 100
    assert result.overall_score >= 80


def test_required_missing_causes_meaningful_penalty():
    profile = _profile(skills=[])
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)
    assert result.required_skill_score == 0


def test_only_preferred_missing_causes_smaller_penalty():
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    job = _job(must_have_skills=["python"], nice_to_have_skills=["docker"])
    result_full = compute_match(
        _job(must_have_skills=["python"]), profile, company_research_id=None
    )
    result_missing_preferred = compute_match(job, profile, company_research_id=None)

    assert result_missing_preferred.overall_score <= result_full.overall_score


def test_no_requirements_at_all_is_a_controlled_neutral_result():
    profile = _profile(skills=[])
    job = _job()
    result = compute_match(job, profile, company_research_id=None)

    assert 0 <= result.overall_score <= 100
    assert 0 <= result.coverage_score <= 100
    assert 0 <= result.required_skill_score <= 100
    assert 0 <= result.preferred_skill_score <= 100


def test_score_always_bounded_and_no_divide_by_zero_with_full_profile():
    profile = _profile(
        skills=[CandidateSkill(id=i, name=f"skill-{i}") for i in range(5)],
        languages=[CandidateLanguage(id=1, language="German", level="C2")],
        education=[CandidateEducation(id=1, institution="TU Berlin", completed=True)],
    )
    job = _job(
        must_have_skills=["skill-0", "skill-1", "docker"],
        nice_to_have_skills=["skill-2"],
        description="Deutsch B1 ist erforderlich. Ein abgeschlossenes Studium ist erforderlich.",
    )
    result = compute_match(job, profile, company_research_id=None)

    for score in (
        result.overall_score,
        result.coverage_score,
        result.required_skill_score,
        result.preferred_skill_score,
        result.experience_support_score,
    ):
        assert 0 <= score <= 100


# --- warnings / empty profile -------------------------------------------


def test_empty_profile_produces_warning_not_failure():
    profile = _profile()
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)

    assert any("no confirmed facts" in w for w in result.warnings)
    assert any("Missing required skill" in w for w in result.warnings)


def test_missing_required_language_produces_warning():
    profile = _profile()
    job = _job(description="Deutsch B2 ist erforderlich.")
    result = compute_match(job, profile, company_research_id=None)
    assert any("Missing required language" in w for w in result.warnings)


def test_negated_language_requirement_does_not_affect_scores_or_warnings():
    """M-01 matcher-level regression: a negated language statement in the
    job text must produce no LANGUAGE requirement row at all, so it can't
    contribute a false "missing" penalty to any score or warning — the
    result must be identical to a job with no language mention at all.
    """
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    job_with_negated_language = _job(
        must_have_skills=["python"],
        description="No German B2 required.",
    )
    job_without_language_mention = _job(must_have_skills=["python"], description="")

    result_negated = compute_match(job_with_negated_language, profile, company_research_id=None)
    result_plain = compute_match(job_without_language_mention, profile, company_research_id=None)

    language_rows = [
        r
        for bucket in (
            result_negated.matched_requirements,
            result_negated.partial_requirements,
            result_negated.missing_requirements,
            result_negated.unknown_requirements,
        )
        for r in bucket
        if r.requirement_type == "LANGUAGE"
    ]
    assert language_rows == []
    assert not any("German" in w for w in result_negated.warnings)
    assert not any("language" in w.lower() for w in result_negated.warnings)

    assert result_negated.required_skill_score == result_plain.required_skill_score
    assert result_negated.overall_score == result_plain.overall_score
    assert result_negated.missing_requirements == result_plain.missing_requirements
    assert result_negated.warnings == result_plain.warnings


# --- provenance / claims / metadata ----------------------------------------


def test_safe_candidate_claims_trace_back_to_matched_evidence():
    profile = _profile(skills=[CandidateSkill(id=7, name="Python")])
    job = _job(must_have_skills=["python"])
    result = compute_match(job, profile, company_research_id=None)

    assert len(result.safe_candidate_claims) == 1
    claim = result.safe_candidate_claims[0]
    assert claim.claim_type == "SKILL"
    assert claim.source_id == 7
    assert claim.profile_version == profile.profile_version


def test_candidate_profile_version_is_pinned():
    profile = _profile(profile_version=42)
    job = _job()
    result = compute_match(job, profile, company_research_id=None)
    assert result.candidate_profile_version == 42


def test_company_research_id_is_passed_through_but_never_alters_matching():
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    job = _job(must_have_skills=["python"])

    result_none = compute_match(job, profile, company_research_id=None)
    result_with_id = compute_match(job, profile, company_research_id=99)

    assert result_none.company_research_id is None
    assert result_with_id.company_research_id == 99
    assert result_none.overall_score == result_with_id.overall_score
    assert result_none.matched_requirements == result_with_id.matched_requirements


def test_algorithm_version_is_stamped():
    profile = _profile()
    job = _job()
    result = compute_match(job, profile, company_research_id=None)
    assert result.algorithm_version == ALGORITHM_VERSION == "v1"
