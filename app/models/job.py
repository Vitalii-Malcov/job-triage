from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.models.application_status import ApplicationStatus

SkillSource = Literal["description_extracted", "description_inferred"]
Recommendation = Literal["APPLY", "MAYBE", "SKIP", "NEEDS_ENRICHMENT"]


class Job(BaseModel):
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str = ""
    url: HttpUrl
    description: str = ""
    source_reference: str | None = None
    skills: list[str] = Field(default_factory=list)
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    skill_source: SkillSource | None = None
    # Source-reported posting type (e.g. Bundesagentur's own
    # "stellenangebotsart": "ARBEIT"/"SELBSTAENDIGKEIT"/"AUSBILDUNG"/
    # "PRAKTIKUM_TRAINEE"). None for sources with no such structured field
    # (XING) or when the source omitted it. See
    # app.agents.posting_classifier for how this is used to keep
    # non-employment listings (training-provider courses, apprenticeships,
    # internships) out of the normal APPLY pipeline — Stage 10 shadow-mode
    # pilot finding. S10-004: persisted on JobRecord.posting_type (see
    # app/db/models.py and app/db/repositories.py::upsert_job) so a later
    # re-score that omits this field can reuse the stored value instead
    # of losing the classification — see
    # app.services.collector_runner.score_and_persist's own docstring.
    # S10-003: max_length matches JobRecord.posting_type's VARCHAR(64).
    posting_type: str | None = Field(default=None, max_length=64)

    # S10-RR-001 (Codex Stage 10 re-review, BLOCKING): normalizes BEFORE
    # the max_length constraint above is checked (mode="before" runs
    # ahead of Pydantic's own built-in string validation) -- "" and
    # whitespace-only are collapsed to None, and surrounding whitespace on
    # a real value is stripped, so max_length is enforced against the
    # ACTUAL content, not incidental padding. Critical for
    # app.services.collector_runner.score_and_persist's preserve-on-omit
    # rule (`if job.posting_type is not None: record.posting_type = ...`):
    # an upstream API response that transiently sends "" instead of
    # omitting the field entirely must still be treated as "no signal",
    # never as an explicit instruction to erase an already-persisted
    # classification (e.g. SELBSTAENDIGKEIT).
    @field_validator("posting_type", mode="before")
    @classmethod
    def _normalize_posting_type(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class JobScore(BaseModel):
    score: int = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    matched_must_have: list[str] = Field(default_factory=list)
    missing_must_have: list[str] = Field(default_factory=list)
    matched_nice_to_have: list[str] = Field(default_factory=list)
    recommendation: Recommendation
    data_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    is_duplicate: bool = False


class JobListItem(BaseModel):
    """Compact representation for GET /jobs."""

    id: int
    source: str
    title: str
    company: str
    location: str
    score: int
    recommendation: str
    status: ApplicationStatus
    last_seen_at: datetime


class JobDetail(BaseModel):
    """Full representation for GET /jobs/{id} and PATCH /jobs/{id}/status."""

    id: int
    fingerprint: str
    source: str
    title: str
    company: str
    location: str
    url: str
    description: str
    skills: list[str]
    data_confidence: float
    skill_source: SkillSource | None
    must_have_skills: list[str]
    nice_to_have_skills: list[str]
    score: int
    recommendation: str
    status: ApplicationStatus
    first_seen_at: datetime
    last_seen_at: datetime


class StatusUpdateRequest(BaseModel):
    status: ApplicationStatus
