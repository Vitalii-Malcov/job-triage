"""Candidate <-> Job match analysis DTOs (Stage 6B).

**Evidence-domain separation, extended.** Stage 6A's Candidate Profile /
Job data / Company Research separation (see
app/db/candidate_profile_repository.py's module docstring) still applies
here: this module combines all three read-only to *analyze* a match, but
never lets one domain's data masquerade as another's. In particular Company
Research contributes nothing but its own `id` (for traceability) — Company
Research v1 has deliberately limited, sometimes-absent evidence, and its
technologies/facts are never promoted into job requirements or candidate
skill matches (Stage 6B spec section 19).

**Hard trust rule, reused from Stage 6A (not reimplemented).** Every piece
of candidate-side evidence referenced in a match — a matched skill,
relevant experience, relevant project, relevant education, relevant
language, or safe candidate claim — passed
`app.models.candidate_profile.is_usable_for_generation` before it was
allowed to appear here. An untrusted fact (INFERRED, IMPORTED, UNKNOWN
source, or non-CONFIRMED confidence) is treated exactly as if it were
absent from the profile — never as weaker evidence, never as a hint.

**No LLM, no embeddings, no network.** Every value in this module is
produced by app.agents.candidate_job_matcher's deterministic, literal-match
algorithm (see that module for the score formula and match-status rules).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

RequirementType = Literal["SKILL", "LANGUAGE", "EDUCATION"]
RequirementImportance = Literal["REQUIRED", "PREFERRED", "UNKNOWN"]
MatchStatus = Literal["MATCH", "PARTIAL", "MISSING", "UNKNOWN"]
EvidenceEntityType = Literal["SKILL", "EXPERIENCE", "EDUCATION", "PROJECT", "LANGUAGE"]
ClaimType = Literal["SKILL", "EXPERIENCE", "PROJECT", "EDUCATION", "LANGUAGE"]


class CandidateEvidenceRef(BaseModel):
    """A pointer back to exactly one Candidate Profile row that justifies a
    match decision — never a bare score or a black-box "trust me" (section
    16: "Why did we say this?" must always be answerable).
    """

    entity_type: EvidenceEntityType
    entity_id: int
    value: str


class RequirementMatch(BaseModel):
    """One vacancy requirement (a required/preferred skill, an explicit
    language level, or the coarse "completed degree" education signal) and
    how it was resolved against the Candidate Profile. See
    app.agents.candidate_job_matcher module docstring for the exact
    MATCH/PARTIAL/MISSING/UNKNOWN rules per requirement_type.
    """

    requirement: str
    normalized_requirement: str
    requirement_type: RequirementType
    importance: RequirementImportance
    match_status: MatchStatus
    candidate_evidence: list[CandidateEvidenceRef] = Field(default_factory=list)
    job_evidence: list[str] = Field(default_factory=list)
    reason: str


class RelevantExperience(BaseModel):
    experience_id: int
    company: str
    job_title: str
    matched_skills: list[str] = Field(default_factory=list)


class RelevantProject(BaseModel):
    project_id: int
    name: str
    matched_skills: list[str] = Field(default_factory=list)


class RelevantEducation(BaseModel):
    education_id: int
    institution: str
    degree: str | None
    completed: bool


class RelevantLanguage(BaseModel):
    language_id: int
    language: str
    level: str


class SafeCandidateClaim(BaseModel):
    """A structured, traceable factual claim — not polished prose (section
    15: phrasing is Stage 6C's job). Every claim here traces back to
    exactly one trusted Candidate Profile row that was actually used as
    evidence somewhere in this match (an evidence-linked subset of the
    profile, not a dump of every trusted fact the candidate has).
    """

    claim_type: ClaimType
    claim: str
    source_entity: str
    source_id: int
    profile_version: int


class CandidateJobMatchData(BaseModel):
    """The computed content of a match analysis — everything
    app.agents.candidate_job_matcher.compute_match produces, before
    persistence assigns an id/created_at. Mirrors the
    CompanyResearchData -> CompanyResearchResponse split in
    app/models/company_research.py.
    """

    job_id: int
    # CP-M-02/17 precedent: pinned at analysis time, never silently
    # reinterpreted against a later profile edit (section 17).
    candidate_profile_version: int
    # Traceability only (section 19) — Company Research content never
    # feeds scoring/matching in v1, see module docstring.
    company_research_id: int | None
    algorithm_version: str

    overall_score: int = Field(ge=0, le=100)
    coverage_score: int = Field(ge=0, le=100)
    required_skill_score: int = Field(ge=0, le=100)
    preferred_skill_score: int = Field(ge=0, le=100)
    # Not in the spec's suggested field list, but exposed anyway per section
    # 9's "must expose sub-scores so 78% is explainable" — see
    # app.agents.candidate_job_matcher's score-formula docstring for what
    # this measures (whether relevant_experiences/relevant_projects
    # actually back the required skills that matched).
    experience_support_score: int = Field(ge=0, le=100)

    matched_requirements: list[RequirementMatch] = Field(default_factory=list)
    partial_requirements: list[RequirementMatch] = Field(default_factory=list)
    missing_requirements: list[RequirementMatch] = Field(default_factory=list)
    unknown_requirements: list[RequirementMatch] = Field(default_factory=list)

    relevant_experiences: list[RelevantExperience] = Field(default_factory=list)
    relevant_projects: list[RelevantProject] = Field(default_factory=list)
    relevant_education: list[RelevantEducation] = Field(default_factory=list)
    relevant_languages: list[RelevantLanguage] = Field(default_factory=list)

    safe_candidate_claims: list[SafeCandidateClaim] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CandidateJobMatch(CandidateJobMatchData):
    """GET/POST /api/v1/jobs/{job_id}/match response shape."""

    id: int
    created_at: datetime


class MatchRequest(BaseModel):
    """POST /api/v1/jobs/{job_id}/match body."""

    force_recompute: bool = False
