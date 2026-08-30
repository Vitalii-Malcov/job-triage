"""Tailored CV Draft DTOs (Stage 6C).

**Core safety principle.** A CV draft contains only trusted candidate
facts. Every nested fact passed `is_usable_for_generation`; every top-level
header field passed `is_top_level_fact_usable_for_generation` — the exact
same Stage 6A functions, never reimplemented or weakened here (Stage 6C
spec section 2).

**6C consumes 6B, it does not re-match.** Skill selection comes from
`CandidateJobMatch.matched_requirements` (SKILL type, MATCH status only —
never MISSING/UNKNOWN, see section 26); project selection comes from
`CandidateJobMatch.relevant_projects`; experience relevance annotation
comes from `CandidateJobMatch.relevant_experiences`. No skill/language/
education matching logic is reimplemented in this module or in
app.agents.cv_adapter (section 4).

**Full-history sections vs. match-evidenced sections.** Experience,
education, certifications, and languages are standard CV sections that
convention (and section 23/25's "include trusted CandidateEducation" /
"use trusted CandidateLanguage facts" wording, unqualified by match
relevance — contrast with section 16/21's explicit "use only... from the
match" for skills/projects) expects to show the candidate's full truthful
history, not only what happens to be evidenced by one particular vacancy's
requirements. Only SKILLS and PROJECTS are filtered down to what the
current match actually evidences; EXPERIENCE/EDUCATION/CERTIFICATIONS/
LANGUAGES include every *trusted* entry regardless of match relevance,
annotated with match metadata (matched_skills/emphasis/match_status) where
available — chronology and factual content are never altered by relevance.

**No LLM, no prose invention (section 15/19/29).** Every text field here
is either copied verbatim from a trusted Candidate Profile field or a
short structured label (skill/project name) — never rewritten, expanded,
or inferred.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.candidate_job_match import MatchStatus, RequirementImportance

CVDraftStatus = Literal["DRAFT"]
ProjectsEmphasis = Literal["STANDARD", "HIGH"]

TopLevelSourceField = Literal[
    "first_name",
    "last_name",
    "professional_title",
    "location_city",
    "location_country",
    "professional_summary",
]


class CVTopLevelFact(BaseModel):
    """Provenance wrapper for one top-level Candidate Profile fact (M-01
    fix). Nested items (skills/experience/projects/education/
    certifications/languages) already carry `source_entity`/`source_id`;
    top-level résumé facts (header fields, professional_summary) need the
    same mechanical traceability — "which Candidate Profile record and
    exact field produced this value?" must never require re-deriving from
    Stage 6A's `field_trust` bookkeeping after the fact.

    One wrapper per field, not one shared object for the whole header:
    `first_name`, `professional_title`, and `location_city` each carry
    independent Stage 6A `field_trust` and can be trusted/untrusted
    completely independently of one another (see
    `app.models.candidate_profile.TOP_LEVEL_TRUST_FIELDS`) — a single
    per-header provenance record would misrepresent that.
    """

    value: str
    source_entity: Literal["candidate_profile"] = "candidate_profile"
    source_id: int
    source_field: TopLevelSourceField
    profile_version: int


class CVHeader(BaseModel):
    """Only trusted top-level Candidate Profile facts (section 12). A
    field absent here means it was either never set or not
    generation-usable — never a placeholder, never inferred. Each present
    field is a `CVTopLevelFact`, not a bare string — see that class's
    docstring for why provenance is per-field, not per-header.

    No contact fields (email/phone/GitHub/LinkedIn/website) — the
    Candidate Profile schema does not model them in Stage 6A/6B/6C v1 (see
    CONTACT_DATA_NOT_MODELED in `TailoredCVDraftData.warnings`, always
    present, and section 13's explicit instruction not to invent, scrape,
    or hardcode them).
    """

    first_name: CVTopLevelFact | None = None
    last_name: CVTopLevelFact | None = None
    professional_title: CVTopLevelFact | None = None
    location_city: CVTopLevelFact | None = None
    location_country: CVTopLevelFact | None = None


class CVSkillItem(BaseModel):
    """One skill line — always a MATCH-status SKILL requirement from the
    pinned match; MISSING/UNKNOWN skills never reach this model (section
    26). `category`/`proficiency`/`years_experience` are copied verbatim
    from the trusted CandidateSkill row — never upgraded (section 17).
    """

    text: str
    category: str
    proficiency: str
    years_experience: float | None
    source_entity: Literal["candidate_skill"] = "candidate_skill"
    source_id: int
    match_requirement: str
    importance: RequirementImportance


class CVExperienceItem(BaseModel):
    """A trusted CandidateExperience entry. `technologies`/
    `responsibilities`/`achievements`/`description` are copied verbatim —
    never rewritten or embellished (section 19). `matched_skills`/
    `emphasis` are match-relevance annotations only; they never alter the
    factual content above.
    """

    source_entity: Literal["candidate_experience"] = "candidate_experience"
    source_id: int
    company: str
    job_title: str
    start_date: date | None
    end_date: date | None
    is_current: bool
    location: str | None
    description: str | None
    responsibilities: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    matched_skills: list[str] = Field(default_factory=list)
    emphasis: Literal["HIGH", "STANDARD"] = "STANDARD"


class CVProjectItem(BaseModel):
    """A CandidateProject selected because it appears in the pinned
    match's `relevant_projects` (section 21) — an unrelated project never
    reaches this model. `matched_skills` is copied from the match, not
    re-derived.
    """

    source_entity: Literal["candidate_project"] = "candidate_project"
    source_id: int
    name: str
    description: str | None
    role: str | None
    technologies: list[str] = Field(default_factory=list)
    repository_url: str | None
    demo_url: str | None
    start_date: date | None
    end_date: date | None
    highlights: list[str] = Field(default_factory=list)
    matched_skills: list[str] = Field(default_factory=list)


class CVEducationItem(BaseModel):
    """A trusted CandidateEducation entry, full history (see module
    docstring). `completed` is copied verbatim — an incomplete degree is
    never rendered as completed (section 23).
    """

    source_entity: Literal["candidate_education"] = "candidate_education"
    source_id: int
    institution: str
    program: str | None
    degree: str | None
    field_of_study: str | None
    start_date: date | None
    end_date: date | None
    completed: bool
    location: str | None


class CVCertificationItem(BaseModel):
    """A trusted CandidateCertification entry. `status` is copied verbatim
    — an IN_PROGRESS certification is never rendered as completed (section
    24).
    """

    source_entity: Literal["candidate_certification"] = "candidate_certification"
    source_id: int
    name: str
    issuer: str | None
    issued_date: date | None
    expires_date: date | None
    status: str


class CVLanguageItem(BaseModel):
    """A trusted CandidateLanguage entry, full history (see module
    docstring). `level` is copied verbatim from the Candidate Profile —
    never upgraded toward a job's required level (section 25/27):
    candidate B1 against a job requiring B2 is still rendered as B1.
    `matched_requirement`/`match_status` are internal metadata *about* a
    job requirement this language relates to (e.g. "German B2" / PARTIAL)
    — present only when the pinned match actually evaluated this language
    against a job requirement; they never change `level` itself.
    """

    source_entity: Literal["candidate_language"] = "candidate_language"
    source_id: int
    language: str
    level: str
    certificate: str | None
    matched_requirement: str | None = None
    match_status: MatchStatus | None = None


class TailoredCVDraftData(BaseModel):
    """The computed content of a CV draft — everything
    app.agents.cv_adapter.compute_cv_draft produces, before persistence
    assigns an id/created_at. Mirrors CandidateJobMatchData ->
    CandidateJobMatch (Stage 6B) and CompanyResearchData ->
    CompanyResearchResponse (Company Research Agent).
    """

    job_id: int
    match_id: int
    # Snapshot pins (section 6/7/8/9) — pinned at draft-generation time,
    # never silently reinterpreted against a later profile/job/match state.
    candidate_profile_version: int
    match_algorithm_version: str
    cv_adapter_version: str
    status: CVDraftStatus = "DRAFT"

    header: CVHeader
    professional_summary: CVTopLevelFact | None = None
    section_order: list[str] = Field(default_factory=list)
    projects_emphasis: ProjectsEmphasis = "STANDARD"

    skills: list[CVSkillItem] = Field(default_factory=list)
    experience: list[CVExperienceItem] = Field(default_factory=list)
    projects: list[CVProjectItem] = Field(default_factory=list)
    education: list[CVEducationItem] = Field(default_factory=list)
    certifications: list[CVCertificationItem] = Field(default_factory=list)
    languages: list[CVLanguageItem] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)


class TailoredCVDraft(TailoredCVDraftData):
    """GET/POST /api/v1/jobs/{job_id}/cv-draft and GET
    /api/v1/cv-drafts/{draft_id} response shape. Immutable once created —
    see app/db/candidate_cv_draft_repository.py.
    """

    id: int
    created_at: datetime


class CVDraftRequest(BaseModel):
    """POST /api/v1/jobs/{job_id}/cv-draft body. `match_id` is required —
    a draft must be pinned to one specific, caller-chosen persisted match,
    never an implicit "whatever GET /jobs/{id}/match currently returns"
    (section 5).
    """

    match_id: int
    force_recompute: bool = False
