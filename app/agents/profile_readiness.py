"""Candidate Profile readiness for application documents (Stage 14A).

**Purpose.** Answers, per fact area, "can a generated CV/Bewerbung state
this about the candidate?" without guessing. Pure function over an
already-loaded `CandidateProfile`; no I/O, no LLM, no network.

**Four honest states per area (Stage 14A spec section 1):**

- `VERIFIED` -- at least one structured fact that passes the exact Stage 6A
  trust rule (`is_usable_for_generation` / `is_top_level_fact_usable_for_
  generation`, imported, never reimplemented). Only these may be claimed.
- `UNSAFE_TO_CLAIM` -- structured rows exist but none is generation-usable
  (untrusted source or unconfirmed). They are excluded from generation
  exactly as if absent; the operator must confirm or delete them.
- `FREE_TEXT_ONLY` -- no usable structured fact, but the free-text
  `professional_summary` appears to mention the area. A keyword hint only:
  the summary is copied verbatim into the CV, but nothing in it is ever
  turned into a structured claim (no employer, dates, degree or level can
  be read reliably out of prose). The operator must enter the fact.
- `MISSING` -- nothing at all.

Per-item caveats that do not change the area state (an incomplete degree,
a language without a CEFR level, a certification that is not COMPLETED) are
reported as `notes` so a reviewer can see what a generator is NOT allowed
to say even though a trusted row exists.

This report is VACANCY-INDEPENDENT and advisory: `SEND_PACKAGE_REQUIRED_AREAS`
/ `send_package_gaps` are a generic "what is the profile still missing"
diagnostic for the operator. The authoritative Stage 14A send-readiness
gate (`app.services.send_readiness`) does NOT accept this report as proof:
it requires vacancy-relevant evidence from the pinned match, an exact
trusted-title match, and requirement-aware education/language checks.
"""

from dataclasses import dataclass, field
from typing import Literal

from app.models.candidate_profile import (
    CandidateProfile,
    is_top_level_fact_usable_for_generation,
    is_usable_for_generation,
)

AreaStatus = Literal["VERIFIED", "UNSAFE_TO_CLAIM", "FREE_TEXT_ONLY", "MISSING"]

AREA_NAME = "NAME"
AREA_TITLE = "PROFESSIONAL_TITLE"
AREA_SUMMARY = "SUMMARY"
AREA_SKILLS = "SKILLS"
AREA_EXPERIENCE = "EXPERIENCE"
AREA_PROJECTS = "PROJECTS"
AREA_EDUCATION = "EDUCATION"
AREA_LANGUAGES = "LANGUAGES"
AREA_CERTIFICATIONS = "CERTIFICATIONS"

AREA_ORDER = (
    AREA_NAME,
    AREA_TITLE,
    AREA_SUMMARY,
    AREA_SKILLS,
    AREA_EXPERIENCE,
    AREA_PROJECTS,
    AREA_EDUCATION,
    AREA_LANGUAGES,
    AREA_CERTIFICATIONS,
)

# Generic diagnostic only (see module docstring). EXPERIENCE
# and PROJECTS are deliberately absent here: the gate requires "at least one
# of them" (see has_relevant_structured_evidence), not both.
SEND_PACKAGE_REQUIRED_AREAS = frozenset(
    {AREA_NAME, AREA_TITLE, AREA_SKILLS, AREA_EDUCATION, AREA_LANGUAGES}
)

# Lower-case substrings that make a summary "appear to mention" an area.
# Deliberately coarse: a hit only ever downgrades MISSING to FREE_TEXT_ONLY
# (an operator prompt), it never creates a claim.
_FREE_TEXT_HINTS: dict[str, tuple[str, ...]] = {
    AREA_TITLE: ("developer", "entwickler", "engineer", "ingenieur"),
    AREA_EXPERIENCE: (
        "experience",
        "erfahrung",
        "worked",
        "work with",
        "freelance",
        "tätigkeit",
        "employed",
        "beschäftigt",
    ),
    AREA_PROJECTS: ("project", "projekt"),
    AREA_EDUCATION: (
        "educat",
        "degree",
        "studium",
        "studied",
        "ausbildung",
        "weiterbildung",
        "training",
        "bachelor",
        "master",
        "diplom",
        "universit",
        "hochschule",
    ),
    AREA_LANGUAGES: (
        "german",
        "deutsch",
        "english",
        "englisch",
        "russian",
        "russisch",
        "native",
        "muttersprach",
    ),
    AREA_CERTIFICATIONS: ("certif", "zertifi"),
}

NOTE_TARGET_ROLES_NOT_A_TITLE = "TARGET_ROLES_PRESENT_BUT_NOT_A_PROFESSIONAL_TITLE"
NOTE_EDUCATION_NOT_COMPLETED = "EDUCATION_NOT_COMPLETED_DEGREE_MUST_NOT_BE_CLAIMED"
NOTE_LANGUAGE_LEVEL_UNKNOWN = "LANGUAGE_LEVEL_UNKNOWN_NOT_RENDERED_IN_LETTER"
NOTE_CERTIFICATION_NOT_COMPLETED = "CERTIFICATION_NOT_COMPLETED_MUST_NOT_BE_CLAIMED"
NOTE_SUMMARY_NOT_STRUCTURED = "SUMMARY_IS_FREE_TEXT_AND_IS_NEVER_PARSED_INTO_CLAIMS"


@dataclass(frozen=True)
class AreaReadiness:
    area: str
    status: AreaStatus
    verified_count: int = 0
    untrusted_count: int = 0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileReadinessReport:
    profile_version: int
    areas: tuple[AreaReadiness, ...] = field(default_factory=tuple)

    def area(self, name: str) -> AreaReadiness:
        for item in self.areas:
            if item.area == name:
                return item
        raise KeyError(name)

    def status(self, name: str) -> AreaStatus:
        return self.area(name).status

    @property
    def verified_areas(self) -> list[str]:
        return [a.area for a in self.areas if a.status == "VERIFIED"]

    @property
    def missing_areas(self) -> list[str]:
        return [a.area for a in self.areas if a.status == "MISSING"]

    @property
    def free_text_only_areas(self) -> list[str]:
        return [a.area for a in self.areas if a.status == "FREE_TEXT_ONLY"]

    @property
    def unsafe_areas(self) -> list[str]:
        return [a.area for a in self.areas if a.status == "UNSAFE_TO_CLAIM"]

    @property
    def has_relevant_structured_evidence(self) -> bool:
        """At least one trusted experience OR project entry -- generic, NOT
        vacancy-relevant; the send gate uses the pinned match instead."""
        return "VERIFIED" in (self.status(AREA_EXPERIENCE), self.status(AREA_PROJECTS))

    @property
    def send_package_gaps(self) -> list[str]:
        gaps = [
            area
            for area in AREA_ORDER
            if area in SEND_PACKAGE_REQUIRED_AREAS and self.status(area) != "VERIFIED"
        ]
        if not self.has_relevant_structured_evidence:
            gaps.append("EXPERIENCE_OR_PROJECT")
        return gaps


def _summary_mentions(profile: CandidateProfile, area: str) -> bool:
    hints = _FREE_TEXT_HINTS.get(area)
    if not hints:
        return False
    text = (profile.professional_summary or "").casefold()
    return any(hint in text for hint in hints)


def _without_structured(profile: CandidateProfile, area: str) -> AreaStatus:
    return "FREE_TEXT_ONLY" if _summary_mentions(profile, area) else "MISSING"


def _top_level_present(profile: CandidateProfile, field_name: str) -> bool:
    value = getattr(profile, field_name)
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _top_level_usable(profile: CandidateProfile, field_name: str) -> bool:
    return _top_level_present(profile, field_name) and is_top_level_fact_usable_for_generation(
        profile, field_name
    )


def _list_area(
    profile: CandidateProfile, area: str, items: list, notes: list[str]
) -> AreaReadiness:
    trusted = [i for i in items if is_usable_for_generation(i.source, i.confidence)]
    untrusted = len(items) - len(trusted)
    if trusted:
        status: AreaStatus = "VERIFIED"
    elif untrusted:
        status = "UNSAFE_TO_CLAIM"
    else:
        status = _without_structured(profile, area)
    return AreaReadiness(
        area=area,
        status=status,
        verified_count=len(trusted),
        untrusted_count=untrusted,
        notes=tuple(notes),
    )


def _name_area(profile: CandidateProfile) -> AreaReadiness:
    fields = ("first_name", "last_name")
    usable = [f for f in fields if _top_level_usable(profile, f)]
    present = [f for f in fields if _top_level_present(profile, f)]
    if len(usable) == len(fields):
        return AreaReadiness(AREA_NAME, "VERIFIED", verified_count=len(usable))
    if present:
        return AreaReadiness(
            AREA_NAME, "UNSAFE_TO_CLAIM", len(usable), untrusted_count=len(present) - len(usable)
        )
    return AreaReadiness(AREA_NAME, "MISSING")


def _scalar_area(profile: CandidateProfile, area: str, field_name: str) -> AreaReadiness:
    notes: list[str] = []
    if area == AREA_TITLE and profile.target_roles:
        notes.append(NOTE_TARGET_ROLES_NOT_A_TITLE)
    if area == AREA_SUMMARY:
        notes.append(NOTE_SUMMARY_NOT_STRUCTURED)
    if _top_level_usable(profile, field_name):
        return AreaReadiness(area, "VERIFIED", verified_count=1, notes=tuple(notes))
    if _top_level_present(profile, field_name):
        return AreaReadiness(area, "UNSAFE_TO_CLAIM", untrusted_count=1, notes=tuple(notes))
    status: AreaStatus = "MISSING" if area == AREA_SUMMARY else _without_structured(profile, area)
    return AreaReadiness(area, status, notes=tuple(notes))


def evaluate_profile_readiness(profile: CandidateProfile) -> ProfileReadinessReport:
    areas: list[AreaReadiness] = [
        _name_area(profile),
        _scalar_area(profile, AREA_TITLE, "professional_title"),
        _scalar_area(profile, AREA_SUMMARY, "professional_summary"),
    ]

    skills = _list_area(profile, AREA_SKILLS, profile.skills, [])
    experience = _list_area(profile, AREA_EXPERIENCE, profile.experiences, [])
    projects = _list_area(profile, AREA_PROJECTS, profile.projects, [])

    trusted_education = [
        e for e in profile.education if is_usable_for_generation(e.source, e.confidence)
    ]
    education_notes = (
        [NOTE_EDUCATION_NOT_COMPLETED] if any(not e.completed for e in trusted_education) else []
    )
    education = _list_area(profile, AREA_EDUCATION, profile.education, education_notes)

    trusted_languages = [
        lang for lang in profile.languages if is_usable_for_generation(lang.source, lang.confidence)
    ]
    language_notes = (
        [NOTE_LANGUAGE_LEVEL_UNKNOWN]
        if any(lang.level == "UNKNOWN" for lang in trusted_languages)
        else []
    )
    languages = _list_area(profile, AREA_LANGUAGES, profile.languages, language_notes)

    trusted_certs = [
        c for c in profile.certifications if is_usable_for_generation(c.source, c.confidence)
    ]
    cert_notes = (
        [NOTE_CERTIFICATION_NOT_COMPLETED]
        if any(c.status != "COMPLETED" for c in trusted_certs)
        else []
    )
    certifications = _list_area(profile, AREA_CERTIFICATIONS, profile.certifications, cert_notes)

    areas.extend([skills, experience, projects, education, languages, certifications])
    return ProfileReadinessReport(profile_version=profile.profile_version, areas=tuple(areas))


def render_profile_readiness(report: ProfileReadinessReport) -> str:
    """Plain-text operator view. Contains area names, states, counts and
    fixed note codes only -- never profile content."""
    lines = [f"PROFILE READINESS (profile_version={report.profile_version})"]
    for item in report.areas:
        line = (
            f"- {item.area}: {item.status} (verified={item.verified_count}, "
            f"untrusted={item.untrusted_count})"
        )
        lines.append(line)
        lines.extend(f"    note: {note}" for note in item.notes)
    lines.append("verified structured: " + (", ".join(report.verified_areas) or "none"))
    lines.append("missing from profile: " + (", ".join(report.missing_areas) or "none"))
    lines.append("free-text summary only: " + (", ".join(report.free_text_only_areas) or "none"))
    lines.append("unsafe to claim: " + (", ".join(report.unsafe_areas) or "none"))
    lines.append("send-package gaps: " + (", ".join(report.send_package_gaps) or "none"))
    return "\n".join(lines)
