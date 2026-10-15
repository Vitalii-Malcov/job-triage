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
- `VERIFIED` is a HUMAN statement: the model refuses `verification_status=
  "VERIFIED"` unless `verified_by="OPERATOR"`, with an `https://` employer
  `source_url` (or an operator entry) and a `checked_at`. Research code
  never sets OPERATOR: an employer portal discovered by research stays
  `UNVERIFIED` until a human confirms it. Aggregators/job boards can never
  verify.
- Package-ready kinds are `APPLICATION_PORTAL` and `EMAIL` only. A generic
  `CAREERS_PAGE` is never a package-ready channel.
- An `EMAIL` is package-ready only under the strict email rule
  (`is_email_send_ready`): operator-verified against the employer's own job
  ad for THIS vacancy, HIGH confidence. A scraped, general-inbox or
  company-level address (e.g. a careers@ address for questions or
  unsolicited applications, or one found on a careers FAQ) is never
  package-ready, whatever its other fields say.
- The predicates re-check every condition themselves (defense in depth
  against an instance built with `model_construct`, which skips validation),
  including the URL/email syntax via `is_strict_https_url` /
  `is_valid_email_address`.
"""

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

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
# Only the employer's own ad for THIS vacancy can back a package-ready
# email; a careers-site address is company-level by definition.
_EMAIL_SOURCES: frozenset[str] = frozenset({"EMPLOYER_JOB_AD"})
PACKAGE_READY_KINDS: frozenset[str] = frozenset({"APPLICATION_PORTAL", "EMAIL"})

# Shape check only (same stance as app.services.follow_up_recipient).
_EMAIL_SHAPE_RE = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$")
_CONTROL_CHARS = ("\r", "\n", "\x00")
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PORT_RE = re.compile(r"^[0-9]{1,5}$")


def _has_control_or_space(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in text)


def is_strict_https_url(value: object) -> bool:
    """True only for a syntactically valid `https://` URL with a real DNS
    hostname: scheme exactly `https`, no userinfo, no control characters or
    whitespace, no backslashes, no IP literal / bracketed host, and an
    explicit port (if any) of 1..65535 digits -- an empty `host:` port is
    rejected, not treated as "no port". Pure string parsing -- never
    resolves or connects."""
    if not isinstance(value, str) or not value.startswith("https://"):
        return False
    if _has_control_or_space(value) or "\\" in value:
        return False
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        return False
    netloc = parts.netloc
    if parts.scheme != "https" or not netloc or not hostname:
        return False
    if "@" in netloc or "[" in netloc or "%" in netloc:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if netloc.count(":") > 1:
        return False
    if ":" in netloc:
        # Checked on the raw netloc: urlsplit().port is None for `host:`.
        port = netloc.rsplit(":", 1)[1]
        if not _PORT_RE.match(port) or not 1 <= int(port) <= 65535:
            return False
    host = hostname.rstrip(".")
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2:
        return False
    if not all(_HOST_LABEL_RE.match(label) for label in labels):
        return False
    # The TLD must carry a letter: rejects IPv4 literals such as 10.0.0.1.
    return any(ch.isalpha() for ch in labels[-1])


def is_valid_email_address(value: object) -> bool:
    """Exactly ONE syntactically plausible address: no control characters
    (CR/LF/NUL header injection), no whitespace, no `,`/`;` recipient
    lists, exactly one `@`."""
    return (
        isinstance(value, str)
        and not _has_control_or_space(value)
        and value.count("@") == 1
        and _EMAIL_SHAPE_RE.match(value) is not None
    )


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
        if self.source_url is not None and not is_strict_https_url(self.source_url):
            raise ValueError("source_url must be a valid https:// URL with a hostname")

        if self.kind == "UNKNOWN":
            if self.value is not None:
                raise ValueError("an UNKNOWN destination carries no value")
            if not (self.reason or "").strip():
                raise ValueError("an UNKNOWN destination requires a reason")
            if self.verification_status == "VERIFIED":
                raise ValueError("an UNKNOWN destination cannot be VERIFIED")
        elif self.kind == "EMAIL":
            if not is_valid_email_address(self.value):
                raise ValueError("EMAIL destination requires a single well-formed address")
        elif not is_strict_https_url(self.value):
            raise ValueError(f"{self.kind} destination requires a valid https:// URL value")

        if self.verification_status == "VERIFIED":
            if self.verified_by != "OPERATOR":
                raise ValueError("only an operator can mark a destination VERIFIED")
            if self.source_type not in _VERIFIABLE_SOURCES:
                raise ValueError("only employer or operator sources can verify a destination")
            if self.checked_at is None:
                raise ValueError("a VERIFIED destination requires checked_at")
            if self.source_type != "OPERATOR_ENTRY" and self.source_url is None:
                raise ValueError("a VERIFIED employer destination requires source_url")
        return self


def unknown_destination(job_id: int, reason: str, **fields) -> ApplicationDestination:
    return ApplicationDestination(job_id=job_id, kind="UNKNOWN", reason=reason, **fields)


def _value_ok(destination: ApplicationDestination) -> bool:
    if destination.kind == "EMAIL":
        return is_valid_email_address(destination.value)
    return is_strict_https_url(destination.value)


def _source_url_ok(destination: ApplicationDestination) -> bool:
    if destination.source_type == "OPERATOR_ENTRY" and destination.source_url is None:
        return True
    return is_strict_https_url(destination.source_url)


def _operator_verified(destination: ApplicationDestination | None) -> bool:
    """Every condition a human verification must leave behind, re-checked
    here rather than trusted from the validator -- including the value and
    source_url syntax."""
    return (
        destination is not None
        and destination.kind in PACKAGE_READY_KINDS
        and _value_ok(destination)
        and _source_url_ok(destination)
        and destination.verification_status == "VERIFIED"
        and destination.verified_by == "OPERATOR"
        and destination.source_type in _VERIFIABLE_SOURCES
        # isinstance, not `is not None`: model_construct can smuggle a str.
        and isinstance(destination.checked_at, datetime)
        and destination.vacancy_specific is True
        and destination.confidence in ("HIGH", "MEDIUM")
    )


def is_email_send_ready(destination: ApplicationDestination | None) -> bool:
    """The strict email rule: an address a future, separately authorized
    stage could address a draft to. Operator-verified against the
    employer's own job ad for this vacancy, HIGH confidence. A scraped or
    company-level address can never satisfy it."""
    return (
        _operator_verified(destination)
        and destination.kind == "EMAIL"
        and is_valid_email_address(destination.value)
        and destination.source_type in _EMAIL_SOURCES
        and destination.confidence == "HIGH"
    )


def is_channel_verified(destination: ApplicationDestination | None) -> bool:
    """A package-ready channel: a portal or email for THIS vacancy that a
    human operator verified -- what a Stage 14B package may target for
    HUMAN delivery (portal upload or a human-sent email). An EMAIL must
    additionally pass the strict email rule; there is no weaker email
    path."""
    if destination is not None and destination.kind == "EMAIL":
        return is_email_send_ready(destination)
    return _operator_verified(destination)
