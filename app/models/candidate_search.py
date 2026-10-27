"""Stage 17B: the candidate's persistent search configuration and contacts.

These are job-search *preferences and constraints* plus contact details,
stored next to the Stage 6A profile (app/db/models.py:
CandidateJobPreferencesRecord / CandidateProfileRecord). They are read by
the job-fit assessment and the Bundesagentur search strategy so that no
search constraint lives only in an operator's (or an assistant's) memory.

`CandidateConfigSpec` is the operator-confirmed input file applied by
`scripts/stage17_candidate_config.py`. It deliberately lives OUTSIDE the
repository (the repository is public; contact data must not be committed).
"""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.candidate_profile import (
    FactConfidence,
    LanguageLevel,
    SkillCategory,
    SourceType,
)

CommuteMeasure = Literal["CAR_ROUTE"]
RemoteWorkScope = Literal["GERMANY", "UNKNOWN"]

# Higher = more authoritative. A stored value may only be replaced by a value
# whose source ranks at least as high (Stage 17B: an old CV never overwrites
# what the candidate confirmed directly).
SOURCE_RANK: dict[str, int] = {
    "USER_CONFIRMED": 5,
    "MANUAL_ENTRY": 4,
    "USER_PROVIDED_DOCUMENT": 3,
    "IMPORTED": 2,
    "INFERRED": 1,
    "UNKNOWN": 0,
}

_PHONE = re.compile(r"^\+[1-9][0-9 ]{6,24}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_POSTAL_CODE = re.compile(r"^[0-9]{5}$")


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceType
    confidence: FactConfidence
    confirmed_on: date | None = None


class GeoPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class CandidateContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    website_url: str | None = Field(default=None, max_length=500)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL.match(value):
            raise ValueError("email is not a plain address")
        return value

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        if value is not None and not _PHONE.match(value):
            raise ValueError("phone must be in international form, e.g. +49 176 ...")
        return value

    @field_validator("website_url")
    @classmethod
    def _website(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("website_url must be an https:// URL")
        return value


class CandidateSearchSettings(BaseModel):
    """The search constraints. `None`/empty = never stated (never guessed)."""

    model_config = ConfigDict(extra="forbid")

    home_city: str | None = Field(default=None, max_length=200)
    home_postal_code: str | None = None
    home_region: str | None = Field(default=None, max_length=100)
    commute_origin_label: str | None = Field(default=None, max_length=300)
    commute_origin: GeoPoint | None = None
    # <= 195 so the Bundesagentur search radius (<= 200 km, limit + 5 km
    # margin) still covers every workplace within the car-route limit.
    max_commute_km: int | None = Field(default=None, ge=1, le=195)
    commute_measure: CommuteMeasure | None = None
    relocation_regions: list[str] = Field(default_factory=list)
    remote_work_scope: RemoteWorkScope = "UNKNOWN"
    remote_work_from_region: str | None = Field(default=None, max_length=100)
    minimum_salary: float | None = Field(default=None, ge=0)
    maximum_salary: float | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=10)
    salary_negotiable: bool | None = None
    earliest_start_date: date | None = None
    primary_roles: list[str] = Field(default_factory=list)
    secondary_roles: list[str] = Field(default_factory=list)

    @field_validator("home_postal_code")
    @classmethod
    def _postal_code(cls, value: str | None) -> str | None:
        if value is not None and not _POSTAL_CODE.match(value):
            raise ValueError("home_postal_code must be a 5-digit German postal code")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "CandidateSearchSettings":
        if (
            self.minimum_salary is not None
            and self.maximum_salary is not None
            and self.maximum_salary < self.minimum_salary
        ):
            raise ValueError("maximum_salary is below minimum_salary")
        if self.max_commute_km is not None and self.commute_measure is None:
            raise ValueError("max_commute_km needs commute_measure (CAR_ROUTE)")
        if self.remote_work_scope != "UNKNOWN" and not self.remote_work_from_region:
            raise ValueError("remote_work_scope needs remote_work_from_region")
        return self

    @property
    def target_roles(self) -> list[str]:
        """Primary roles first, then secondary, without duplicates."""
        seen: set[str] = set()
        roles: list[str] = []
        for role in [*self.primary_roles, *self.secondary_roles]:
            key = role.casefold()
            if key not in seen:
                seen.add(key)
                roles.append(role)
        return roles


class StoredSearchConfig(BaseModel):
    """What the database currently holds, with per-field provenance."""

    profile_version: int
    location_city: str | None
    location_country: str | None
    search: CandidateSearchSettings
    search_trust: dict[str, Provenance] = Field(default_factory=dict)
    contact: CandidateContact
    contact_trust: dict[str, Provenance] = Field(default_factory=dict)


class SpecLanguage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=1, max_length=100)
    level: LanguageLevel
    certificate: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class SpecSkill(BaseModel):
    """A skill to ADD (existing skills are never modified or removed)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    category: SkillCategory = "OTHER"
    # e.g. "Learning/project experience only -- not commercial."
    notes: str | None = None


class SpecProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_city: str | None = Field(default=None, max_length=200)
    location_country: str | None = Field(default=None, max_length=200)


class CandidateConfigSpec(BaseModel):
    """The operator-confirmed configuration file (kept outside the repo)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["jobtriage.candidate-config/1"] = Field(alias="schema")
    source: SourceType
    confirmed_on: date
    profile: SpecProfile = Field(default_factory=SpecProfile)
    contact: CandidateContact | None = None
    search: CandidateSearchSettings | None = None
    languages: list[SpecLanguage] = Field(default_factory=list)
    skills: list[SpecSkill] = Field(default_factory=list)
    # Per-field source for a search value that did NOT come from the
    # candidate directly, e.g. {"commute_origin": "IMPORTED"} for station
    # coordinates looked up in OpenStreetMap. Stored as that field's
    # provenance instead of `source`; it can only lower the trust.
    search_sources: dict[str, SourceType] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _search_sources_name_search_fields(self) -> "CandidateConfigSpec":
        unknown = set(self.search_sources) - set(CandidateSearchSettings.model_fields)
        if unknown:
            raise ValueError(f"search_sources names unknown fields: {sorted(unknown)}")
        for name, field_source in self.search_sources.items():
            if SOURCE_RANK[field_source] > SOURCE_RANK[self.source]:
                raise ValueError(f"search_sources.{name} cannot outrank the file's source")
        return self

    def search_source(self, name: str) -> str:
        return self.search_sources.get(name, self.source)

    @field_validator("source")
    @classmethod
    def _trusted_source(cls, value: str) -> str:
        if value not in {"USER_CONFIRMED", "MANUAL_ENTRY"}:
            raise ValueError("a configuration file must be USER_CONFIRMED or MANUAL_ENTRY")
        return value
