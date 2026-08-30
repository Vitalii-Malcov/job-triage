"""Deterministic CV Draft adaptation (Stage 6C).

**Purpose.** Converts Candidate Profile + a pinned Candidate Job Match into
a structured, evidence-first `TailoredCVDraftData` — which candidate facts
belong in the CV for this vacancy, in what order, with which relevance
emphasis, and with full provenance back to the Candidate Profile row that
justifies each line. This module performs no I/O of its own —
`compute_cv_draft` is a pure function over already-loaded data, exactly
like app.agents.candidate_job_matcher.compute_match.

**6C consumes 6B; it does not re-match (spec section 3/4).** Skill
selection is read directly off `match.matched_requirements` (SKILL type —
by construction from Stage 6B these are always MATCH-status only, since a
SKILL requirement is either MATCH or MISSING, never PARTIAL/UNKNOWN; the
MISSING/UNKNOWN case is filtered defensively anyway, see
`_select_skills`). Project selection is read directly off
`match.relevant_projects`. No skill/language/education matching,
normalization, or scoring logic is reimplemented here — this module never
imports app.agents.job_scorer.normalize_skill or
app.agents.requirement_extractor.

**Hard trust rule, reused from Stage 6A (not reimplemented).**
`app.db.candidate_profile_repository.to_candidate_profile_response`
deliberately does NOT filter by trust — GET /candidate-profile must show
everything the candidate entered, trusted or not. This module is
therefore the one that filters: every profile entity
(skill/experience/project/education/certification/language) is checked
against `is_usable_for_generation(source, confidence)` before being
eligible for inclusion, and every top-level header/summary field against
`is_top_level_fact_usable_for_generation`. An untrusted fact is excluded
exactly as if it did not exist in the profile.

**Full-history vs. match-evidenced sections — see app/models/cv_draft.py's
module docstring** for the exact rule (skills/projects are filtered to
what the pinned match evidences; experience/education/certifications/
languages show the full trusted history, annotated with match-relevance
metadata where available).

**No LLM, no network, no external actions (section 29/42).** Every text
value is either copied verbatim from a trusted Candidate Profile field or
a short structured label — never rewritten, expanded, or invented.

**CV_ADAPTER_VERSION.** `"v1"` — bumped whenever selection/ordering logic
here changes in a way that would produce a different draft for the same
inputs (mirrors app.agents.candidate_job_matcher.ALGORITHM_VERSION's
rationale exactly).
"""

from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.candidate_profile import (
    CandidateProfile,
    is_top_level_fact_usable_for_generation,
    is_usable_for_generation,
)
from app.models.cv_draft import (
    CVCertificationItem,
    CVEducationItem,
    CVExperienceItem,
    CVHeader,
    CVLanguageItem,
    CVProjectItem,
    CVSkillItem,
    CVTopLevelFact,
    TailoredCVDraftData,
)

CV_ADAPTER_VERSION = "v1"


class CVDraftMatchNotFoundError(Exception):
    """`match_id` does not correspond to any persisted CandidateJobMatch
    (Stage 6C section 56) — mapped to 404 by app/api/routes.py.
    """

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(f"No candidate job match found with id={match_id}.")


class CVDraftMatchJobMismatchError(Exception):
    """`match_id` exists but its `job_id` does not equal the job_id in the
    request URL (section 5/48). Mapped to 422, not 409: the request itself
    names a structurally inconsistent job_id + match_id combination — a
    request-validation failure, not a missing resource (404) or a state
    that changed since the match was computed (409, see
    CVDraftProfileChangedError/CVDraftJobChangedError below). This 422
    vs. 409 split is Stage 6C's one deliberate, documented choice for
    section 48's "choose and document one consistent semantic."
    """

    def __init__(self, match_id: int, job_id: int) -> None:
        self.match_id = match_id
        self.job_id = job_id
        super().__init__(f"Match {match_id} does not belong to job {job_id}.")


class CVDraftProfileChangedError(Exception):
    """The current CandidateProfileRecord.profile_version no longer equals
    the pinned match's candidate_profile_version (section 7) — 409.
    Carries only version numbers (technical metadata), never profile
    content — app/api/routes.py surfaces `match_profile_version`/
    `current_profile_version` in the response detail, nothing else.
    """

    def __init__(self, match_profile_version: int, current_profile_version: int) -> None:
        self.match_profile_version = match_profile_version
        self.current_profile_version = current_profile_version
        super().__init__("Candidate profile changed since this match was computed.")


class CVDraftJobChangedError(Exception):
    """The job's current content fingerprint no longer equals the pinned
    match's job_snapshot_fingerprint (section 8) — 409. No job title,
    description, or skill list in the message.
    """

    def __init__(self) -> None:
        super().__init__("Job changed since this match was computed.")


_DEFAULT_SECTION_ORDER = (
    "HEADER",
    "SUMMARY",
    "SKILLS",
    "EXPERIENCE",
    "PROJECTS",
    "EDUCATION",
    "CERTIFICATIONS",
    "LANGUAGES",
)
_PROJECTS_EMPHASIZED_SECTION_ORDER = (
    "HEADER",
    "SUMMARY",
    "SKILLS",
    "PROJECTS",
    "EXPERIENCE",
    "EDUCATION",
    "CERTIFICATIONS",
    "LANGUAGES",
)

_HEADER_FIELDS = (
    "first_name",
    "last_name",
    "professional_title",
    "location_city",
    "location_country",
)

_TOP_LEVEL_IMPORTANCE_ORDER = {"REQUIRED": 0, "PREFERRED": 1, "UNKNOWN": 2}


def _trusted(items: list) -> list:
    return [item for item in items if is_usable_for_generation(item.source, item.confidence)]


def _top_level_fact(profile: CandidateProfile, field_name: str) -> CVTopLevelFact | None:
    """Build a provenance-carrying top-level fact for `field_name`, or
    None if it isn't generation-usable (M-01 fix) — never a bare string.
    `source_id`/`profile_version` are read from the actual loaded profile,
    never hardcoded, even though Stage 6A's singleton constraint currently
    guarantees `profile.id == 1`.
    """
    if not is_top_level_fact_usable_for_generation(profile, field_name):
        return None
    value = getattr(profile, field_name)
    if value is None:
        return None
    value = value.strip() if isinstance(value, str) else value
    if not value:
        return None
    return CVTopLevelFact(
        value=value,
        source_id=profile.id,
        source_field=field_name,
        profile_version=profile.profile_version,
    )


def _build_header(profile: CandidateProfile) -> CVHeader:
    values = {field: _top_level_fact(profile, field) for field in _HEADER_FIELDS}
    return CVHeader(**values)


def _build_summary(profile: CandidateProfile) -> CVTopLevelFact | None:
    return _top_level_fact(profile, "professional_summary")


def _select_skills(match: CandidateJobMatch, profile: CandidateProfile) -> list[CVSkillItem]:
    """Only MATCH-status SKILL requirements from the pinned match (section
    16/26) — REQUIRED first, PREFERRED next, stable within each group in
    the match's own requirement order.
    """
    skills_by_id = {skill.id: skill for skill in profile.skills}
    candidates = [
        requirement
        for requirement in match.matched_requirements
        if requirement.requirement_type == "SKILL" and requirement.match_status == "MATCH"
    ]
    ordered = sorted(candidates, key=lambda r: _TOP_LEVEL_IMPORTANCE_ORDER.get(r.importance, 99))

    items: list[CVSkillItem] = []
    for requirement in ordered:
        if not requirement.candidate_evidence:
            continue
        skill_id = requirement.candidate_evidence[0].entity_id
        skill = skills_by_id.get(skill_id)
        if skill is None:
            # Defensive only: the pinned match's profile_version must equal
            # the current profile's version (enforced by the caller before
            # compute_cv_draft ever runs), so every evidence id it names is
            # guaranteed to exist. Skipped rather than raised, matching this
            # module's "never fail a draft over one unresolved line" stance.
            continue
        items.append(
            CVSkillItem(
                text=skill.name,
                category=skill.category,
                proficiency=skill.proficiency,
                years_experience=skill.years_experience,
                source_id=skill.id,
                match_requirement=requirement.requirement,
                importance=requirement.importance,
            )
        )
    return items


def _experience_sort_key(experience) -> tuple:
    # Reverse-chronological (section 20): current role(s) first, then by
    # start_date descending; an experience with no start_date sorts last.
    return (
        0 if experience.is_current else 1,
        experience.start_date is None,
        -experience.start_date.toordinal() if experience.start_date else 0,
    )


def _select_experience(
    match: CandidateJobMatch, profile: CandidateProfile
) -> list[CVExperienceItem]:
    trusted = _trusted(profile.experiences)
    relevance_by_id = {exp.experience_id: exp.matched_skills for exp in match.relevant_experiences}
    ordered = sorted(trusted, key=_experience_sort_key)

    items = []
    for experience in ordered:
        matched_skills = relevance_by_id.get(experience.id, [])
        items.append(
            CVExperienceItem(
                source_id=experience.id,
                company=experience.company,
                job_title=experience.job_title,
                start_date=experience.start_date,
                end_date=experience.end_date,
                is_current=experience.is_current,
                location=experience.location,
                description=experience.description,
                responsibilities=list(experience.responsibilities),
                achievements=list(experience.achievements),
                technologies=list(experience.technologies),
                matched_skills=list(matched_skills),
                emphasis="HIGH" if matched_skills else "STANDARD",
            )
        )
    return items


def _select_projects(match: CandidateJobMatch, profile: CandidateProfile) -> list[CVProjectItem]:
    """Only projects present in the pinned match's `relevant_projects`
    (section 21) — ranked by number of matched skills, descending.
    """
    projects_by_id = {project.id: project for project in profile.projects}
    ranked = sorted(match.relevant_projects, key=lambda p: len(p.matched_skills), reverse=True)

    items = []
    for relevant in ranked:
        project = projects_by_id.get(relevant.project_id)
        if project is None:
            continue
        items.append(
            CVProjectItem(
                source_id=project.id,
                name=project.name,
                description=project.description,
                role=project.role,
                technologies=list(project.technologies),
                repository_url=project.repository_url,
                demo_url=project.demo_url,
                start_date=project.start_date,
                end_date=project.end_date,
                highlights=list(project.highlights),
                matched_skills=list(relevant.matched_skills),
            )
        )
    return items


def _education_sort_key(education) -> tuple:
    negate = -1
    if education.end_date is not None:
        return (0, negate * education.end_date.toordinal())
    if education.start_date is not None:
        return (0, negate * education.start_date.toordinal())
    return (1, 0)


def _select_education(profile: CandidateProfile) -> list[CVEducationItem]:
    trusted = sorted(_trusted(profile.education), key=_education_sort_key)
    return [
        CVEducationItem(
            source_id=education.id,
            institution=education.institution,
            program=education.program,
            degree=education.degree,
            field_of_study=education.field_of_study,
            start_date=education.start_date,
            end_date=education.end_date,
            completed=education.completed,
            location=education.location,
        )
        for education in trusted
    ]


def _select_certifications(profile: CandidateProfile) -> list[CVCertificationItem]:
    return [
        CVCertificationItem(
            source_id=cert.id,
            name=cert.name,
            issuer=cert.issuer,
            issued_date=cert.issued_date,
            expires_date=cert.expires_date,
            status=cert.status,
        )
        for cert in _trusted(profile.certifications)
    ]


def _language_requirement_lookup(match: CandidateJobMatch) -> dict[int, RequirementMatch]:
    lookup: dict[int, RequirementMatch] = {}
    for requirement in (
        match.matched_requirements + match.partial_requirements + match.unknown_requirements
    ):
        if requirement.requirement_type != "LANGUAGE":
            continue
        for ref in requirement.candidate_evidence:
            if ref.entity_type == "LANGUAGE":
                lookup.setdefault(ref.entity_id, requirement)
    return lookup


def _select_languages(match: CandidateJobMatch, profile: CandidateProfile) -> list[CVLanguageItem]:
    trusted = _trusted(profile.languages)
    requirement_lookup = _language_requirement_lookup(match)

    items = []
    for language in trusted:
        requirement = requirement_lookup.get(language.id)
        items.append(
            CVLanguageItem(
                source_id=language.id,
                language=language.language,
                level=language.level,
                certificate=language.certificate,
                matched_requirement=requirement.requirement if requirement else None,
                match_status=requirement.match_status if requirement else None,
            )
        )
    return items


def compute_cv_draft(profile: CandidateProfile, match: CandidateJobMatch) -> TailoredCVDraftData:
    header = _build_header(profile)
    summary = _build_summary(profile)
    skills = _select_skills(match, profile)
    trusted_experience_count = len(_trusted(profile.experiences))
    experience = _select_experience(match, profile)
    projects = _select_projects(match, profile)
    education = _select_education(profile)
    certifications = _select_certifications(profile)
    languages = _select_languages(match, profile)

    # Section 38: a single, explicit, non-"clever" deterministic rule —
    # emphasize PROJECTS ahead of EXPERIENCE only when the candidate has no
    # trusted professional experience at all but does have at least one
    # match-relevant project (the classic junior/career-changer signal
    # this project's own data can actually support, as opposed to
    # sniffing "junior" out of free-text job titles).
    projects_emphasis = "HIGH" if trusted_experience_count == 0 and projects else "STANDARD"
    section_order = list(
        _PROJECTS_EMPHASIZED_SECTION_ORDER
        if projects_emphasis == "HIGH"
        else _DEFAULT_SECTION_ORDER
    )

    warnings: list[str] = []
    if header.first_name is None and header.last_name is None:
        warnings.append("NO_TRUSTED_NAME")
    if header.professional_title is None:
        warnings.append("NO_TRUSTED_TITLE")
    if not skills:
        warnings.append("NO_RELEVANT_SKILLS")
    if trusted_experience_count == 0:
        warnings.append("NO_RELEVANT_EXPERIENCE")
    # Always present in v1 (section 13/37) — the Candidate Profile schema
    # never models contact fields at all, not a situational gap.
    warnings.append("CONTACT_DATA_NOT_MODELED")

    return TailoredCVDraftData(
        job_id=match.job_id,
        match_id=match.id,
        candidate_profile_version=match.candidate_profile_version,
        match_algorithm_version=match.algorithm_version,
        cv_adapter_version=CV_ADAPTER_VERSION,
        header=header,
        professional_summary=summary,
        section_order=section_order,
        projects_emphasis=projects_emphasis,
        skills=skills,
        experience=experience,
        projects=projects,
        education=education,
        certifications=certifications,
        languages=languages,
        warnings=warnings,
    )
