from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl

from app.models.application_status import ApplicationStatus


class Job(BaseModel):
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str = ""
    url: HttpUrl
    description: str = ""
    skills: list[str] = Field(default_factory=list)
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)


class JobScore(BaseModel):
    score: int = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    matched_must_have: list[str] = Field(default_factory=list)
    missing_must_have: list[str] = Field(default_factory=list)
    matched_nice_to_have: list[str] = Field(default_factory=list)
    recommendation: str
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
    score: int
    recommendation: str
    status: ApplicationStatus
    first_seen_at: datetime
    last_seen_at: datetime


class StatusUpdateRequest(BaseModel):
    status: ApplicationStatus
