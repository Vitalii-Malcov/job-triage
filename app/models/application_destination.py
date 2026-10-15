"""Where an application for ONE vacancy may be delivered (Stage 14A).

**Groundwork only -- nothing here sends anything.** This is a typed,
provenance-carrying description of a vacancy's application channel that a
future Stage 14B package builder consumes. No code in this project
transmits to an `ApplicationDestination`.

**Vacancy-scoped, not company-scoped.** Company Research
(`app.models.company_research`) describes an employer; a destination
describes how THIS posting accepts applications (an employer can accept a
portal for posted jobs and email only for unsolicited ones -- the exact
E362/B finding). It therefore lives beside, not inside, company research.

**Fail closed.**
- `kind="UNKNOWN"` is the default answer and requires a `reason`.
- A destination is `VERIFIED` only with an `https://` employer or operator
  `source_url` and a `checked_at`; aggregators/job boards can never verify.
- An email becomes send-ready (`is_email_send_ready`) only when an operator
  verified it against the employer's own vacancy-specific source. A scraped
  or general-inbox address (e.g. a careers@ address for questions or
  unsolicited applications) is never send-ready, whatever its other fields
  say.
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DestinationKind = Literal["EMAIL", "APPLICATION_PORTAL", "CAREERS_PAGE", "UNKNOWN"]

DestinationSourceType = Literal[
    "EMPLOYER_JOB_AD",  # the employer's own page for this vacancy
    "EMPLOYER_CAREERS_SITE",  # employer careers pages/FAQ, not vacancy-specific
    "JOB_BOARD_LISTING",  # e.g. Bundesagentur, StepStone, XING listing
    "AGGREGATOR",  # third-party re-publication
    "OPERATOR_ENTRY",  # typed in by the operator without a public page
    "UNKNOWN",
]

VerificationStatus = Literal["VERIFIED", "UNVERIFIED", "CONTRADICTED", "NOT_FOUND"]
DestinationConfidence = Literal["HIGH", "MEDIUM", "LOW", "NONE"]
VerifiedBy = Literal["OPERATOR", "NONE"]

EMPLOYER_SOURCES: frozenset[str] = frozenset({"EMPLOYER_JOB_AD", "EMPLOYER_CAREERS_SITE"})
_VERIFIABLE_SOURCES: frozenset[str] = EMPLOYER_SOURCES | {"OPERATOR_ENTRY"}

# Shape check only (same stance as app.services.follow_up_recipient).
_EMAIL_SHAPE_RE = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$")
_CONTROL_CHARS = ("\r", "\n", "\x00")


class ApplicationDestination(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: int
    kind: DestinationKind = "UNKNOWN"
    # An email address for EMAIL, an https URL for PORTAL/CAREERS_PAGE,
    # None for UNKNOWN.
    value: str | None = None
    source_url: str | None = None
    source_type: DestinationSourceType = "UNKNOWN"
    verification_status: VerificationStatus = "UNVERIFIED"
    confidence: DestinationConfidence = "NONE"
    verified_by: VerifiedBy = "NONE"
    # True only when the source states this channel is for THIS vacancy
    # (not a general inbox / unsolicited-application address).
    vacancy_specific: bool = False
    provenance: str = Field(default="", max_length=500)
    checked_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _validate(self) -> "ApplicationDestination":
        for text in (self.value, self.source_url):
            if text is not None and any(ch in text for ch in _CONTROL_CHARS):
                raise ValueError("destination fields must not contain control characters")
        if self.source_url is not None and not self.source_url.startswith("https://"):
            raise ValueError("source_url must be an https:// URL")

        if self.kind == "UNKNOWN":
            if self.value is not None:
                raise ValueError("an UNKNOWN destination carries no value")
            if not (self.reason or "").strip():
                raise ValueError("an UNKNOWN destination requires a reason")
            if self.verification_status == "VERIFIED":
                raise ValueError("an UNKNOWN destination cannot be VERIFIED")
        elif self.kind == "EMAIL":
            if not self.value or not _EMAIL_SHAPE_RE.match(self.value):
                raise ValueError("EMAIL destination requires a well-formed address")
        elif not (self.value or "").startswith("https://"):
            raise ValueError(f"{self.kind} destination requires an https:// URL value")

        if self.verification_status == "VERIFIED":
            if self.source_type not in _VERIFIABLE_SOURCES:
                raise ValueError("only employer or operator sources can verify a destination")
            if self.checked_at is None:
                raise ValueError("a VERIFIED destination requires checked_at")
            if self.source_type != "OPERATOR_ENTRY" and self.source_url is None:
                raise ValueError("a VERIFIED employer destination requires source_url")
        return self


def unknown_destination(job_id: int, reason: str, **fields) -> ApplicationDestination:
    return ApplicationDestination(job_id=job_id, kind="UNKNOWN", reason=reason, **fields)


def is_channel_verified(destination: ApplicationDestination | None) -> bool:
    """A known, verified, vacancy-specific channel from an employer or
    operator source -- what a Stage 14B package may target for HUMAN
    delivery (portal upload or a human-sent email)."""
    return (
        destination is not None
        and destination.kind != "UNKNOWN"
        and destination.verification_status == "VERIFIED"
        and destination.vacancy_specific
        and destination.confidence in ("HIGH", "MEDIUM")
    )


def is_email_send_ready(destination: ApplicationDestination | None) -> bool:
    """Stricter than `is_channel_verified`: an email address a future,
    separately authorized stage could address a draft to. Requires an
    operator verification against the employer's own vacancy source and
    HIGH confidence; a scraped address can never satisfy `verified_by`."""
    return (
        is_channel_verified(destination)
        and destination.kind == "EMAIL"
        and destination.verified_by == "OPERATOR"
        and destination.source_type in EMPLOYER_SOURCES
        and destination.confidence == "HIGH"
    )
