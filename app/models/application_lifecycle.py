"""Stage 15: application lifecycle vocabulary and read DTOs.

An *application* is the candidate's real attempt to get one specific job,
tracked from discovery through the employer's response. It is a separate
entity from `JobRecord` on purpose: `JobRecord.status` (`ApplicationStatus`,
Stage 1) is a coarse per-posting flag bound to a DB-local job row, while an
application is identified by its stable external vacancy identity
(source + source reference, Stage 14D `VacancyIdentity`) and carries an
immutable, evidence-backed event history. Nothing in Stage 15 writes
`JobRecord.status`.

Every value below is a closed vocabulary; the database enforces the same
sets with CHECK constraints (see `app.db.models.APPLICATION_CHECKS`).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApplicationLifecycleStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    MATCHED = "MATCHED"
    PACKAGE_READY = "PACKAGE_READY"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    RECEIVED = "RECEIVED"
    UNDER_REVIEW = "UNDER_REVIEW"
    INTERVIEW = "INTERVIEW"
    TECHNICAL_INTERVIEW = "TECHNICAL_INTERVIEW"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class ApplicationEventType(StrEnum):
    # The application row was created (old_status is NULL).
    CREATED = "CREATED"
    # A policy-validated status change.
    STATUS_CHANGED = "STATUS_CHANGED"
    # An operator override outside the transition table (reason required).
    STATUS_OVERRIDDEN = "STATUS_OVERRIDDEN"
    # Correlated inbound correspondence that did NOT change the status
    # (duplicate-state receipt, request for information, out-of-order mail).
    INBOUND_MESSAGE = "INBOUND_MESSAGE"


class ApplicationEventOrigin(StrEnum):
    SYSTEM = "SYSTEM"
    GMAIL = "GMAIL"
    PORTAL = "PORTAL"
    OPERATOR = "OPERATOR"
    IMPORT = "IMPORT"


class ApplicationChannel(StrEnum):
    EMPLOYER_PORTAL = "EMPLOYER_PORTAL"
    EMAIL = "EMAIL"
    JOB_BOARD = "JOB_BOARD"
    OTHER = "OTHER"


class ResponseClassification(StrEnum):
    """Stage 15 meaning of one inbound application-related message."""

    RECEIPT_CONFIRMATION = "RECEIPT_CONFIRMATION"
    UNDER_REVIEW_NOTICE = "UNDER_REVIEW_NOTICE"
    INTERVIEW_INVITATION = "INTERVIEW_INVITATION"
    TECHNICAL_INTERVIEW_INVITATION = "TECHNICAL_INTERVIEW_INVITATION"
    OFFER = "OFFER"
    REJECTION = "REJECTION"
    REQUEST_FOR_INFORMATION = "REQUEST_FOR_INFORMATION"
    OTHER_APPLICATION_MESSAGE = "OTHER_APPLICATION_MESSAGE"
    UNKNOWN = "UNKNOWN"


class Application(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    candidate_profile_id: int | None
    job_id: int | None
    company_name: str
    role_title: str
    location: str | None
    source: str
    source_reference: str | None
    external_job_id: str | None
    vacancy_fingerprint: str | None
    application_channel: ApplicationChannel
    destination_url: str | None
    employer_domains: tuple[str, ...]
    review_package_id: int | None
    current_status: ApplicationLifecycleStatus
    submitted_at: datetime | None
    received_at: datetime | None
    last_status_changed_at: datetime
    last_employer_contact_at: datetime | None
    next_followup_at: datetime | None
    followup_required: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ApplicationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    application_id: int
    sequence: int
    event_type: ApplicationEventType
    old_status: ApplicationLifecycleStatus | None
    new_status: ApplicationLifecycleStatus | None
    origin: ApplicationEventOrigin
    actor: str
    reason: str | None
    source_reference: str | None
    occurred_at: datetime | None
    recorded_at: datetime
    evidence_hash: str | None
    evidence_summary: str | None
    correlation_key: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApplicationDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    application: Application
    events: list[ApplicationEvent]
