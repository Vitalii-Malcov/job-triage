"""Candidate Profile DTOs: the structured, factual API shape for
GET/PATCH /api/v1/candidate-profile (Stage 6A).

**This is the single source of truth for candidate-side facts.** Future
CV/Bewerbung generation (Stage 6B+) must read candidate claims from here —
never invent skills, experience, education, certifications, languages,
projects, achievements, dates, or preferences that aren't present in this
model. See CLAUDE.md and the module docstring on
app/db/candidate_profile_repository.py for the full evidence-domain
separation (Candidate Profile / Job data / Company Research must never be
mixed implicitly).

**Provenance, not fake confidence.** Every nested fact carries `source`
(where it came from) and `confidence` (its current trust state) — see
SourceType/FactConfidence below and `is_usable_for_generation`. Stage 6A has
no LLM/document-ingestion pipeline, so every fact entered through this API
defaults to MANUAL_ENTRY/CONFIRMED (a human typing their own true facts
through an authenticated, single-user API) — INFERRED/UNCONFIRMED only ever
appear if a caller explicitly sets them (e.g. "I think I used this but I'm
not sure").
"""

import re
import unicodedata
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text_identity(value: str) -> str:
    """Canonical form for within-profile dedup identity (skill/language
    names) — NFKC-normalize, collapse internal whitespace, strip, casefold.
    Mirrors app.db.repositories.normalize_company_name exactly (same
    Unicode-equivalence rationale), duplicated here rather than imported
    from there since app/db/candidate_profile_repository.py imports FROM
    this module, not the other way around (repositories may depend on
    models; models must never depend on app/db).

    Shared by CandidateProfilePatchRequest's in-payload duplicate
    validators (below) and CandidateSkillRecord/CandidateLanguageRecord's
    DB-level UNIQUE(candidate_profile_id, normalized_name) constraint (see
    app/db/candidate_profile_repository.py) — using the same function in
    both places guarantees a payload that passes Pydantic validation can
    never still trip the DB constraint.
    """
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _WHITESPACE_RUN.sub(" ", normalized)
    return normalized.strip().casefold()


SkillCategory = Literal[
    "LANGUAGE",
    "FRAMEWORK",
    "DATABASE",
    "DEVOPS",
    "TESTING",
    "TOOL",
    "CLOUD",
    "AI",
    "OTHER",
]

SkillProficiency = Literal["BEGINNER", "BASIC", "INTERMEDIATE", "ADVANCED", "EXPERT", "UNKNOWN"]

CertificationStatus = Literal["COMPLETED", "IN_PROGRESS", "PLANNED", "UNKNOWN"]

LanguageLevel = Literal["A1", "A2", "B1", "B2", "C1", "C2", "NATIVE", "UNKNOWN"]

RemotePreference = Literal["ONSITE", "HYBRID", "REMOTE", "FLEXIBLE", "UNKNOWN"]

EmploymentType = Literal["FULL_TIME", "PART_TIME", "CONTRACT", "INTERNSHIP", "FREELANCE", "UNKNOWN"]

# Where a fact came from. Only USER_CONFIRMED / USER_PROVIDED_DOCUMENT /
# MANUAL_ENTRY are "the candidate (or a document they supplied) directly
# asserted this" — IMPORTED and INFERRED name a fact that arrived through
# some automated path (neither exists in Stage 6A, which has no ingestion
# pipeline; reserved for a future Stage 6A.2). UNKNOWN means provenance
# itself was never recorded.
SourceType = Literal[
    "USER_CONFIRMED",
    "USER_PROVIDED_DOCUMENT",
    "MANUAL_ENTRY",
    "IMPORTED",
    "INFERRED",
    "UNKNOWN",
]

# The fact's *current* trust state — independent of where it originally
# came from (e.g. an IMPORTED fact a human has since reviewed and confirmed
# would carry source=IMPORTED, confidence=CONFIRMED). See
# is_usable_for_generation for the single rule future agents must apply.
FactConfidence = Literal["CONFIRMED", "UNCONFIRMED", "INFERRED", "UNKNOWN"]

# CP-M-01 (Codex review): confidence=CONFIRMED alone is not enough — a fact
# whose *source* was never a direct human assertion (IMPORTED, INFERRED,
# UNKNOWN) must never be treated as generation-safe, no matter what its
# confidence field says. Only these three sources represent "the candidate,
# or a document they supplied, directly asserted this."
TRUSTED_GENERATION_SOURCES: frozenset[SourceType] = frozenset(
    {"USER_CONFIRMED", "USER_PROVIDED_DOCUMENT", "MANUAL_ENTRY"}
)


def is_usable_for_generation(source: SourceType, confidence: FactConfidence) -> bool:
    """The one rule Stage 6B+ (CV/Bewerbung generation) must apply before
    treating any candidate fact — nested (skill/experience/education/...)
    or top-level (see is_top_level_fact_usable_for_generation) — as usable:
    BOTH the source must be a trusted, directly-human-asserted one
    (TRUSTED_GENERATION_SOURCES) AND confidence must be CONFIRMED.

    Confidence alone is insufficient (CP-M-01): `source=INFERRED,
    confidence=CONFIRMED` or `source=UNKNOWN, confidence=CONFIRMED` must
    never pass — a fact nobody directly asserted is not made trustworthy by
    a confidence flag alone. This is a two-argument function on purpose;
    the previous one-argument version could accept exactly those unsafe
    combinations and has been removed rather than kept alongside this one,
    so no caller can accidentally use the weaker check.
    """
    return source in TRUSTED_GENERATION_SOURCES and confidence == "CONFIRMED"


# Stage 6A section 5 (CP-M-02): top-level candidate-profile fields that
# carry independent per-field provenance via CandidateProfile.field_trust /
# CandidateProfilePatchRequest.field_trust — see FieldTrust and
# is_top_level_fact_usable_for_generation below. `target_roles` is
# deliberately included: it stays modeled as profile self-description
# (Stage 6A section 4), not a job-search preference
# (CandidateJobPreferences has no per-field trust — job preferences are
# never treated as résumé facts to begin with, see that class's docstring),
# so if it remains top-level data it needs the same provenance guarantee as
# every other top-level field (section 5's explicit requirement).
TOP_LEVEL_TRUST_FIELDS: frozenset[str] = frozenset(
    {
        "first_name",
        "last_name",
        "professional_title",
        "location_city",
        "location_country",
        "professional_summary",
        "career_goal",
        "target_roles",
    }
)


class FieldTrust(BaseModel):
    """Provenance envelope for one top-level Candidate Profile field
    (CP-M-02) — see TOP_LEVEL_TRUST_FIELDS for which fields carry one.

    Defaults match a direct authenticated PATCH with no explicit override:
    the human is asserting this fact about themselves right now (Stage 6A
    section 6) — MANUAL_ENTRY/CONFIRMED. A caller may explicitly supply a
    different source/confidence (e.g. INFERRED/UNCONFIRMED) via
    CandidateProfilePatchRequest.field_trust; that value is persisted
    exactly as given, never silently upgraded to MANUAL_ENTRY/CONFIRMED.
    """

    source: SourceType = "MANUAL_ENTRY"
    confidence: FactConfidence = "CONFIRMED"


class CandidateSkill(BaseModel):
    """A single structured skill claim — never a free-text blob (section 5)."""

    id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    category: SkillCategory = "OTHER"
    # Never inferred from mere keyword appearance — UNKNOWN unless the
    # candidate explicitly states a proficiency level (section 5).
    proficiency: SkillProficiency = "UNKNOWN"
    years_experience: float | None = Field(default=None, ge=0, le=80)
    last_used_year: int | None = Field(default=None, ge=1950, le=2100)
    source: SourceType = "MANUAL_ENTRY"
    confidence: FactConfidence = "CONFIRMED"
    notes: str | None = None

    @model_validator(mode="after")
    def _normalize_name(self) -> "CandidateSkill":
        stripped = self.name.strip()
        if not stripped:
            raise ValueError("Skill name must not be blank.")
        self.name = stripped
        return self


class CandidateExperience(BaseModel):
    """A single work-experience entry. Responsibilities/achievements/
    technologies are only ever what's explicitly listed here — a
    technology mentioned elsewhere in the profile (e.g. in `skills`) is
    never automatically attached to an experience entry (section 6).
    """

    id: int | None = None
    company: str = Field(min_length=1, max_length=300)
    job_title: str = Field(min_length=1, max_length=300)
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool = False
    location: str | None = None
    description: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    source: SourceType = "MANUAL_ENTRY"
    confidence: FactConfidence = "CONFIRMED"

    @model_validator(mode="after")
    def _validate_dates(self) -> "CandidateExperience":
        if self.is_current and self.end_date is not None:
            raise ValueError("is_current experience must not have an end_date.")
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("end_date must not be before start_date.")
        return self


class CandidateEducation(BaseModel):
    """Supports incomplete education (section 7) — `completed` is never
    inferred from the presence/absence of end_date; both are independent,
    explicitly provided facts.
    """

    id: int | None = None
    institution: str = Field(min_length=1, max_length=300)
    program: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    completed: bool = False
    location: str | None = None
    notes: str | None = None
    source: SourceType = "MANUAL_ENTRY"
    confidence: FactConfidence = "CONFIRMED"

    @model_validator(mode="after")
    def _validate_dates(self) -> "CandidateEducation":
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("end_date must not be before start_date.")
        return self


def _validate_optional_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not (stripped.startswith("http://") or stripped.startswith("https://")):
        raise ValueError(f"{field_name} must be a http:// or https:// URL.")
    return stripped


class CandidateCertification(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=300)
    issuer: str | None = None
    issued_date: date | None = None
    expires_date: date | None = None
    credential_id: str | None = None
    credential_url: str | None = None
    # Never assumed COMPLETED merely because a name was entered — UNKNOWN
    # unless explicitly stated (section 8).
    status: CertificationStatus = "UNKNOWN"
    source: SourceType = "MANUAL_ENTRY"
    confidence: FactConfidence = "CONFIRMED"

    @model_validator(mode="after")
    def _validate(self) -> "CandidateCertification":
        if self.issued_date is not None and self.expires_date is not None:
            if self.expires_date < self.issued_date:
                raise ValueError("expires_date must not be before issued_date.")
        self.credential_url = _validate_optional_url(self.credential_url, "credential_url")
        return self


class CandidateProject(BaseModel):
    """Portfolio project claims. GitHub/other repositories are never
    inspected automatically to fabricate claims in Stage 6A (section 9) —
    every field here is candidate-approved information only.
    """

    id: int | None = None
    name: str = Field(min_length=1, max_length=300)
    description: str | None = None
    role: str | None = None
    technologies: list[str] = Field(default_factory=list)
    repository_url: str | None = None
    demo_url: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    highlights: list[str] = Field(default_factory=list)
    source: SourceType = "MANUAL_ENTRY"
    confidence: FactConfidence = "CONFIRMED"

    @model_validator(mode="after")
    def _validate(self) -> "CandidateProject":
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("end_date must not be before start_date.")
        self.repository_url = _validate_optional_url(self.repository_url, "repository_url")
        self.demo_url = _validate_optional_url(self.demo_url, "demo_url")
        return self


class CandidateLanguage(BaseModel):
    id: int | None = None
    language: str = Field(min_length=1, max_length=100)
    # Never upgraded automatically (section 10) — UNKNOWN unless the
    # candidate explicitly states a CEFR level or NATIVE.
    level: LanguageLevel = "UNKNOWN"
    certificate: str | None = None
    notes: str | None = None
    source: SourceType = "MANUAL_ENTRY"
    confidence: FactConfidence = "CONFIRMED"

    @model_validator(mode="after")
    def _normalize_language(self) -> "CandidateLanguage":
        stripped = self.language.strip()
        if not stripped:
            raise ValueError("Language must not be blank.")
        self.language = stripped
        return self


class CandidateJobPreferences(BaseModel):
    """Job-search preferences — deliberately NOT résumé facts (section 11).
    Kept in its own nested object (backed by its own table, see
    app/db/models.py's CandidateJobPreferencesRecord) so future consumers
    can distinguish "what the candidate has done" from "what the candidate
    is looking for" without inspecting field names.
    """

    preferred_locations: list[str] = Field(default_factory=list)
    remote_preference: RemotePreference = "UNKNOWN"
    employment_types: list[str] = Field(default_factory=list)
    minimum_salary: float | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=10)
    # Tri-state on purpose: None = never stated, distinct from an explicit
    # True/False (never invent an unstated preference — section 1).
    relocation: bool | None = None
    travel: bool | None = None


class CandidateProfile(BaseModel):
    """Full candidate profile — what GET /candidate-profile returns and
    what PATCH returns after applying an update.
    """

    id: int
    profile_version: int
    created_at: datetime
    updated_at: datetime

    first_name: str | None = None
    last_name: str | None = None
    professional_title: str | None = None
    location_city: str | None = None
    location_country: str | None = None

    professional_summary: str = ""
    career_goal: str = ""
    target_roles: list[str] = Field(default_factory=list)

    # CP-M-02: per-field provenance for the top-level fields above — keyed
    # by field name, only present once that field has actually been set via
    # a PATCH (see TOP_LEVEL_TRUST_FIELDS / apply_candidate_profile_patch).
    # A field absent from this dict has no recorded trust and is never
    # generation-usable (see is_top_level_fact_usable_for_generation) even
    # if its value happens to be non-empty.
    field_trust: dict[str, FieldTrust] = Field(default_factory=dict)

    skills: list[CandidateSkill] = Field(default_factory=list)
    experiences: list[CandidateExperience] = Field(default_factory=list)
    education: list[CandidateEducation] = Field(default_factory=list)
    certifications: list[CandidateCertification] = Field(default_factory=list)
    projects: list[CandidateProject] = Field(default_factory=list)
    languages: list[CandidateLanguage] = Field(default_factory=list)
    job_preferences: CandidateJobPreferences = Field(default_factory=CandidateJobPreferences)


def is_top_level_fact_usable_for_generation(profile: CandidateProfile, field_name: str) -> bool:
    """Stage 6B safety helper (CP-M-02): mechanically ask "is this
    top-level candidate fact (e.g. professional_title, professional_summary)
    safe to state as true in a generated document?" without guessing.

    Delegates to the exact same trusted-source + confirmed-state rule as
    is_usable_for_generation (no duplicate trust logic — CP-M-01's fix
    covers both nested and top-level facts through one function). A field
    with no recorded trust entry (never set via PATCH, so still at its
    blank/default value) is never usable — absence of provenance is not
    itself a trusted state.
    """
    trust = profile.field_trust.get(field_name)
    if trust is None:
        return False
    return is_usable_for_generation(trust.source, trust.confidence)


def _reject_duplicate_normalized(items: list, key, label: str) -> None:
    seen: set[str] = set()
    for item in items:
        normalized = key(item)
        if normalized in seen:
            raise ValueError(f"Duplicate {label} in payload: {normalized!r}.")
        seen.add(normalized)


class CandidateProfilePatchRequest(BaseModel):
    """PATCH /api/v1/candidate-profile body.

    **Partial-update semantics (section 16):** a key omitted from the
    payload (not present in the JSON body at all) is left completely
    untouched — this is what makes `{"professional_title": "..."}` safe to
    send without erasing skills/projects/education/languages. A key that
    *is* present in the payload is applied in full:
    - scalar fields (first_name, professional_title, ...) are simply
      overwritten;
    - list-of-structured-object fields (skills, experiences, education,
      certifications, projects, languages, target_roles) *replace* the
      corresponding list wholesale — this is still safe under the rule
      above (an omitted list field is untouched), and matches how PATCH
      commonly treats array fields in REST APIs generally (e.g. GitHub,
      Stripe): "if you send a list, that IS the new list."
    - `job_preferences`, if present, replaces the preferences object
      wholesale (it's a single nested object, not a list of facts, and has
      no per-item identity to merge against).

    No PUT endpoint is exposed (section 16's suggested resolution): a
    full-replace PUT over this many optional nested collections has no
    unambiguous way to distinguish "the client wants an empty list" from
    "the client didn't send this field" without additional protocol
    (e.g. always requiring every field) — PATCH's `exclude_unset` already
    gives an unambiguous, simpler answer to that question, so a second
    endpoint with different erasure semantics would only add a way to
    accidentally destroy data.

    **CP-M-03: `expected_profile_version` is required, and is concurrency
    metadata, not a candidate fact.** It is never persisted as profile
    content and never enters `field_trust` bookkeeping — see
    app/db/candidate_profile_repository.py's apply_candidate_profile_patch,
    which excludes it (and `field_trust` itself) from the set of "fields
    the caller is patching." Omitting it is a structural 422 (Pydantic
    requires it, no default); a value that no longer matches the current
    `profile_version` is a 409 (app/api/routes.py maps
    CandidateProfileVersionConflictError there) — the caller must GET the
    current profile and retry with the fresh version.

    **CP-M-02: `field_trust`, if present, only accepts entries for fields
    also being set in this same PATCH.** A `field_trust` entry for a field
    that isn't in this payload's own top-level keys is rejected (422) —
    trust metadata is never adjusted independently of the value it
    describes in Stage 6A. A top-level field set without a matching
    `field_trust` entry defaults to MANUAL_ENTRY/CONFIRMED (see
    FieldTrust); an explicit entry is persisted exactly as given, never
    silently upgraded.
    """

    expected_profile_version: int = Field(ge=1)

    first_name: str | None = None
    last_name: str | None = None
    professional_title: str | None = None
    location_city: str | None = None
    location_country: str | None = None

    professional_summary: str | None = None
    career_goal: str | None = None
    target_roles: list[str] | None = None

    field_trust: dict[str, FieldTrust] | None = None

    skills: list[CandidateSkill] | None = None
    experiences: list[CandidateExperience] | None = None
    education: list[CandidateEducation] | None = None
    certifications: list[CandidateCertification] | None = None
    projects: list[CandidateProject] | None = None
    languages: list[CandidateLanguage] | None = None
    job_preferences: CandidateJobPreferences | None = None

    @model_validator(mode="after")
    def _reject_duplicates(self) -> "CandidateProfilePatchRequest":
        if self.skills is not None:
            _reject_duplicate_normalized(
                self.skills, lambda s: normalize_text_identity(s.name), "skill"
            )
        if self.languages is not None:
            _reject_duplicate_normalized(
                self.languages, lambda lang: normalize_text_identity(lang.language), "language"
            )
        if self.projects is not None:
            _reject_duplicate_normalized(
                self.projects, lambda p: normalize_text_identity(p.name), "project"
            )
        return self

    @model_validator(mode="after")
    def _validate_field_trust(self) -> "CandidateProfilePatchRequest":
        if self.field_trust is None:
            return self
        provided_value_fields = self.model_fields_set - {"expected_profile_version", "field_trust"}
        for field_name in self.field_trust:
            if field_name not in TOP_LEVEL_TRUST_FIELDS:
                raise ValueError(
                    f"field_trust key {field_name!r} is not a recognized top-level "
                    f"candidate profile field."
                )
            if field_name not in provided_value_fields:
                raise ValueError(
                    f"field_trust entry for {field_name!r} was supplied, but {field_name!r} "
                    "itself was not included in this PATCH body."
                )
        return self
