"""Stage 14D: application contact data, kept apart from the Candidate Profile.

The Candidate Profile is the matching/evidence model; it deliberately has
no contact fields, and adding them would change profile semantics and the
transfer digest. Contact data needed to actually apply (application
e-mail, phone, postal address) lives here instead: a small, explicit
object that an operator confirms and keeps in a LOCAL, untracked JSON file
(see docs/STAGE_14D_LIVE_PILOT_READINESS.md). Nothing is ever derived,
guessed or defaulted: an absent file is `CONTACT_DATA_MISSING`.

PII: `__repr__`/`__str__` are masked and nothing here logs a value.
Problem codes carry field names only.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models.application_destination import is_valid_email_address
from app.models.candidate_profile import (
    CandidateProfile,
    is_top_level_fact_usable_for_generation,
)

CONTACT_DATA_MISSING = "CONTACT_DATA_MISSING"
CONTACT_EMAIL_MISSING = "CONTACT_EMAIL_MISSING"
CONTACT_PHONE_MISSING = "CONTACT_PHONE_MISSING"
CONTACT_NAME_MISMATCH = "CONTACT_NAME_MISMATCH"
CONTACT_PROFILE_NAME_UNTRUSTED = "CONTACT_PROFILE_NAME_UNTRUSTED"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Digits with the usual separators; 6..20 digits in total.
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()/.-]*[0-9]$")


class ApplicationIdentityInvalidError(ValueError):
    """The application identity file is unreadable or invalid. The message
    names the problem category only, never a value."""


class ApplicationIdentity(BaseModel):
    """Operator-confirmed contact data for applications."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    full_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    street: str | None = Field(default=None, max_length=200)
    postal_code: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    # Only a human statement makes this data usable.
    confirmed_by: Literal["OPERATOR"]
    confirmed_at: datetime

    @model_validator(mode="after")
    def _validate(self) -> "ApplicationIdentity":
        for name in ("full_name", "email", "phone", "street", "postal_code", "city", "country"):
            value = getattr(self, name)
            if value is None:
                continue
            if _CONTROL_RE.search(value):
                raise ValueError(f"{name} must not contain control characters")
            if not value.strip():
                raise ValueError(f"{name} must not be blank (omit it instead)")
        if self.email is not None and not is_valid_email_address(self.email):
            raise ValueError("email is not a single well-formed address")
        if self.phone is not None:
            digits = sum(ch.isdigit() for ch in self.phone)
            if not _PHONE_RE.match(self.phone) or not 6 <= digits <= 20:
                raise ValueError("phone is not a plausible telephone number")
        return self

    def __repr__(self) -> str:
        present = [name for name in ("email", "phone", "street", "city") if getattr(self, name)]
        return f"ApplicationIdentity(<redacted>, present={present})"

    __str__ = __repr__

    @property
    def postal_line(self) -> str | None:
        parts = " ".join(p for p in (self.postal_code, self.city) if p)
        return parts or None


def _normalized(name: str) -> str:
    return " ".join(name.split()).casefold()


def contact_data_problems(
    identity: ApplicationIdentity | None, profile: CandidateProfile
) -> tuple[str, ...]:
    """Every reason the contact data is not usable for this candidate's
    application; empty means complete. E-mail and phone are required; the
    identity's name must equal the profile's TRUSTED first + last name."""
    trusted = [
        (getattr(profile, field) or "").strip()
        if is_top_level_fact_usable_for_generation(profile, field)
        else ""
        for field in ("first_name", "last_name")
    ]
    return contact_problems_for_names(identity, trusted[0], trusted[1])


def contact_problems_for_names(
    identity: ApplicationIdentity | None, first_name: str | None, last_name: str | None
) -> tuple[str, ...]:
    """Same rules against an already-trusted first/last name (e.g. the CV
    draft header, which carries trusted facts only)."""
    if identity is None:
        return (CONTACT_DATA_MISSING,)
    problems = []
    if identity.email is None:
        problems.append(CONTACT_EMAIL_MISSING)
    if identity.phone is None:
        problems.append(CONTACT_PHONE_MISSING)
    names = [(first_name or "").strip(), (last_name or "").strip()]
    if not all(names):
        problems.append(CONTACT_PROFILE_NAME_UNTRUSTED)
    elif _normalized(" ".join(names)) != _normalized(identity.full_name):
        problems.append(CONTACT_NAME_MISMATCH)
    return tuple(problems)


def load_application_identity(path: Path | None) -> ApplicationIdentity | None:
    """None when no file is configured (-> CONTACT_DATA_MISSING downstream);
    a configured but unreadable/invalid file raises
    `ApplicationIdentityInvalidError` instead of being treated as absent."""
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ApplicationIdentityInvalidError("application identity file is unreadable") from None
    except ValueError:
        raise ApplicationIdentityInvalidError("application identity file is not JSON") from None
    try:
        return ApplicationIdentity.model_validate(raw)
    except ValidationError as exc:
        raise ApplicationIdentityInvalidError(
            f"application identity is invalid: {_error_summary(exc)}"
        ) from None


def _error_summary(exc: ValidationError) -> str:
    """Field paths for field errors; for this model's own cross-field
    checks, their fixed message (which names fields, never values)."""
    parts = set()
    for error in exc.errors():
        if error["loc"]:
            parts.add(".".join(str(p) for p in error["loc"]))
        else:
            parts.add(error["msg"].removeprefix("Value error, "))
    return ", ".join(sorted(parts))
