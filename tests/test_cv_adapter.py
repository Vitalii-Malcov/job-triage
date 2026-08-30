from datetime import UTC, datetime

import pytest

from app.agents.cv_adapter import CV_ADAPTER_VERSION, compute_cv_draft
from app.models.candidate_job_match import (
    CandidateEvidenceRef,
    CandidateJobMatch,
    RelevantExperience,
    RelevantProject,
    RequirementMatch,
)
from app.models.candidate_profile import (
    CandidateCertification,
    CandidateEducation,
    CandidateExperience,
    CandidateJobPreferences,
    CandidateLanguage,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
    FieldTrust,
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


def _match(**overrides) -> CandidateJobMatch:
    defaults = dict(
        id=1,
        job_id=1,
        candidate_profile_version=1,
        company_research_id=None,
        algorithm_version="v1",
        overall_score=50,
        coverage_score=50,
        required_skill_score=50,
        preferred_skill_score=100,
        experience_support_score=50,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return CandidateJobMatch(**defaults)


def _skill_match(
    skill_id: int, name: str, requirement: str = None, importance: str = "REQUIRED"
) -> RequirementMatch:
    requirement = requirement or name.casefold()
    return RequirementMatch(
        requirement=requirement,
        normalized_requirement=requirement,
        requirement_type="SKILL",
        importance=importance,
        match_status="MATCH",
        candidate_evidence=[
            CandidateEvidenceRef(entity_type="SKILL", entity_id=skill_id, value=name)
        ],
        job_evidence=[requirement],
        reason="matched",
    )


# --- header / top-level trust -----------------------------------------


def test_header_includes_only_top_level_trusted_fields():
    profile = _profile(
        first_name="Anna",
        last_name="Example",
        professional_title="Junior Python Developer",
        field_trust={
            "first_name": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED"),
            "last_name": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED"),
            "professional_title": FieldTrust(source="INFERRED", confidence="CONFIRMED"),
        },
    )
    match = _match()
    draft = compute_cv_draft(profile, match)

    assert draft.header.first_name.value == "Anna"
    assert draft.header.last_name.value == "Example"
    # professional_title's trust is INFERRED+CONFIRMED -> not generation-usable.
    assert draft.header.professional_title is None
    assert "NO_TRUSTED_TITLE" in draft.warnings


def test_header_field_with_no_trust_entry_is_omitted():
    profile = _profile(first_name="Anna")  # no field_trust entry at all
    match = _match()
    draft = compute_cv_draft(profile, match)
    assert draft.header.first_name is None
    assert "NO_TRUSTED_NAME" in draft.warnings


def test_never_replaces_trusted_title_with_job_title():
    """Section 14: candidate's own trusted title must never be rewritten
    to the vacancy's title."""
    profile = _profile(
        professional_title="Junior Python Backend Developer",
        field_trust={
            "professional_title": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED")
        },
    )
    match = _match()
    draft = compute_cv_draft(profile, match)
    assert draft.header.professional_title.value == "Junior Python Backend Developer"


def test_professional_summary_trusted_included_verbatim():
    profile = _profile(
        professional_summary="Backend developer focused on Python APIs.",
        field_trust={
            "professional_summary": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED")
        },
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.professional_summary.value == "Backend developer focused on Python APIs."


def test_professional_summary_untrusted_is_omitted():
    profile = _profile(
        professional_summary="Presumably a backend developer.",
        field_trust={"professional_summary": FieldTrust(source="INFERRED", confidence="CONFIRMED")},
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.professional_summary is None


# --- M-01: top-level fact provenance ------------------------------------


def test_top_level_facts_carry_exact_provenance():
    profile = _profile(
        id=1,
        profile_version=4,
        first_name="Anna",
        professional_title="Junior Python Developer",
        professional_summary="Backend-focused developer.",
        field_trust={
            "first_name": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED"),
            "professional_title": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED"),
            "professional_summary": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED"),
        },
    )
    draft = compute_cv_draft(profile, _match())

    first_name = draft.header.first_name
    assert first_name.value == "Anna"
    assert first_name.source_entity == "candidate_profile"
    assert first_name.source_id == profile.id
    assert first_name.source_field == "first_name"
    assert first_name.profile_version == profile.profile_version

    title = draft.header.professional_title
    assert title.value == "Junior Python Developer"
    assert title.source_entity == "candidate_profile"
    assert title.source_id == profile.id
    assert title.source_field == "professional_title"
    assert title.profile_version == profile.profile_version

    summary = draft.professional_summary
    assert summary.value == "Backend-focused developer."
    assert summary.source_entity == "candidate_profile"
    assert summary.source_id == profile.id
    assert summary.source_field == "professional_summary"
    assert summary.profile_version == profile.profile_version


def test_top_level_fact_uses_real_profile_id_not_hardcoded():
    profile = _profile(
        id=1,
        first_name="Anna",
        field_trust={"first_name": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED")},
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.header.first_name.source_id == profile.id


def test_top_level_facts_independent_per_field_trust():
    """Section 13: mixed trust across fields — no shared/global trust
    assumption. first_name trusted+included, professional_title
    untrusted+omitted, professional_summary trusted+included."""
    profile = _profile(
        first_name="Anna",
        professional_title="Senior Architect",
        professional_summary="Backend engineer.",
        field_trust={
            "first_name": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED"),
            "professional_title": FieldTrust(source="INFERRED", confidence="CONFIRMED"),
            "professional_summary": FieldTrust(source="USER_CONFIRMED", confidence="CONFIRMED"),
        },
    )
    draft = compute_cv_draft(profile, _match())

    assert draft.header.first_name is not None
    assert draft.header.first_name.value == "Anna"

    assert draft.header.professional_title is None

    assert draft.professional_summary is not None
    assert draft.professional_summary.value == "Backend engineer."


def test_untrusted_top_level_fact_has_no_value_and_no_provenance_object():
    """Section 15: no value, no provenance object, no accidental
    fallback — the field is exactly None, not a CVTopLevelFact with an
    empty/placeholder value."""
    profile = _profile(
        professional_title="Senior Architect",
        field_trust={"professional_title": FieldTrust(source="INFERRED", confidence="CONFIRMED")},
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.header.professional_title is None


def test_contact_data_not_modeled_warning_always_present():
    draft = compute_cv_draft(_profile(), _match())
    assert "CONTACT_DATA_NOT_MODELED" in draft.warnings


# --- skills: match gate + trust matrix (section 44/45) ---------------


def test_matched_skill_included_missing_skill_excluded():
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    match = _match(
        matched_requirements=[_skill_match(1, "Python")],
        missing_requirements=[
            RequirementMatch(
                requirement="docker",
                normalized_requirement="docker",
                requirement_type="SKILL",
                importance="REQUIRED",
                match_status="MISSING",
                candidate_evidence=[],
                job_evidence=["docker"],
                reason="not found",
            )
        ],
    )
    draft = compute_cv_draft(profile, match)
    skill_names = [s.text for s in draft.skills]
    assert skill_names == ["Python"]
    assert "Docker" not in skill_names


def test_required_skills_ordered_before_preferred():
    profile = _profile(
        skills=[CandidateSkill(id=1, name="MySQL"), CandidateSkill(id=2, name="Python")]
    )
    match = _match(
        matched_requirements=[
            _skill_match(1, "MySQL", importance="PREFERRED"),
            _skill_match(2, "Python", importance="REQUIRED"),
        ]
    )
    draft = compute_cv_draft(profile, match)
    assert [s.text for s in draft.skills] == ["Python", "MySQL"]


def test_skill_proficiency_and_category_preserved_verbatim():
    profile = _profile(
        skills=[
            CandidateSkill(
                id=1, name="Python", category="OTHER", proficiency="ADVANCED", years_experience=3.5
            )
        ]
    )
    match = _match(matched_requirements=[_skill_match(1, "Python")])
    draft = compute_cv_draft(profile, match)
    assert draft.skills[0].proficiency == "ADVANCED"
    assert draft.skills[0].category == "OTHER"
    assert draft.skills[0].years_experience == 3.5


def test_skill_proficiency_unknown_not_upgraded():
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])  # default proficiency UNKNOWN
    match = _match(matched_requirements=[_skill_match(1, "Python")])
    draft = compute_cv_draft(profile, match)
    assert draft.skills[0].proficiency == "UNKNOWN"


def test_no_relevant_skills_warning():
    draft = compute_cv_draft(_profile(), _match())
    assert "NO_RELEVANT_SKILLS" in draft.warnings


# --- experience (section 18/19/20/49) ----------------------------------


@pytest.mark.parametrize(
    ("source", "confidence"),
    [
        ("INFERRED", "CONFIRMED"),
        ("IMPORTED", "CONFIRMED"),
        ("UNKNOWN", "CONFIRMED"),
        ("MANUAL_ENTRY", "UNCONFIRMED"),
    ],
)
def test_untrusted_experience_excluded(source, confidence):
    profile = _profile(
        experiences=[
            CandidateExperience(
                id=1,
                company="Acme",
                job_title="Dev",
                technologies=["Python"],
                source=source,
                confidence=confidence,
            )
        ]
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.experience == []
    assert "NO_RELEVANT_EXPERIENCE" in draft.warnings


def test_trusted_experience_included_with_verbatim_technologies():
    profile = _profile(
        experiences=[
            CandidateExperience(id=1, company="Acme", job_title="Dev", technologies=["Python"])
        ]
    )
    draft = compute_cv_draft(profile, _match())
    assert len(draft.experience) == 1
    assert draft.experience[0].technologies == ["Python"]


def test_global_skill_not_attached_to_unrelated_experience():
    """Section 18/49: a globally trusted skill (Docker) must never be
    injected into an experience whose own technologies list omits it."""
    profile = _profile(
        skills=[CandidateSkill(id=1, name="Docker")],
        experiences=[
            CandidateExperience(id=1, company="Acme", job_title="Dev", technologies=["Python"])
        ],
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.experience[0].technologies == ["Python"]
    assert "Docker" not in draft.experience[0].technologies


def test_experience_reverse_chronological_order_preserved():
    profile = _profile(
        experiences=[
            CandidateExperience(
                id=1, company="Old", job_title="Dev", start_date="2019-01-01", end_date="2020-01-01"
            ),
            CandidateExperience(
                id=2, company="New", job_title="Dev", start_date="2022-01-01", is_current=True
            ),
        ]
    )
    draft = compute_cv_draft(profile, _match())
    assert [e.company for e in draft.experience] == ["New", "Old"]


def test_more_relevant_experience_not_reordered_ahead_of_chronology():
    """Section 20: reorder is not allowed to violate truthful chronology —
    only emphasis metadata may reflect relevance."""
    profile = _profile(
        experiences=[
            CandidateExperience(
                id=1,
                company="Old-Relevant",
                job_title="Dev",
                technologies=["Python"],
                start_date="2019-01-01",
                end_date="2020-01-01",
            ),
            CandidateExperience(
                id=2,
                company="New-Unrelated",
                job_title="Dev",
                technologies=["Java"],
                start_date="2022-01-01",
                is_current=True,
            ),
        ]
    )
    match = _match(
        relevant_experiences=[
            RelevantExperience(
                experience_id=1, company="Old-Relevant", job_title="Dev", matched_skills=["Python"]
            )
        ]
    )
    draft = compute_cv_draft(profile, match)
    assert [e.company for e in draft.experience] == ["New-Unrelated", "Old-Relevant"]
    assert draft.experience[1].emphasis == "HIGH"
    assert draft.experience[0].emphasis == "STANDARD"


# --- projects (section 21/22/50) ----------------------------------------


def test_relevant_project_included_unrelated_excluded():
    profile = _profile(
        projects=[
            CandidateProject(id=1, name="Relevant", technologies=["Python"]),
            CandidateProject(id=2, name="Unrelated", technologies=["C++"]),
        ]
    )
    match = _match(
        relevant_projects=[
            RelevantProject(project_id=1, name="Relevant", matched_skills=["Python"])
        ]
    )
    draft = compute_cv_draft(profile, match)
    assert [p.name for p in draft.projects] == ["Relevant"]


def test_projects_ranked_by_matched_skill_count():
    profile = _profile(
        projects=[
            CandidateProject(id=1, name="Small", technologies=["Python"]),
            CandidateProject(id=2, name="Big", technologies=["Python", "Flask", "MySQL"]),
        ]
    )
    match = _match(
        relevant_projects=[
            RelevantProject(project_id=1, name="Small", matched_skills=["Python"]),
            RelevantProject(project_id=2, name="Big", matched_skills=["Python", "Flask", "MySQL"]),
        ]
    )
    draft = compute_cv_draft(profile, match)
    assert [p.name for p in draft.projects] == ["Big", "Small"]


def test_project_technologies_never_invented():
    profile = _profile(projects=[CandidateProject(id=1, name="Solo", technologies=["Python"])])
    match = _match(
        relevant_projects=[RelevantProject(project_id=1, name="Solo", matched_skills=["Python"])]
    )
    draft = compute_cv_draft(profile, match)
    assert draft.projects[0].technologies == ["Python"]


# --- education (section 23/51) -------------------------------------------


def test_incomplete_education_remains_incomplete():
    profile = _profile(
        education=[CandidateEducation(id=1, institution="TU Berlin", completed=False)]
    )
    draft = compute_cv_draft(profile, _match())
    assert len(draft.education) == 1
    assert draft.education[0].completed is False


def test_untrusted_education_excluded():
    profile = _profile(
        education=[
            CandidateEducation(
                id=1,
                institution="TU Berlin",
                completed=True,
                source="INFERRED",
                confidence="CONFIRMED",
            )
        ]
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.education == []


# --- languages (section 25/27/52) -----------------------------------------


def test_language_level_never_upgraded_toward_requirement():
    profile = _profile(languages=[CandidateLanguage(id=1, language="German", level="B1")])
    match = _match(
        partial_requirements=[
            RequirementMatch(
                requirement="German B2",
                normalized_requirement="german-b2",
                requirement_type="LANGUAGE",
                importance="REQUIRED",
                match_status="PARTIAL",
                candidate_evidence=[
                    CandidateEvidenceRef(entity_type="LANGUAGE", entity_id=1, value="German")
                ],
                job_evidence=["German B2 erforderlich"],
                reason="below",
            )
        ]
    )
    draft = compute_cv_draft(profile, match)
    assert len(draft.languages) == 1
    assert draft.languages[0].level == "B1"
    assert draft.languages[0].matched_requirement == "German B2"
    assert draft.languages[0].match_status == "PARTIAL"


def test_untrusted_language_excluded():
    profile = _profile(
        languages=[
            CandidateLanguage(
                id=1, language="German", level="C2", source="UNKNOWN", confidence="CONFIRMED"
            )
        ]
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.languages == []


def test_language_with_no_job_relation_has_no_match_metadata():
    profile = _profile(languages=[CandidateLanguage(id=1, language="French", level="B2")])
    draft = compute_cv_draft(profile, _match())
    assert draft.languages[0].matched_requirement is None
    assert draft.languages[0].match_status is None


# --- certifications ------------------------------------------------------


def test_in_progress_certification_not_rendered_completed():
    profile = _profile(
        certifications=[CandidateCertification(id=1, name="AWS", status="IN_PROGRESS")]
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.certifications[0].status == "IN_PROGRESS"


def test_untrusted_certification_excluded():
    profile = _profile(
        certifications=[
            CandidateCertification(
                id=1, name="AWS", status="COMPLETED", source="IMPORTED", confidence="CONFIRMED"
            )
        ]
    )
    draft = compute_cv_draft(profile, _match())
    assert draft.certifications == []


# --- safe claim / provenance traceability (section 53) ------------------


def test_every_skill_item_traces_back_to_a_source_id():
    profile = _profile(skills=[CandidateSkill(id=7, name="Python")])
    match = _match(matched_requirements=[_skill_match(7, "Python")])
    draft = compute_cv_draft(profile, match)
    assert draft.skills[0].source_entity == "candidate_skill"
    assert draft.skills[0].source_id == 7


def test_every_experience_and_project_item_traces_back_to_a_source_id():
    profile = _profile(
        experiences=[CandidateExperience(id=3, company="Acme", job_title="Dev")],
        projects=[CandidateProject(id=4, name="Proj", technologies=["Python"])],
    )
    match = _match(
        relevant_projects=[RelevantProject(project_id=4, name="Proj", matched_skills=["Python"])]
    )
    draft = compute_cv_draft(profile, match)
    assert draft.experience[0].source_entity == "candidate_experience"
    assert draft.experience[0].source_id == 3
    assert draft.projects[0].source_entity == "candidate_project"
    assert draft.projects[0].source_id == 4


# --- snapshot pinning / metadata -----------------------------------------


def test_draft_pins_match_and_profile_version_and_algorithm_version():
    profile = _profile(profile_version=9)
    match = _match(id=42, job_id=17, candidate_profile_version=9, algorithm_version="v1")
    draft = compute_cv_draft(profile, match)
    assert draft.job_id == 17
    assert draft.match_id == 42
    assert draft.candidate_profile_version == 9
    assert draft.match_algorithm_version == "v1"
    assert draft.cv_adapter_version == CV_ADAPTER_VERSION == "v1"
    assert draft.status == "DRAFT"


# --- section ordering / emphasis (section 38) -----------------------------


def test_default_section_order_when_experience_exists():
    profile = _profile(experiences=[CandidateExperience(id=1, company="Acme", job_title="Dev")])
    draft = compute_cv_draft(profile, _match())
    assert draft.section_order == [
        "HEADER",
        "SUMMARY",
        "SKILLS",
        "EXPERIENCE",
        "PROJECTS",
        "EDUCATION",
        "CERTIFICATIONS",
        "LANGUAGES",
    ]
    assert draft.projects_emphasis == "STANDARD"


def test_projects_emphasized_when_no_experience_but_relevant_projects_exist():
    profile = _profile(projects=[CandidateProject(id=1, name="Proj", technologies=["Python"])])
    match = _match(
        relevant_projects=[RelevantProject(project_id=1, name="Proj", matched_skills=["Python"])]
    )
    draft = compute_cv_draft(profile, match)
    assert draft.projects_emphasis == "HIGH"
    assert draft.section_order.index("PROJECTS") < draft.section_order.index("EXPERIENCE")


def test_no_projects_emphasis_when_no_experience_and_no_projects():
    draft = compute_cv_draft(_profile(), _match())
    assert draft.projects_emphasis == "STANDARD"
    assert draft.section_order == list(
        [
            "HEADER",
            "SUMMARY",
            "SKILLS",
            "EXPERIENCE",
            "PROJECTS",
            "EDUCATION",
            "CERTIFICATIONS",
            "LANGUAGES",
        ]
    )


# --- company research non-influence (section 19 reused rationale) --------


def test_company_research_id_never_influences_draft_content():
    profile = _profile(skills=[CandidateSkill(id=1, name="Python")])
    match_a = _match(matched_requirements=[_skill_match(1, "Python")], company_research_id=None)
    match_b = _match(matched_requirements=[_skill_match(1, "Python")], company_research_id=77)

    draft_a = compute_cv_draft(profile, match_a)
    draft_b = compute_cv_draft(profile, match_b)
    assert draft_a.skills == draft_b.skills
    assert draft_a.experience == draft_b.experience
