"""Stage 14A: profile readiness + profile -> Bewerbung quality.

End-to-end over the pure pipeline (matcher -> CV adapter -> evidence ->
deterministic provider -> trusted renderer), with synthetic profiles only.
No DB, no network.
"""

import asyncio
from datetime import UTC, date, datetime

import pytest

from app.agents.bewerbung_renderer import build_evidence, parse_plan, render_draft, resolve_plan
from app.agents.candidate_job_matcher import JobMatchInput, compute_match
from app.agents.cv_adapter import compute_cv_draft
from app.agents.profile_readiness import (
    AREA_CERTIFICATIONS,
    AREA_EDUCATION,
    AREA_EXPERIENCE,
    AREA_LANGUAGES,
    AREA_NAME,
    AREA_PROJECTS,
    AREA_SKILLS,
    AREA_TITLE,
    NOTE_CERTIFICATION_NOT_COMPLETED,
    NOTE_EDUCATION_NOT_COMPLETED,
    NOTE_LANGUAGE_LEVEL_UNKNOWN,
    NOTE_TARGET_ROLES_NOT_A_TITLE,
    evaluate_profile_readiness,
    render_profile_readiness,
)
from app.models.candidate_job_match import CandidateJobMatch
from app.models.candidate_profile import (
    CandidateCertification,
    CandidateEducation,
    CandidateExperience,
    CandidateLanguage,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
    FieldTrust,
)
from app.models.cv_draft import TailoredCVDraft
from app.providers.bewerbung.deterministic import DeterministicBewerbungProvider

_TRUSTED = FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED")
_SUMMARY = (
    "Engineering background with earlier programming work as a freelancer. "
    "Currently in a Weiterbildung with practical project work."
)


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        id=1,
        profile_version=3,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        first_name="Anna",
        last_name="Example",
        professional_summary=_SUMMARY,
        target_roles=["Junior Python Developer"],
        field_trust={
            "first_name": _TRUSTED,
            "last_name": _TRUSTED,
            "professional_summary": _TRUSTED,
            "target_roles": _TRUSTED,
        },
        skills=[
            CandidateSkill(id=1, name="Python", category="LANGUAGE"),
            CandidateSkill(id=2, name="Git", category="TOOL"),
            CandidateSkill(id=3, name="REST API"),
        ],
    )
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _complete_profile(**overrides) -> CandidateProfile:
    trust = {
        "first_name": _TRUSTED,
        "last_name": _TRUSTED,
        "professional_title": _TRUSTED,
        "professional_summary": _TRUSTED,
    }
    defaults = dict(
        professional_title="Python Backend Developer",
        field_trust=trust,
        experiences=[
            CandidateExperience(
                id=11,
                company="Example Freelance GmbH",
                job_title="Software Developer (Teilzeit)",
                start_date=date(2019, 1, 1),
                end_date=date(2021, 12, 31),
                technologies=["Python", "Git"],
            )
        ],
        projects=[
            CandidateProject(id=21, name="Job Triage API", technologies=["Python", "FastAPI"])
        ],
        education=[
            CandidateEducation(
                id=31, institution="Example Akademie", program="Python Backend", completed=False
            )
        ],
        languages=[
            CandidateLanguage(id=41, language="Deutsch", level="B2"),
            CandidateLanguage(id=42, language="Englisch", level="UNKNOWN"),
        ],
    )
    defaults.update(overrides)
    return _profile(**defaults)


_JOB = JobMatchInput(
    job_id=7,
    title="AI Engineer (m/w/d)",
    description="Wir suchen Unterstützung mit Python, Git und REST.",
    must_have_skills=["python", "git", "rest"],
)


def _pipeline(profile: CandidateProfile, job: JobMatchInput = _JOB):
    data = compute_match(job, profile, company_research_id=None)
    match = CandidateJobMatch(id=1, created_at=datetime.now(UTC), **data.model_dump())
    cv_data = compute_cv_draft(profile, match)
    cv = TailoredCVDraft(id=1, created_at=datetime.now(UTC), **cv_data.model_dump())
    evidence, registry = build_evidence(cv, match, job.title, "Example AG", job.description)
    raw_plan = asyncio.run(DeterministicBewerbungProvider().generate_plan(evidence))
    plan = parse_plan(raw_plan)
    rendered = render_draft(plan, resolve_plan(plan, registry), job.title, "Example AG", None)
    body = " ".join(p.text for p in rendered.body_paragraphs)
    return match, cv, evidence, body


# --- profile readiness --------------------------------------------------


def test_readiness_reproduces_stage13d_gap_shape():
    """The live 13D profile shape: name/skills/summary only. Title,
    experience, projects and education appear only in free text; languages
    are absent. Nothing in the summary is promoted to a structured fact."""
    report = evaluate_profile_readiness(_profile())

    assert report.status(AREA_NAME) == "VERIFIED"
    assert report.status(AREA_SKILLS) == "VERIFIED"
    assert report.status(AREA_TITLE) == "FREE_TEXT_ONLY"  # "Engineering ..." in summary
    assert NOTE_TARGET_ROLES_NOT_A_TITLE in report.area(AREA_TITLE).notes
    assert report.status(AREA_EXPERIENCE) == "FREE_TEXT_ONLY"
    assert report.status(AREA_PROJECTS) == "FREE_TEXT_ONLY"
    assert report.status(AREA_EDUCATION) == "FREE_TEXT_ONLY"
    assert report.status(AREA_LANGUAGES) == "MISSING"
    assert report.status(AREA_CERTIFICATIONS) == "MISSING"
    assert not report.has_relevant_structured_evidence
    assert report.send_package_gaps == [
        AREA_TITLE,
        AREA_EDUCATION,
        AREA_LANGUAGES,
        "EXPERIENCE_OR_PROJECT",
    ]


def test_readiness_complete_profile_has_no_send_package_gaps():
    report = evaluate_profile_readiness(_complete_profile())
    assert report.send_package_gaps == []
    assert report.status(AREA_TITLE) == "VERIFIED"
    assert NOTE_EDUCATION_NOT_COMPLETED in report.area(AREA_EDUCATION).notes
    assert NOTE_LANGUAGE_LEVEL_UNKNOWN in report.area(AREA_LANGUAGES).notes


@pytest.mark.parametrize(
    ("source", "confidence"),
    [("INFERRED", "CONFIRMED"), ("IMPORTED", "CONFIRMED"), ("MANUAL_ENTRY", "UNCONFIRMED")],
)
def test_readiness_untrusted_rows_are_unsafe_not_verified(source, confidence):
    profile = _profile(
        experiences=[
            CandidateExperience(
                id=1, company="X", job_title="Dev", source=source, confidence=confidence
            )
        ],
        certifications=[
            CandidateCertification(id=2, name="Cert", source=source, confidence=confidence)
        ],
    )
    report = evaluate_profile_readiness(profile)
    assert report.status(AREA_EXPERIENCE) == "UNSAFE_TO_CLAIM"
    assert report.area(AREA_EXPERIENCE).untrusted_count == 1
    assert report.status(AREA_CERTIFICATIONS) == "UNSAFE_TO_CLAIM"
    assert AREA_EXPERIENCE in report.unsafe_areas


def test_readiness_untrusted_title_is_unsafe():
    profile = _profile(
        professional_title="Senior Architect",
        field_trust={**_profile().field_trust, "professional_title": FieldTrust(source="INFERRED")},
    )
    assert evaluate_profile_readiness(profile).status(AREA_TITLE) == "UNSAFE_TO_CLAIM"


def test_readiness_certification_not_completed_is_noted():
    profile = _profile(certifications=[CandidateCertification(id=1, name="AWS", status="PLANNED")])
    area = evaluate_profile_readiness(profile).area(AREA_CERTIFICATIONS)
    assert area.status == "VERIFIED"
    assert NOTE_CERTIFICATION_NOT_COMPLETED in area.notes


def test_readiness_render_contains_no_profile_content():
    text = render_profile_readiness(evaluate_profile_readiness(_complete_profile()))
    for secret in ("Anna", "Example Freelance", "Job Triage API", "Akademie", "Deutsch"):
        assert secret not in text
    assert "send-package gaps: none" in text


# --- generation quality -------------------------------------------------


def test_structured_experience_included_when_trusted_and_preferred_over_project():
    match, cv, evidence, body = _pipeline(_complete_profile())

    assert [e.experience_id for e in match.relevant_experiences] == [11]
    assert cv.experience[0].company == "Example Freelance GmbH"
    assert "Software Developer (Teilzeit) bei Example Freelance GmbH" in body
    # project is referenced too (experience first, then project)
    assert "das Projekt Job Triage API" in body
    assert body.index("Example Freelance GmbH") < body.index("Job Triage API")
    assert "NO_RELEVANT_EXPERIENCE" not in cv.warnings
    assert "NO_TRUSTED_TITLE" not in cv.warnings


def test_structured_project_included_when_relevant_without_experience():
    profile = _complete_profile(experiences=[])
    match, cv, _, body = _pipeline(profile)
    assert [p.project_id for p in match.relevant_projects] == [21]
    assert [p.name for p in cv.projects] == ["Job Triage API"]
    assert "das Projekt Job Triage API" in body
    assert cv.projects_emphasis == "HIGH"
    assert "NO_RELEVANT_EXPERIENCE" in cv.warnings


def test_irrelevant_project_not_claimed():
    profile = _complete_profile(
        experiences=[],
        projects=[CandidateProject(id=22, name="Garden Planner", technologies=["Kotlin"])],
    )
    _, cv, _, body = _pipeline(profile)
    assert cv.projects == []
    assert "Garden Planner" not in body
    assert "NO_RELEVANT_PROJECTS" in cv.warnings


def test_language_rendered_only_with_known_level_and_education_never_rendered():
    _, cv, evidence, body = _pipeline(_complete_profile())
    assert "Deutschkenntnisse auf B2-Niveau" in body
    assert "Englisch" not in body  # UNKNOWN level: listed in CV, never claimed in letter
    assert {item.language for item in cv.languages} == {"Deutsch", "Englisch"}
    # Education is in the CV with its honest completion flag, never in the letter.
    assert cv.education[0].completed is False
    assert "Akademie" not in body
    assert "Abschluss" not in body
    assert all(c.source_entity != "candidate_education" for c in evidence.allowed_claims)


@pytest.mark.parametrize(
    ("source", "confidence"),
    [("INFERRED", "CONFIRMED"), ("IMPORTED", "CONFIRMED"), ("USER_CONFIRMED", "UNCONFIRMED")],
)
def test_untrusted_facts_excluded_everywhere(source, confidence):
    untrusted = dict(source=source, confidence=confidence)
    profile = _profile(
        experiences=[
            CandidateExperience(
                id=1, company="Fake Corp", job_title="Lead", technologies=["Python"], **untrusted
            )
        ],
        projects=[CandidateProject(id=2, name="Ghost Project", technologies=["Git"], **untrusted)],
        education=[CandidateEducation(id=3, institution="Fake Uni", completed=True, **untrusted)],
        languages=[CandidateLanguage(id=4, language="Deutsch", level="C2", **untrusted)],
        certifications=[CandidateCertification(id=5, name="Fake Cert", status="COMPLETED")],
    )
    profile.certifications[0].source = source
    profile.certifications[0].confidence = confidence
    _, cv, evidence, body = _pipeline(profile)

    assert cv.experience == [] and cv.projects == [] and cv.education == []
    assert cv.languages == [] and cv.certifications == []
    claims = " ".join(c.claim for c in evidence.allowed_claims)
    for fake in ("Fake Corp", "Ghost Project", "Fake Uni", "C2", "Fake Cert"):
        assert fake not in body
        assert fake not in claims


def test_missing_facts_produce_warnings_not_text():
    """The live 13D shape: only skills are claimable; every missing area is
    a warning and the letter claims nothing but skills."""
    _, cv, _, body = _pipeline(_profile())
    for code in (
        "NO_TRUSTED_TITLE",
        "NO_RELEVANT_EXPERIENCE",
        "NO_RELEVANT_PROJECTS",
        "NO_TRUSTED_EDUCATION",
        "NO_TRUSTED_LANGUAGES",
    ):
        assert code in cv.warnings
    assert "Ich bringe Kenntnisse in Python, Git, REST API mit" in body
    for forbidden in ("Erfahrung als", "Projekt", "Muttersprache", "Niveau", "freelancer"):
        assert forbidden not in body


def test_trusted_experience_without_match_overlap_warns():
    profile = _complete_profile(
        experiences=[
            CandidateExperience(id=12, company="Bakery", job_title="Baker", technologies=[])
        ]
    )
    _, cv, _, body = _pipeline(profile)
    assert "NO_MATCH_RELEVANT_EXPERIENCE" in cv.warnings
    assert "Bakery" not in body  # only match-relevant experience is cited in the letter
    assert cv.experience[0].company == "Bakery"  # full trusted history stays in the CV
