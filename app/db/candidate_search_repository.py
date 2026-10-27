"""Stage 17B: read and change the persistent candidate search configuration.

Two steps, so a dry run shows exactly what `--apply` would write:

1. `plan_config_change` (pure, no writes) compares an operator-confirmed
   `CandidateConfigSpec` with what is stored and yields the field changes,
   the language/skill additions and every REFUSAL. A value is refused when
   the stored value carries a more authoritative source than the spec (an old
   CV never overwrites what the candidate confirmed), or when a language
   level would rise without a USER_CONFIRMED source.
2. `apply_config_plan` claims the profile version with the same atomic
   compare-and-swap as Stage 6A's PATCH (`profile_version` + 1, refused if
   stale), writes every change and one append-only
   `candidate_profile_revisions` row (actor, reason, before/after values) in
   ONE transaction.

Skills and languages are only ever added or updated, never removed; existing
skills are never modified. Already-sent applications are not touched.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.candidate_profile_repository import (
    CandidateProfileVersionConflictError,
    get_candidate_profile,
)
from app.db.models import (
    CandidateJobPreferencesRecord,
    CandidateLanguageRecord,
    CandidateProfileRecord,
    CandidateProfileRevisionRecord,
    CandidateSkillRecord,
)
from app.models.candidate_profile import normalize_text_identity
from app.models.candidate_search import (
    SOURCE_RANK,
    CandidateConfigSpec,
    CandidateContact,
    CandidateSearchSettings,
    GeoPoint,
    Provenance,
    StoredSearchConfig,
)

_LEVEL_ORDER = {"UNKNOWN": 0, "A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "NATIVE": 7}

# CandidateSearchSettings field -> CandidateJobPreferencesRecord column. The
# three Stage 6A columns (minimum_salary, salary_currency) are shared, not
# duplicated.
_SEARCH_COLUMNS: dict[str, str] = {
    "home_city": "home_city",
    "home_postal_code": "home_postal_code",
    "home_region": "home_region",
    "commute_origin_label": "commute_origin_label",
    "max_commute_km": "max_commute_km",
    "commute_measure": "commute_measure",
    "remote_work_scope": "remote_work_scope",
    "remote_work_from_region": "remote_work_from_region",
    "minimum_salary": "minimum_salary",
    "maximum_salary": "maximum_salary",
    "salary_currency": "salary_currency",
    "salary_negotiable": "salary_negotiable",
    "earliest_start_date": "earliest_start_date",
}
_SEARCH_LIST_COLUMNS: dict[str, str] = {
    "relocation_regions": "relocation_regions_json",
    "primary_roles": "primary_roles_json",
    "secondary_roles": "secondary_roles_json",
}
_CONTACT_COLUMNS = {
    "email": "contact_email",
    "phone": "contact_phone",
    "website_url": "contact_website_url",
}
_PROFILE_FIELDS = ("location_city", "location_country")


class CandidateConfigRefusedError(Exception):
    """The plan holds refusals; nothing was written."""


@dataclass(frozen=True)
class FieldChange:
    area: str  # profile | search | contact | language | skill | derived
    name: str
    old: Any
    new: Any


@dataclass
class ConfigPlan:
    spec: CandidateConfigSpec
    base_version: int
    changes: list[FieldChange] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.changes


def _loads(value: str | None, default):
    return json.loads(value) if value else default


def _trust_map(value: str | None) -> dict[str, Provenance]:
    return {name: Provenance(**entry) for name, entry in _loads(value, {}).items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, GeoPoint):
        return value.model_dump()
    return value


def stored_search_settings(record: CandidateJobPreferencesRecord | None) -> CandidateSearchSettings:
    if record is None:
        return CandidateSearchSettings()
    origin = None
    if record.commute_origin_lat is not None and record.commute_origin_lon is not None:
        origin = GeoPoint(lat=record.commute_origin_lat, lon=record.commute_origin_lon)
    values = {name: getattr(record, column) for name, column in _SEARCH_COLUMNS.items()}
    values.update(
        {name: _loads(getattr(record, column), []) for name, column in _SEARCH_LIST_COLUMNS.items()}
    )
    values["commute_origin"] = origin
    values["remote_work_scope"] = values["remote_work_scope"] or "UNKNOWN"
    return CandidateSearchSettings.model_construct(**values)


def load_search_config(db: Session) -> StoredSearchConfig | None:
    """Pure read (never creates the profile). None if no profile exists."""
    with db.no_autoflush:
        profile = get_candidate_profile(db)
        if profile is None:
            return None
        preferences = profile.job_preferences
        return StoredSearchConfig(
            profile_version=profile.profile_version,
            location_city=profile.location_city,
            location_country=profile.location_country,
            search=stored_search_settings(preferences),
            search_trust=_trust_map(preferences.search_trust_json if preferences else None),
            contact=CandidateContact.model_construct(
                email=profile.contact_email,
                phone=profile.contact_phone,
                website_url=profile.contact_website_url,
            ),
            contact_trust=_trust_map(profile.contact_trust_json),
        )


def _field_provenance(field_trust: dict, name: str) -> Provenance | None:
    entry = field_trust.get(name)
    return Provenance(source=entry["source"], confidence=entry["confidence"]) if entry else None


def _outranked(stored: Provenance | None, source: str) -> bool:
    return stored is not None and SOURCE_RANK[stored.source] > SOURCE_RANK[source]


def plan_config_change(profile: CandidateProfileRecord, spec: CandidateConfigSpec) -> ConfigPlan:
    plan = ConfigPlan(spec=spec, base_version=profile.profile_version)
    source = spec.source

    field_trust = _loads(profile.field_trust_json, {})
    for name in _PROFILE_FIELDS:
        new = getattr(spec.profile, name)
        if new is None or new == getattr(profile, name):
            continue
        stored = _field_provenance(field_trust, name)
        if _outranked(stored, source):
            plan.refusals.append(f"profile.{name}: stored source {stored.source} outranks {source}")
            continue
        plan.changes.append(FieldChange("profile", name, getattr(profile, name), new))

    if spec.contact is not None:
        trust = _trust_map(profile.contact_trust_json)
        for name, column in _CONTACT_COLUMNS.items():
            new = getattr(spec.contact, name)
            if new is None or new == getattr(profile, column):
                continue
            if _outranked(trust.get(name), source):
                plan.refusals.append(f"contact.{name}: stored source outranks {source}")
                continue
            plan.changes.append(FieldChange("contact", name, getattr(profile, column), new))

    preferences = profile.job_preferences
    if spec.search is not None:
        current = stored_search_settings(preferences)
        trust = _trust_map(preferences.search_trust_json if preferences else None)
        # Only what the file states explicitly; an omitted key is untouched.
        stated = spec.search.model_fields_set
        for name in [*_SEARCH_COLUMNS, *_SEARCH_LIST_COLUMNS, "commute_origin"]:
            new, old = getattr(spec.search, name), getattr(current, name)
            if name not in stated or new == old:
                continue
            field_source = spec.search_source(name)
            if _outranked(trust.get(name), field_source):
                plan.refusals.append(f"search.{name}: stored source outranks {field_source}")
                continue
            plan.changes.append(FieldChange("search", name, _jsonable(old), _jsonable(new)))
        # The role priorities stay in the search configuration. They are NOT
        # copied into `profile.target_roles`: that field drives the Stage
        # 11A/11B collector gates (a non-"Junior" or support role there makes
        # them fail open and lets senior/unrelated postings reach APPLY).

    languages = {lang.normalized_language: lang for lang in profile.languages}
    for spec_language in spec.languages:
        key = normalize_text_identity(spec_language.language)
        stored = languages.get(key)
        new = {**spec_language.model_dump(), "source": source}
        if stored is None:
            plan.changes.append(FieldChange("language", spec_language.language, None, new))
            continue
        old = {
            "language": stored.language,
            "level": stored.level,
            "certificate": stored.certificate,
            "notes": stored.notes,
            "source": stored.source,
        }
        if old == new:
            continue
        if SOURCE_RANK.get(stored.source, 0) > SOURCE_RANK[source]:
            plan.refusals.append(f"language {stored.language}: stored source outranks {source}")
            continue
        if _LEVEL_ORDER[new["level"]] > _LEVEL_ORDER[stored.level] and source != "USER_CONFIRMED":
            plan.refusals.append(
                f"language {stored.language}: level {stored.level} -> {new['level']} "
                "needs a USER_CONFIRMED source"
            )
            continue
        plan.changes.append(FieldChange("language", stored.language, old, new))

    skills = {skill.normalized_name for skill in profile.skills}
    for spec_skill in spec.skills:
        if normalize_text_identity(spec_skill.name) in skills:
            continue  # existing skills are never modified
        plan.changes.append(FieldChange("skill", spec_skill.name, None, spec_skill.model_dump()))
    return plan


def _apply_search(
    profile: CandidateProfileRecord, spec: CandidateConfigSpec, changed: set[str], trust: dict
) -> None:
    search = spec.search
    preferences = profile.job_preferences
    if preferences is None:
        preferences = CandidateJobPreferencesRecord()
        profile.job_preferences = preferences
    for name, column in _SEARCH_COLUMNS.items():
        if name in changed:
            setattr(preferences, column, getattr(search, name))
    for name, column in _SEARCH_LIST_COLUMNS.items():
        if name in changed:
            setattr(preferences, column, json.dumps(getattr(search, name)))
    if "commute_origin" in changed:
        origin = search.commute_origin
        preferences.commute_origin_lat = origin.lat if origin else None
        preferences.commute_origin_lon = origin.lon if origin else None
    stored = _loads(preferences.search_trust_json, {})
    stored.update({name: {**trust, "source": spec.search_source(name)} for name in changed})
    preferences.search_trust_json = json.dumps(stored)
    # Keep the Stage 6A preference columns consistent with the EFFECTIVE
    # configuration (stored values plus this change), never with a partial
    # file's defaults.
    effective = stored_search_settings(preferences)
    if "relocation_regions" in changed:
        preferences.relocation = bool(effective.relocation_regions)
    if {"home_city", "relocation_regions"} & changed:
        locations = [loc for loc in [effective.home_city, *effective.relocation_regions] if loc]
        preferences.preferred_locations_json = json.dumps(locations)
    if "remote_work_scope" in changed:
        preferences.remote_preference = (
            "FLEXIBLE" if effective.remote_work_scope == "GERMANY" else "UNKNOWN"
        )


def apply_config_plan(
    db: Session, plan: ConfigPlan, *, actor: str, reason: str, now: datetime | None = None
) -> int:
    """Write `plan` atomically; returns the new profile_version."""
    if plan.refusals:
        raise CandidateConfigRefusedError("; ".join(plan.refusals))
    if plan.is_noop:
        raise CandidateConfigRefusedError("nothing to change")
    if not actor.strip() or not reason.strip():
        raise CandidateConfigRefusedError("actor and reason are required")
    now = now or datetime.now(UTC)
    spec = plan.spec
    trust = {
        "source": spec.source,
        "confidence": "CONFIRMED",
        "confirmed_on": str(spec.confirmed_on),
    }

    result = db.execute(
        update(CandidateProfileRecord)
        .where(
            CandidateProfileRecord.id == 1,
            CandidateProfileRecord.profile_version == plan.base_version,
        )
        .values(profile_version=CandidateProfileRecord.profile_version + 1, updated_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        current = db.get(CandidateProfileRecord, 1)
        raise CandidateProfileVersionConflictError(
            current_version=current.profile_version if current is not None else None
        )
    try:
        profile = db.get(CandidateProfileRecord, 1)
        db.refresh(profile)
        by_area: dict[str, set[str]] = {}
        for change in plan.changes:
            by_area.setdefault(change.area, set()).add(change.name)

        field_trust = _loads(profile.field_trust_json, {})
        for name in by_area.get("profile", set()):
            setattr(profile, name, getattr(spec.profile, name))
            field_trust[name] = {"source": spec.source, "confidence": "CONFIRMED"}
        profile.field_trust_json = json.dumps(field_trust)

        if "contact" in by_area:
            contact_trust = _loads(profile.contact_trust_json, {})
            for name in by_area["contact"]:
                setattr(profile, _CONTACT_COLUMNS[name], getattr(spec.contact, name))
                contact_trust[name] = trust
            profile.contact_trust_json = json.dumps(contact_trust)

        if "search" in by_area:
            _apply_search(profile, spec, by_area["search"], trust)

        languages = {lang.normalized_language: lang for lang in profile.languages}
        changed_languages = {normalize_text_identity(n) for n in by_area.get("language", set())}
        for spec_language in spec.languages:
            key = normalize_text_identity(spec_language.language)
            if key not in changed_languages:
                continue
            record = languages.get(key)
            if record is None:
                record = CandidateLanguageRecord(
                    language=spec_language.language, normalized_language=key
                )
                profile.languages.append(record)
            record.level = spec_language.level
            record.certificate = spec_language.certificate
            record.notes = spec_language.notes
            record.source = spec.source
            record.confidence = "CONFIRMED"

        for spec_skill in spec.skills:
            if spec_skill.name in by_area.get("skill", set()):
                profile.skills.append(
                    CandidateSkillRecord(
                        name=spec_skill.name,
                        normalized_name=normalize_text_identity(spec_skill.name),
                        category=spec_skill.category,
                        notes=spec_skill.notes,
                        source=spec.source,
                        confidence="CONFIRMED",
                    )
                )

        new_version = plan.base_version + 1
        db.add(
            CandidateProfileRevisionRecord(
                candidate_profile_id=1,
                profile_version=new_version,
                recorded_at=now,
                actor=actor.strip(),
                reason=reason.strip(),
                source=spec.source,
                changed_fields_json=json.dumps(sorted(f"{c.area}.{c.name}" for c in plan.changes)),
                before_json=json.dumps(
                    {f"{c.area}.{c.name}": c.old for c in plan.changes}, default=str
                ),
                after_json=json.dumps(
                    {f"{c.area}.{c.name}": c.new for c in plan.changes}, default=str
                ),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return new_version
