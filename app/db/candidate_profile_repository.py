"""Candidate Profile persistence (Stage 6A).

**Evidence-domain separation.** This project's future CV/Bewerbung agent
(Stage 6B+) will combine three, and only three, factual authorities:

    Candidate Profile (this module) + Job data + Company Research
        -> CV/Bewerbung drafts

never an LLM inventing candidate-side facts. Candidate Profile is the sole
factual authority for candidate-side claims (skills, experience, education,
certifications, languages, projects, preferences) — Job data
(app/db/repositories.py's JobRecord) is the sole authority for vacancy-side
claims, and Company Research (app/db/repositories.py's CompanyResearchRecord)
is the sole authority for company-side claims. This module must never read
from or write into either of those tables, and nothing here should ever be
treated by a future consumer as vacancy- or company-side evidence.

**Singleton.** Exactly one CandidateProfileRecord may ever exist — see that
class's docstring in app/db/models.py for why `id=1` is enforced with a DB
CHECK constraint rather than application convention alone.

**Kept in its own module, not app/db/repositories.py.** That file is
already large from the Company Research Agent; Candidate Profile is a
self-contained domain with no cross-dependency on it (job/company-research
repository functions are never called from here, and vice versa) — a
separate module keeps each file smaller and more explicit (CLAUDE.md).
"""

import json
import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    CandidateCertificationRecord,
    CandidateEducationRecord,
    CandidateExperienceRecord,
    CandidateJobPreferencesRecord,
    CandidateLanguageRecord,
    CandidateProfileRecord,
    CandidateProjectRecord,
    CandidateSkillRecord,
)
from app.models.candidate_profile import (
    TOP_LEVEL_TRUST_FIELDS,
    CandidateCertification,
    CandidateEducation,
    CandidateExperience,
    CandidateJobPreferences,
    CandidateLanguage,
    CandidateProfile,
    CandidateProfilePatchRequest,
    CandidateProject,
    CandidateSkill,
    FieldTrust,
    normalize_text_identity,
)

logger = logging.getLogger(__name__)

_CANDIDATE_PROFILE_SINGLETON_ID = 1


class CandidateProfileConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — mirrors app.db.repositories.CompanyResearchConsistencyError
    (same rationale): e.g. the singleton row implied to exist by a CHECK/PK
    collision comes back None on reload. No code path in this project
    deletes CandidateProfileRecord, so this should be unreachable — raised
    instead of silently returning None and letting a caller crash later
    with an unrelated AttributeError.
    """


class CandidateProfileVersionConflictError(Exception):
    """Raised by apply_candidate_profile_patch (CP-M-03) when the caller's
    `expected_profile_version` no longer matches the current
    `profile_version` — either a concurrent PATCH already committed first,
    or the caller never read a fresh GET before patching.

    The atomic CAS in apply_candidate_profile_patch claims the version
    *before* any child-collection mutation runs, so this is always raised
    before anything is changed — never after a partial write. Carries only
    `current_version` (not personal data) so app/api/routes.py can surface
    it safely in a 409 response without leaking profile content; the
    caller's fix is always the same: GET the profile again and retry with
    the fresh version.
    """

    def __init__(self, current_version: int | None) -> None:
        self.current_version = current_version
        super().__init__(
            "Candidate profile was modified by another request. Reload the profile and retry."
        )


def _json_list(values: list[str]) -> str:
    return json.dumps(values)


def _parse_json_list(value: str) -> list[str]:
    return json.loads(value)


def _load_field_trust(value: str) -> dict[str, FieldTrust]:
    raw = json.loads(value)
    return {name: FieldTrust(**entry) for name, entry in raw.items()}


def _dump_field_trust(trust_map: dict[str, FieldTrust]) -> str:
    return json.dumps({name: trust.model_dump() for name, trust in trust_map.items()})


def get_or_create_candidate_profile(db: Session) -> CandidateProfileRecord:
    """Return the single canonical CandidateProfileRecord, creating an
    empty one (profile_version=1, every field blank/None/[]) on first
    access. Idempotent and race-safe: two concurrent first-callers both
    attempting to create id=1 will have one succeed and the other catch
    IntegrityError and reload the winner, rather than raising or leaving
    the database without a profile.

    GET /api/v1/candidate-profile always returns 200 via this — there is
    no "not yet initialized -> 404" state, since the singleton is
    guaranteed to exist (possibly empty) as soon as anything touches it.
    """
    profile = db.get(CandidateProfileRecord, _CANDIDATE_PROFILE_SINGLETON_ID)
    if profile is not None:
        return profile

    profile = CandidateProfileRecord(id=_CANDIDATE_PROFILE_SINGLETON_ID, profile_version=1)
    db.add(profile)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        profile = db.get(CandidateProfileRecord, _CANDIDATE_PROFILE_SINGLETON_ID)
        if profile is None:
            raise CandidateProfileConsistencyError(
                "Expected the singleton candidate_profiles row (id=1) to exist after a "
                "UNIQUE/CHECK constraint collision, but none was found."
            ) from None
        return profile

    db.refresh(profile)
    return profile


def get_candidate_profile(db: Session) -> CandidateProfileRecord | None:
    """Pure lookup of the singleton `CandidateProfileRecord` — `None` if
    it has never been created, with **no create-on-miss side effect**.

    Exists specifically for safety-sensitive freshness/authority checks
    (Stage 6E's review creation/approval flow) where a missing profile
    must fail closed rather than be silently (re)created merely to
    satisfy a version comparison — using `get_or_create_candidate_profile`
    there would let a deleted profile "pass" a version check against a
    freshly created empty replacement, which is not the historical
    authority that actually produced the reviewed content. Do not use this
    for `GET /api/v1/candidate-profile`, which deliberately always
    initializes the singleton — see `get_or_create_candidate_profile`.
    """
    return db.get(CandidateProfileRecord, _CANDIDATE_PROFILE_SINGLETON_ID)


def _skill_to_record(skill: CandidateSkill) -> CandidateSkillRecord:
    return CandidateSkillRecord(
        name=skill.name,
        normalized_name=normalize_text_identity(skill.name),
        category=skill.category,
        proficiency=skill.proficiency,
        years_experience=skill.years_experience,
        last_used_year=skill.last_used_year,
        source=skill.source,
        confidence=skill.confidence,
        notes=skill.notes,
    )


def _experience_to_record(experience: CandidateExperience) -> CandidateExperienceRecord:
    return CandidateExperienceRecord(
        company=experience.company,
        job_title=experience.job_title,
        start_date=experience.start_date,
        end_date=experience.end_date,
        is_current=experience.is_current,
        location=experience.location,
        description=experience.description,
        responsibilities_json=_json_list(experience.responsibilities),
        achievements_json=_json_list(experience.achievements),
        technologies_json=_json_list(experience.technologies),
        source=experience.source,
        confidence=experience.confidence,
    )


def _education_to_record(education: CandidateEducation) -> CandidateEducationRecord:
    return CandidateEducationRecord(
        institution=education.institution,
        program=education.program,
        degree=education.degree,
        field_of_study=education.field_of_study,
        start_date=education.start_date,
        end_date=education.end_date,
        completed=education.completed,
        location=education.location,
        notes=education.notes,
        source=education.source,
        confidence=education.confidence,
    )


def _certification_to_record(
    certification: CandidateCertification,
) -> CandidateCertificationRecord:
    return CandidateCertificationRecord(
        name=certification.name,
        issuer=certification.issuer,
        issued_date=certification.issued_date,
        expires_date=certification.expires_date,
        credential_id=certification.credential_id,
        credential_url=certification.credential_url,
        status=certification.status,
        source=certification.source,
        confidence=certification.confidence,
    )


def _project_to_record(project: CandidateProject) -> CandidateProjectRecord:
    return CandidateProjectRecord(
        name=project.name,
        description=project.description,
        role=project.role,
        technologies_json=_json_list(project.technologies),
        repository_url=project.repository_url,
        demo_url=project.demo_url,
        start_date=project.start_date,
        end_date=project.end_date,
        highlights_json=_json_list(project.highlights),
        source=project.source,
        confidence=project.confidence,
    )


def _language_to_record(language: CandidateLanguage) -> CandidateLanguageRecord:
    return CandidateLanguageRecord(
        language=language.language,
        normalized_language=normalize_text_identity(language.language),
        level=language.level,
        certificate=language.certificate,
        notes=language.notes,
        source=language.source,
        confidence=language.confidence,
    )


def _preferences_to_record(
    preferences: CandidateJobPreferences,
) -> CandidateJobPreferencesRecord:
    return CandidateJobPreferencesRecord(
        preferred_locations_json=_json_list(preferences.preferred_locations),
        remote_preference=preferences.remote_preference,
        employment_types_json=_json_list(preferences.employment_types),
        minimum_salary=preferences.minimum_salary,
        salary_currency=preferences.salary_currency,
        relocation=preferences.relocation,
        travel=preferences.travel,
    )


_SCALAR_FIELDS = (
    "first_name",
    "last_name",
    "professional_title",
    "location_city",
    "location_country",
    "professional_summary",
    "career_goal",
)


def apply_candidate_profile_patch(
    db: Session, patch: CandidateProfilePatchRequest
) -> CandidateProfileRecord:
    """Apply a partial update to the singleton Candidate Profile.

    Only keys actually present in the request body (patch.model_fields_set,
    minus the concurrency-metadata `expected_profile_version` and
    `field_trust` — see below) are touched — see
    CandidateProfilePatchRequest's docstring for the full partial-update
    contract this implements: an omitted key is left completely untouched;
    a present list-of-structured-object field (skills, experiences,
    education, certifications, projects, languages, target_roles)
    *replaces* that collection wholesale (relying on each relationship's
    `cascade="all, delete-orphan"` to delete the old rows); a present
    `job_preferences` replaces the preferences object wholesale.

    **CP-M-03: optimistic concurrency via an atomic compare-and-swap.**
    `patch.expected_profile_version` must match the row's current
    `profile_version` — enforced with a single `UPDATE ... WHERE id=1 AND
    profile_version=:expected` statement executed *before* any child-
    collection mutation, not a Python-level read-then-compare (which would
    leave a TOCTOU window between the check and the write). If the CAS
    matches zero rows, nothing has been touched yet — the caller's stale
    write is rejected via CandidateProfileVersionConflictError before any
    destructive DELETE/INSERT on skills/experiences/etc. runs. If it
    succeeds, every subsequent mutation in this call (scalar fields,
    field_trust, every replaced collection) happens inside the *same*,
    still-open transaction and is committed together in one `db.commit()`
    at the end — an exception anywhere in between rolls back the whole
    transaction, restoring both the version and every child row to exactly
    what they were before this call (see the explicit try/except below).

    **CP-M-02: top-level field provenance.** For every
    TOP_LEVEL_TRUST_FIELDS name present in this PATCH, its `field_trust`
    entry is set to the caller's explicit `patch.field_trust[name]` if
    given, else defaults to MANUAL_ENTRY/CONFIRMED (a direct authenticated
    PATCH is itself a human assertion). A field's trust entry is only ever
    touched when that field's *value* is also being set in this same call
    — see CandidateProfilePatchRequest's own validator, which already
    rejects a `field_trust` entry with no matching value field.

    profile_version is incremented exactly once per accepted, non-empty
    PATCH (Stage 6A section 14) — an empty-body PATCH (nothing beyond
    `expected_profile_version`) never mutates or bumps the version, but
    still validates the expected version against the current one (a stale
    expected_profile_version on an otherwise-empty PATCH is still a 409 —
    see CandidateProfileVersionConflictError, consistency over silently
    accepting it as a no-op). Stage 6A does not attempt no-op detection
    beyond that: a PATCH whose fields happen to already equal the current
    values still counts as "a key was present" and bumps the version once.

    **M-01: the empty-PATCH version check is DB-authoritative, not a
    Python-level comparison.** Comparing `patch.expected_profile_version`
    against the already-loaded `profile.profile_version` has a TOCTOU
    window: another request can commit a version bump between that load
    and this function returning, and the stale in-memory value would still
    read as "matched." Instead this runs the same kind of atomic,
    single-statement check as the CAS below — `UPDATE ... WHERE id=1 AND
    profile_version=:expected` — except its SET clause reassigns
    `profile_version` to itself, so a matched row is left byte-for-byte
    unchanged (no version bump, no `updated_at` touch, since that column
    has no `onupdate` and isn't included in this statement's SET clause
    either). `rowcount == 1` means the expected version was still current
    at the moment the database evaluated the WHERE clause, which is the
    only instant that matters — not the moment this function happened to
    load `profile` earlier.
    """
    profile = get_or_create_candidate_profile(db)
    expected_version = patch.expected_profile_version
    provided = patch.model_fields_set - {"expected_profile_version", "field_trust"}

    if not provided:
        noop_check_stmt = (
            update(CandidateProfileRecord)
            .where(
                CandidateProfileRecord.id == _CANDIDATE_PROFILE_SINGLETON_ID,
                CandidateProfileRecord.profile_version == expected_version,
            )
            .values(profile_version=CandidateProfileRecord.profile_version)
        )
        result = db.execute(noop_check_stmt)
        if result.rowcount == 0:
            db.rollback()
            db.expire_all()
            current = db.get(CandidateProfileRecord, _CANDIDATE_PROFILE_SINGLETON_ID)
            raise CandidateProfileVersionConflictError(
                current_version=current.profile_version if current is not None else None
            )
        db.commit()
        db.refresh(profile)
        return profile

    cas_stmt = (
        update(CandidateProfileRecord)
        .where(
            CandidateProfileRecord.id == _CANDIDATE_PROFILE_SINGLETON_ID,
            CandidateProfileRecord.profile_version == expected_version,
        )
        .values(profile_version=CandidateProfileRecord.profile_version + 1)
    )
    result = db.execute(cas_stmt)
    if result.rowcount == 0:
        db.rollback()
        db.expire_all()
        current = db.get(CandidateProfileRecord, _CANDIDATE_PROFILE_SINGLETON_ID)
        raise CandidateProfileVersionConflictError(
            current_version=current.profile_version if current is not None else None
        )
    # The Core UPDATE above bypasses the ORM's normal attribute sync — the
    # in-memory `profile` object's profile_version is now stale relative to
    # what was just written (uncommitted) in this same transaction. Correct
    # it directly rather than round-tripping with db.refresh(): the new
    # value is deterministic (expected_version + 1) since the CAS above
    # only succeeds when the row's version was exactly expected_version.
    profile.profile_version = expected_version + 1

    try:
        for field_name in _SCALAR_FIELDS:
            if field_name in provided:
                setattr(profile, field_name, getattr(patch, field_name))

        if "target_roles" in provided:
            profile.target_roles_json = _json_list(patch.target_roles)

        touched_trust_fields = provided & TOP_LEVEL_TRUST_FIELDS
        if touched_trust_fields:
            trust_map = _load_field_trust(profile.field_trust_json)
            overrides = patch.field_trust or {}
            for field_name in touched_trust_fields:
                trust_map[field_name] = overrides.get(field_name, FieldTrust())
            profile.field_trust_json = _dump_field_trust(trust_map)

        # Each replaced collection is cleared and flushed *before* the new
        # rows are assigned. Without the intermediate flush, SQLAlchemy's
        # unit-of-work can emit the new INSERTs before the old rows'
        # cascade-triggered DELETEs within the same flush, which trips
        # UNIQUE(candidate_profile_id, normalized_name)/(..., normalized_language)
        # whenever a replacement list re-uses a name/language that was
        # already present (e.g. keeping "Python" while dropping "SQL") —
        # the old and new rows would briefly coexist mid-flush otherwise.
        if "skills" in provided:
            profile.skills = []
            db.flush()
            profile.skills = [_skill_to_record(skill) for skill in patch.skills]
        if "experiences" in provided:
            profile.experiences = []
            db.flush()
            profile.experiences = [_experience_to_record(exp) for exp in patch.experiences]
        if "education" in provided:
            profile.education = []
            db.flush()
            profile.education = [_education_to_record(edu) for edu in patch.education]
        if "certifications" in provided:
            profile.certifications = []
            db.flush()
            profile.certifications = [
                _certification_to_record(cert) for cert in patch.certifications
            ]
        if "projects" in provided:
            profile.projects = []
            db.flush()
            profile.projects = [_project_to_record(project) for project in patch.projects]
        if "languages" in provided:
            profile.languages = []
            db.flush()
            profile.languages = [_language_to_record(language) for language in patch.languages]
        if "job_preferences" in provided:
            profile.job_preferences = None
            db.flush()
            profile.job_preferences = _preferences_to_record(patch.job_preferences)

        db.add(profile)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(profile)

    # Audit-safe (Stage 6A sections 9/21): log which *fields* changed, never
    # their values — candidate names, skills, employers etc. must never
    # reach application logs.
    logger.info(
        "candidate_profile_patched profile_version=%s fields=%s",
        profile.profile_version,
        ",".join(sorted(provided)),
    )
    return profile


def to_candidate_profile_response(record: CandidateProfileRecord) -> CandidateProfile:
    """Convert the persisted ORM graph into the typed API response shape.

    Never logs or otherwise exposes the record outside this explicit,
    structured conversion (Stage 6A section 21) — callers (routes.py) pass
    the result straight to FastAPI's response serialization.
    """
    return CandidateProfile(
        id=record.id,
        profile_version=record.profile_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        first_name=record.first_name,
        last_name=record.last_name,
        professional_title=record.professional_title,
        location_city=record.location_city,
        location_country=record.location_country,
        professional_summary=record.professional_summary,
        career_goal=record.career_goal,
        target_roles=_parse_json_list(record.target_roles_json),
        field_trust=_load_field_trust(record.field_trust_json),
        skills=[
            CandidateSkill(
                id=skill.id,
                name=skill.name,
                category=skill.category,
                proficiency=skill.proficiency,
                years_experience=skill.years_experience,
                last_used_year=skill.last_used_year,
                source=skill.source,
                confidence=skill.confidence,
                notes=skill.notes,
            )
            for skill in record.skills
        ],
        experiences=[
            CandidateExperience(
                id=exp.id,
                company=exp.company,
                job_title=exp.job_title,
                start_date=exp.start_date,
                end_date=exp.end_date,
                is_current=exp.is_current,
                location=exp.location,
                description=exp.description,
                responsibilities=_parse_json_list(exp.responsibilities_json),
                achievements=_parse_json_list(exp.achievements_json),
                technologies=_parse_json_list(exp.technologies_json),
                source=exp.source,
                confidence=exp.confidence,
            )
            for exp in record.experiences
        ],
        education=[
            CandidateEducation(
                id=edu.id,
                institution=edu.institution,
                program=edu.program,
                degree=edu.degree,
                field_of_study=edu.field_of_study,
                start_date=edu.start_date,
                end_date=edu.end_date,
                completed=edu.completed,
                location=edu.location,
                notes=edu.notes,
                source=edu.source,
                confidence=edu.confidence,
            )
            for edu in record.education
        ],
        certifications=[
            CandidateCertification(
                id=cert.id,
                name=cert.name,
                issuer=cert.issuer,
                issued_date=cert.issued_date,
                expires_date=cert.expires_date,
                credential_id=cert.credential_id,
                credential_url=cert.credential_url,
                status=cert.status,
                source=cert.source,
                confidence=cert.confidence,
            )
            for cert in record.certifications
        ],
        projects=[
            CandidateProject(
                id=project.id,
                name=project.name,
                description=project.description,
                role=project.role,
                technologies=_parse_json_list(project.technologies_json),
                repository_url=project.repository_url,
                demo_url=project.demo_url,
                start_date=project.start_date,
                end_date=project.end_date,
                highlights=_parse_json_list(project.highlights_json),
                source=project.source,
                confidence=project.confidence,
            )
            for project in record.projects
        ],
        languages=[
            CandidateLanguage(
                id=language.id,
                language=language.language,
                level=language.level,
                certificate=language.certificate,
                notes=language.notes,
                source=language.source,
                confidence=language.confidence,
            )
            for language in record.languages
        ],
        job_preferences=_preferences_from_record(record.job_preferences),
    )


def _preferences_from_record(
    record: CandidateJobPreferencesRecord | None,
) -> CandidateJobPreferences:
    if record is None:
        return CandidateJobPreferences()
    return CandidateJobPreferences(
        preferred_locations=_parse_json_list(record.preferred_locations_json),
        remote_preference=record.remote_preference,
        employment_types=_parse_json_list(record.employment_types_json),
        minimum_salary=record.minimum_salary,
        salary_currency=record.salary_currency,
        relocation=record.relocation,
        travel=record.travel,
    )


def count_candidate_profiles(db: Session) -> int:
    """Test/diagnostic helper: how many candidate_profiles rows exist.
    Should only ever be 0 or 1 (the DB CHECK constraint forbids more).
    """
    return len(db.scalars(select(CandidateProfileRecord)).all())
