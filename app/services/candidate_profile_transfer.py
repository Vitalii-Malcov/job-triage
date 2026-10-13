"""Stage 13B: provenance-preserving CandidateProfile transfer snapshot.

Moves the ONE canonical CandidateProfile (facts + provenance) from the
historical SQLite pilot into a FRESH PostgreSQL deployment. This is not a
database migration tool: no other table is ever read for transfer or
written by it. Jobs, Gmail state, approvals, sends, drafts, scheduler and
collector progress all stay in the archived SQLite file; the PostgreSQL
pilot starts with a clean operational state (see docs/STAGE_13B_REHEARSAL.md).

**Why not GET -> PATCH.** `CandidateProfilePatchRequest` defaults a
top-level field written without an explicit `field_trust` entry to
MANUAL_ENTRY/CONFIRMED, and `FieldTrust`/the nested models default an absent
`source`/`confidence` the same way. Replaying a profile through the API
would therefore silently UPGRADE a fact whose trust entry is absent (or
INFERRED/UNCONFIRMED data whose metadata got lost) into a generation-usable
one. This module never relies on any of those defaults:

- the snapshot models below have NO defaults -- every attribute, including
  every `source`/`confidence`, must be present explicitly;
- `field_trust` is read straight from `candidate_profiles.field_trust_json`
  and parsed strictly (the repository's own `_load_field_trust` would fill
  a missing `confidence` in with CONFIRMED) -- an absent entry stays absent;
- import writes ORM rows directly, inside one transaction, with exactly the
  snapshot's values.

**Semantic content vs persistence identity.** Child row ids, created_at /
updated_at and profile_version are database-local metadata, not candidate
facts: they are excluded from the semantic payload. Import creates the
singleton with `profile_version = 1` (FRESH_PROFILE_VERSION) -- historical
review/match/approval state that referenced older versions is deliberately
not migrated, so no older version number has anything to stay consistent
with.

**Determinism.** The ORM relationships carry no `order_by`, so the
application gives no ordering guarantee for nested collections; the payload
sorts every collection by each item's canonical JSON. Lists INSIDE an item
(responsibilities, technologies, ...) and `target_roles` keep their stored
order -- they are persisted as ordered JSON arrays. The semantic digest is
SHA-256 over canonical JSON (sorted keys, compact separators, UTF-8, no NaN)
of {format, format_version, profile} -- never exported_at or any path.

**PII.** A snapshot is the candidate's personal data. Nothing here logs or
raises with a candidate value: errors carry a category plus field
paths/types only, and pydantic errors are re-raised sanitized.
"""

import hashlib
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import String, func, select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.candidate_profile_repository import (
    _certification_to_record,
    _education_to_record,
    _experience_to_record,
    _language_to_record,
    _preferences_to_record,
    _project_to_record,
    _skill_to_record,
    get_candidate_profile,
    to_candidate_profile_response,
)
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
    CandidateProject,
    CandidateSkill,
    CertificationStatus,
    FactConfidence,
    LanguageLevel,
    RemotePreference,
    SkillCategory,
    SkillProficiency,
    SourceType,
    is_top_level_fact_usable_for_generation,
    is_usable_for_generation,
    normalize_text_identity,
)

logger = logging.getLogger(__name__)

FORMAT_ID = "jobtriage.candidate_profile_transfer"
FORMAT_VERSION = 1
FRESH_PROFILE_VERSION = 1

# The CandidateProfile factual domain -- the ONLY tables transfer reads for
# content or writes. Everything else in Base.metadata is operational/historical
# state that must be empty on the destination (see historical_state_counts).
PROFILE_DOMAIN_TABLES: tuple[str, ...] = (
    CandidateProfileRecord.__tablename__,
    CandidateSkillRecord.__tablename__,
    CandidateExperienceRecord.__tablename__,
    CandidateEducationRecord.__tablename__,
    CandidateCertificationRecord.__tablename__,
    CandidateProjectRecord.__tablename__,
    CandidateLanguageRecord.__tablename__,
    CandidateJobPreferencesRecord.__tablename__,
)
ALEMBIC_VERSION_TABLE = "alembic_version"

COLLECTIONS: tuple[str, ...] = (
    "skills",
    "experiences",
    "education",
    "certifications",
    "projects",
    "languages",
)
SCALAR_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "professional_title",
    "location_city",
    "location_country",
    "professional_summary",
    "career_goal",
)
# Persistence identity / concurrency metadata -- never semantic content.
NON_SEMANTIC_PROFILE_FIELDS = frozenset({"id", "profile_version", "created_at", "updated_at"})
NON_SEMANTIC_ITEM_FIELDS = frozenset({"id"})


# --- errors (category only, never a candidate value) ------------------------------


class TransferError(Exception):
    """Base class. `category` is a stable machine-readable reason; the message
    never contains a candidate value."""

    category = "transfer_error"


class SourceProfileMissingError(TransferError):
    category = "source_profile_missing"


class SourceProfileInvalidError(TransferError):
    category = "source_profile_invalid"


class SnapshotInvalidError(TransferError):
    category = "snapshot_invalid"


class SnapshotDigestMismatchError(TransferError):
    category = "snapshot_digest_mismatch"


class SchemaRevisionMismatchError(TransferError):
    category = "schema_revision_mismatch"


class DestinationNotEmptyError(TransferError):
    category = "destination_not_empty"


class ImportVerificationError(TransferError):
    category = "import_verification_failed"


# --- strict snapshot models: no defaults, no coercion, no extras -------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TransferFieldTrust(_Strict):
    source: SourceType
    confidence: FactConfidence


class TransferSkill(_Strict):
    name: str
    category: SkillCategory
    proficiency: SkillProficiency
    years_experience: float | None
    last_used_year: int | None
    source: SourceType
    confidence: FactConfidence
    notes: str | None


class TransferExperience(_Strict):
    company: str
    job_title: str
    start_date: str | None
    end_date: str | None
    is_current: bool
    location: str | None
    description: str | None
    responsibilities: list[str]
    achievements: list[str]
    technologies: list[str]
    source: SourceType
    confidence: FactConfidence


class TransferEducation(_Strict):
    institution: str
    program: str | None
    degree: str | None
    field_of_study: str | None
    start_date: str | None
    end_date: str | None
    completed: bool
    location: str | None
    notes: str | None
    source: SourceType
    confidence: FactConfidence


class TransferCertification(_Strict):
    name: str
    issuer: str | None
    issued_date: str | None
    expires_date: str | None
    credential_id: str | None
    credential_url: str | None
    status: CertificationStatus
    source: SourceType
    confidence: FactConfidence


class TransferProject(_Strict):
    name: str
    description: str | None
    role: str | None
    technologies: list[str]
    repository_url: str | None
    demo_url: str | None
    start_date: str | None
    end_date: str | None
    highlights: list[str]
    source: SourceType
    confidence: FactConfidence


class TransferLanguage(_Strict):
    language: str
    level: LanguageLevel
    certificate: str | None
    notes: str | None
    source: SourceType
    confidence: FactConfidence


class TransferJobPreferences(_Strict):
    preferred_locations: list[str]
    remote_preference: RemotePreference
    employment_types: list[str]
    minimum_salary: float | None
    salary_currency: str | None
    relocation: bool | None
    travel: bool | None


class TransferProfile(_Strict):
    """Every transferable fact and its provenance. `field_trust` holds exactly
    the entries the source had -- a key's ABSENCE is meaningful (not usable for
    generation) and is preserved. `job_preferences` is None when the source
    never stored a preferences row."""

    first_name: str | None
    last_name: str | None
    professional_title: str | None
    location_city: str | None
    location_country: str | None
    professional_summary: str
    career_goal: str
    target_roles: list[str]
    field_trust: dict[str, TransferFieldTrust]
    skills: list[TransferSkill]
    experiences: list[TransferExperience]
    education: list[TransferEducation]
    certifications: list[TransferCertification]
    projects: list[TransferProject]
    languages: list[TransferLanguage]
    job_preferences: TransferJobPreferences | None


# (snapshot model, domain model, ORM record builder) per collection. The domain
# model re-applies the application's own validators (date order, URL shape,
# name strip); the builder is the repository's own row mapping.
_COLLECTION_SPECS: dict[str, tuple[type[_Strict], type[BaseModel], Any]] = {
    "skills": (TransferSkill, CandidateSkill, _skill_to_record),
    "experiences": (TransferExperience, CandidateExperience, _experience_to_record),
    "education": (TransferEducation, CandidateEducation, _education_to_record),
    "certifications": (TransferCertification, CandidateCertification, _certification_to_record),
    "projects": (TransferProject, CandidateProject, _project_to_record),
    "languages": (TransferLanguage, CandidateLanguage, _language_to_record),
}
_DUPLICATE_IDENTITY = {"skills": "name", "languages": "language", "projects": "name"}

_KNOWN_FIELD_NAMES = frozenset(
    {"format", "format_version", "exported_at", "source", "semantic_digest", "profile"}
    | {"dialect", "alembic_revision"}
    | set(TransferProfile.model_fields)
    | set(TransferJobPreferences.model_fields)
    | set(TransferFieldTrust.model_fields)
    | {name for spec in _COLLECTION_SPECS.values() for name in spec[0].model_fields}
)


# --- canonical JSON / digest -------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def semantic_digest(profile: TransferProfile) -> str:
    document = {
        "format": FORMAT_ID,
        "format_version": FORMAT_VERSION,
        "profile": profile.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _sanitized_errors(exc: ValidationError) -> str:
    """Error locations and types only -- pydantic's own message includes the
    offending INPUT VALUE, which here is candidate personal data."""
    parts = []
    for error in exc.errors()[:10]:
        loc = ".".join(
            str(p) if isinstance(p, int) or p in _KNOWN_FIELD_NAMES else "<key>"
            for p in error.get("loc", ())
        )
        parts.append(f"{loc or '<root>'}:{error.get('type', 'invalid')}")
    return ", ".join(parts)


# --- payload construction / validation ---------------------------------------------


def _domain_check(collection: str, item: _Strict) -> None:
    """Re-validate one item through the application's own domain model and
    require that doing so changes nothing (no silent normalization)."""
    _transfer_model, domain_model, _builder = _COLLECTION_SPECS[collection]
    data = item.model_dump(mode="json")
    try:
        domain = domain_model.model_validate(data)
    except ValidationError as exc:
        raise SnapshotInvalidError(
            f"{collection} item fails domain validation ({_sanitized_errors(exc)})"
        ) from None
    if domain.model_dump(mode="json", exclude=NON_SEMANTIC_ITEM_FIELDS) != data:
        raise SnapshotInvalidError(f"{collection} item is not in canonical domain form")


def _check_profile(profile: TransferProfile) -> None:
    unknown = set(profile.field_trust) - TOP_LEVEL_TRUST_FIELDS
    if unknown:
        raise SnapshotInvalidError(
            f"field_trust has {len(unknown)} key(s) that are not top-level trust fields"
        )
    for collection in COLLECTIONS:
        items = getattr(profile, collection)
        for item in items:
            _domain_check(collection, item)
        keys = [canonical_json(item.model_dump(mode="json")) for item in items]
        if keys != sorted(keys):
            raise SnapshotInvalidError(f"{collection} is not in canonical order")
        identity = _DUPLICATE_IDENTITY.get(collection)
        if identity is not None:
            normalized = [normalize_text_identity(getattr(item, identity)) for item in items]
            if len(set(normalized)) != len(normalized):
                raise SnapshotInvalidError(f"duplicate normalized {collection} entry")
    if profile.job_preferences is not None:
        data = profile.job_preferences.model_dump(mode="json")
        try:
            domain = CandidateJobPreferences.model_validate(data)
        except ValidationError as exc:
            raise SnapshotInvalidError(
                f"job_preferences fail domain validation ({_sanitized_errors(exc)})"
            ) from None
        if domain.model_dump(mode="json") != data:
            raise SnapshotInvalidError("job_preferences are not in canonical domain form")
    _check_column_lengths(profile)


def validate_profile_payload(payload: Any) -> TransferProfile:
    """Strictly validate a semantic payload (a JSON-shaped dict). Rejects
    anything whose validated, re-serialized form differs from the input."""
    try:
        profile = TransferProfile.model_validate_json(canonical_json(payload))
    except (ValidationError, ValueError, TypeError) as exc:
        detail = _sanitized_errors(exc) if isinstance(exc, ValidationError) else "not JSON"
        raise SnapshotInvalidError(f"profile payload is invalid ({detail})") from None
    if canonical_json(profile.model_dump(mode="json")) != canonical_json(payload):
        raise SnapshotInvalidError("profile payload is not in canonical form")
    _check_profile(profile)
    return profile


def _sorted_items(items: Iterable[dict]) -> list[dict]:
    return sorted(items, key=canonical_json)


def _strict_field_trust(raw: str) -> dict[str, dict[str, str]]:
    """field_trust_json exactly as stored: every entry must carry BOTH source
    and confidence. A partial entry is refused rather than defaulted (the
    repository's lenient loader would fill a missing confidence in as
    CONFIRMED -- a trust upgrade). A duplicate object key at any depth is
    ambiguous provenance (plain json.loads would keep the LAST value) and is
    refused too."""
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except SnapshotInvalidError:
        raise SourceProfileInvalidError(
            "field_trust_json contains a duplicate object key"
        ) from None
    except (TypeError, ValueError):
        raise SourceProfileInvalidError("field_trust_json is not valid JSON") from None
    if not isinstance(data, dict):
        raise SourceProfileInvalidError("field_trust_json is not a JSON object")
    trust: dict[str, dict[str, str]] = {}
    for name, entry in data.items():
        try:
            trust[name] = TransferFieldTrust.model_validate(entry).model_dump(mode="json")
        except ValidationError as exc:
            raise SourceProfileInvalidError(
                f"field_trust entry is incomplete or invalid ({_sanitized_errors(exc)})"
            ) from None
    return trust


def payload_from_record(record: CandidateProfileRecord) -> TransferProfile:
    """The semantic payload of a persisted profile. The ORM graph is first
    converted through the application's typed CandidateProfile response
    (malformed rows fail here, not silently later)."""
    try:
        response = to_candidate_profile_response(record)
    except ValidationError as exc:
        raise SourceProfileInvalidError(
            f"stored profile fails typed validation ({_sanitized_errors(exc)})"
        ) from None
    except (TypeError, ValueError):
        raise SourceProfileInvalidError("stored profile has malformed JSON columns") from None

    payload: dict[str, Any] = {name: getattr(response, name) for name in SCALAR_FIELDS}
    payload["target_roles"] = list(response.target_roles)
    payload["field_trust"] = _strict_field_trust(record.field_trust_json)
    for collection in COLLECTIONS:
        payload[collection] = _sorted_items(
            item.model_dump(mode="json", exclude=NON_SEMANTIC_ITEM_FIELDS)
            for item in getattr(response, collection)
        )
    payload["job_preferences"] = (
        None if record.job_preferences is None else response.job_preferences.model_dump(mode="json")
    )
    try:
        return validate_profile_payload(payload)
    except SnapshotInvalidError as exc:
        raise SourceProfileInvalidError(str(exc)) from None


# --- database helpers -------------------------------------------------------------


def application_head() -> str:
    from app.services.readiness import expected_alembic_heads

    heads = expected_alembic_heads()
    if len(heads) != 1:
        raise SchemaRevisionMismatchError(f"application has {len(heads)} migration heads")
    return heads[0]


def database_revisions(db: Session) -> list[str]:
    try:
        return [
            row[0] for row in db.execute(text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}"))
        ]
    except Exception:  # noqa: BLE001 -- missing table/unreadable: no revision
        db.rollback()
        return []


def require_head_revision(db: Session) -> str:
    head = application_head()
    revisions = database_revisions(db)
    if revisions != [head]:
        raise SchemaRevisionMismatchError(
            f"database holds {len(revisions)} revision row(s), not exactly the application head"
        )
    return head


def _table_count(db: Session, table_name: str) -> int:
    table = Base.metadata.tables[table_name]
    return int(db.execute(select(func.count()).select_from(table)).scalar_one())


def profile_domain_counts(db: Session) -> dict[str, int]:
    return {name: _table_count(db, name) for name in PROFILE_DOMAIN_TABLES}


def historical_state_tables() -> tuple[str, ...]:
    """Every application table outside the CandidateProfile domain -- derived
    from the ORM metadata, never a hand-written list that could go stale."""
    return tuple(sorted(name for name in Base.metadata.tables if name not in PROFILE_DOMAIN_TABLES))


def historical_state_counts(db: Session) -> dict[str, int]:
    return {name: _table_count(db, name) for name in historical_state_tables()}


def _check_column_lengths(profile: TransferProfile) -> None:
    """Refuse a value longer than its destination column (SQLite never
    enforces VARCHAR lengths; PostgreSQL would reject mid-transaction)."""
    records = build_records(profile)
    for record in _iter_records(records):
        for column in record.__table__.columns:
            if isinstance(column.type, String) and column.type.length is not None:
                value = getattr(record, column.key, None)
                if isinstance(value, str) and len(value) > column.type.length:
                    raise SnapshotInvalidError(
                        f"{record.__tablename__}.{column.key} exceeds its column length"
                    )


def _iter_records(profile_record: CandidateProfileRecord):
    yield profile_record
    for collection in COLLECTIONS:
        yield from getattr(profile_record, collection)
    if profile_record.job_preferences is not None:
        yield profile_record.job_preferences


def build_records(profile: TransferProfile) -> CandidateProfileRecord:
    """The complete, unsaved ORM graph for one fresh profile. Every value --
    including every source/confidence and the exact field_trust map -- comes
    from the snapshot; nothing falls back to a model or column default."""
    record = CandidateProfileRecord(
        id=1,
        profile_version=FRESH_PROFILE_VERSION,
        **{name: getattr(profile, name) for name in SCALAR_FIELDS},
        target_roles_json=json.dumps(list(profile.target_roles)),
        field_trust_json=json.dumps(
            {name: trust.model_dump(mode="json") for name, trust in profile.field_trust.items()}
        ),
    )
    for collection in COLLECTIONS:
        _transfer_model, domain_model, builder = _COLLECTION_SPECS[collection]
        setattr(
            record,
            collection,
            [
                builder(domain_model.model_validate(item.model_dump(mode="json")))
                for item in getattr(profile, collection)
            ],
        )
    if profile.job_preferences is not None:
        record.job_preferences = _preferences_to_record(
            CandidateJobPreferences.model_validate(profile.job_preferences.model_dump(mode="json"))
        )
    return record


# --- export / snapshot ------------------------------------------------------------


def export_profile(db: Session) -> TransferProfile:
    """PURE READ: never creates the singleton (get_candidate_profile, not
    get_or_create), never flushes, never commits. Requires the source schema
    to be exactly at the application head."""
    require_head_revision(db)
    count = _table_count(db, CandidateProfileRecord.__tablename__)
    if count != 1:
        raise SourceProfileMissingError(
            f"source holds {count} candidate profile(s); exactly one is required"
        )
    with db.no_autoflush:
        record = get_candidate_profile(db)
        if record is None:
            raise SourceProfileMissingError("source candidate profile is missing")
        return payload_from_record(record)


def build_snapshot(
    profile: TransferProfile, *, source_dialect: str, source_revision: str
) -> dict[str, Any]:
    return {
        "format": FORMAT_ID,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "source": {"dialect": source_dialect, "alembic_revision": source_revision},
        "semantic_digest": semantic_digest(profile),
        "profile": profile.model_dump(mode="json"),
    }


def serialize_snapshot(snapshot: dict[str, Any]) -> bytes:
    return (json.dumps(snapshot, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


class ParsedSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_dialect: str
    source_revision: str
    digest: str
    profile: TransferProfile


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise SnapshotInvalidError("snapshot JSON contains a duplicate object key")
    return dict(pairs)


def _reject_constant(_name: str) -> None:
    raise SnapshotInvalidError("snapshot JSON contains NaN/Infinity")


_SNAPSHOT_KEYS = frozenset(
    {"format", "format_version", "exported_at", "source", "semantic_digest", "profile"}
)


def parse_snapshot(data: bytes) -> ParsedSnapshot:
    """Validate a snapshot completely -- structure, format/version, strict
    profile payload, and the recomputed semantic digest -- before anything
    may act on it."""
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except SnapshotInvalidError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise SnapshotInvalidError("snapshot is not valid UTF-8 JSON") from None
    if not isinstance(document, dict) or set(document) != _SNAPSHOT_KEYS:
        raise SnapshotInvalidError("snapshot does not have exactly the expected top-level keys")
    if document["format"] != FORMAT_ID:
        raise SnapshotInvalidError("unsupported snapshot format")
    version = document["format_version"]
    if type(version) is not int or version != FORMAT_VERSION:
        raise SnapshotInvalidError("unsupported snapshot format_version")
    source = document["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"dialect", "alembic_revision"}
        or not all(isinstance(v, str) and v for v in source.values())
    ):
        raise SnapshotInvalidError("snapshot source metadata is malformed")
    declared = document["semantic_digest"]
    if not isinstance(declared, str) or len(declared) != 64:
        raise SnapshotInvalidError("snapshot semantic_digest is malformed")
    if not isinstance(document["exported_at"], str):
        raise SnapshotInvalidError("snapshot exported_at is malformed")

    profile = validate_profile_payload(document["profile"])
    actual = semantic_digest(profile)
    if actual != declared:
        raise SnapshotDigestMismatchError("snapshot content does not match its semantic digest")
    return ParsedSnapshot(
        source_dialect=source["dialect"],
        source_revision=source["alembic_revision"],
        digest=actual,
        profile=profile,
    )


# --- import -----------------------------------------------------------------------


def import_profile(db: Session, snapshot: ParsedSnapshot) -> str:
    """Create the destination's ONE CandidateProfile from a validated snapshot.

    Preflight (all before any write): schema exactly at the application head,
    the snapshot exported from that same revision, no candidate profile and no
    profile child rows, and every other application table empty (a fresh
    deployment -- historical state is never merged into). Then ONE
    transaction: add the complete graph, flush, re-read it from the database,
    and commit only if its semantic digest equals the snapshot's. Any error
    rolls everything back. There is no overwrite/merge/upsert mode.

    Returns the verified destination digest.
    """
    head = require_head_revision(db)
    if snapshot.source_revision != head:
        raise SchemaRevisionMismatchError(
            "snapshot was exported from a different schema revision than this application"
        )
    domain_counts = profile_domain_counts(db)
    occupied = sorted(name for name, count in domain_counts.items() if count)
    if occupied:
        raise DestinationNotEmptyError(
            f"destination already holds candidate profile data in {len(occupied)} table(s)"
        )
    historical = historical_state_counts(db)
    dirty = sorted(name for name, count in historical.items() if count)
    if dirty:
        raise DestinationNotEmptyError(
            f"destination is not a fresh deployment: {len(dirty)} operational table(s) hold rows"
        )
    # The preflight reads opened a transaction; end it so the import below is
    # its own single transaction from the first write to the commit.
    db.rollback()

    try:
        db.add(build_records(snapshot.profile))
        db.flush()
        db.expire_all()
        with db.no_autoflush:
            stored = get_candidate_profile(db)
            if stored is None:
                raise ImportVerificationError("imported profile is not readable")
            if stored.profile_version != FRESH_PROFILE_VERSION:
                raise ImportVerificationError("imported profile_version is not the fresh version")
            destination_digest = semantic_digest(payload_from_record(stored))
        if destination_digest != snapshot.digest:
            raise ImportVerificationError(
                "imported profile does not reproduce the snapshot's semantic digest"
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "candidate_profile_transfer_imported digest=%s %s",
        destination_digest,
        " ".join(f"{c}={len(getattr(snapshot.profile, c))}" for c in COLLECTIONS),
    )
    return destination_digest


# --- semantic / trust equivalence -------------------------------------------------


def _compare_models(path: str, left: BaseModel | None, right: BaseModel | None) -> list[str]:
    if left is None or right is None:
        return [] if left is right else [f"{path}:presence"]
    mismatches = []
    for name in type(left).model_fields:
        if getattr(left, name) != getattr(right, name):
            mismatches.append(f"{path}.{name}")
    return mismatches


def compare_profiles(source: TransferProfile, destination: TransferProfile) -> list[str]:
    """Explicit field-by-field comparison, independent of the digest code.
    Returns mismatching PATHS only (never values); empty means equal."""
    mismatches = [
        name for name in SCALAR_FIELDS if getattr(source, name) != getattr(destination, name)
    ]
    if source.target_roles != destination.target_roles:
        mismatches.append("target_roles")
    if set(source.field_trust) != set(destination.field_trust):
        mismatches.append("field_trust:keys")
    for name in sorted(set(source.field_trust) & set(destination.field_trust)):
        mismatches.extend(
            _compare_models(
                f"field_trust.{name}", source.field_trust[name], destination.field_trust[name]
            )
        )
    for collection in COLLECTIONS:
        left, right = getattr(source, collection), getattr(destination, collection)
        if len(left) != len(right):
            mismatches.append(f"{collection}:count")
            continue
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            mismatches.extend(_compare_models(f"{collection}[{index}]", a, b))
    mismatches.extend(
        _compare_models("job_preferences", source.job_preferences, destination.job_preferences)
    )
    return mismatches


def trust_decisions(profile: CandidateProfile) -> dict[str, Any]:
    """Every generation-trust decision the application derives from a
    profile: top-level usability per TOP_LEVEL_TRUST_FIELDS field, and per
    nested fact (keyed by its canonical content, ids excluded) the
    source/confidence pair plus is_usable_for_generation."""
    decisions: dict[str, Any] = {
        "top_level": {
            name: is_top_level_fact_usable_for_generation(profile, name)
            for name in sorted(TOP_LEVEL_TRUST_FIELDS)
        }
    }
    for collection in COLLECTIONS:
        decisions[collection] = sorted(
            (
                canonical_json(item.model_dump(mode="json", exclude=NON_SEMANTIC_ITEM_FIELDS)),
                item.source,
                item.confidence,
                is_usable_for_generation(item.source, item.confidence),
            )
            for item in getattr(profile, collection)
        )
    return decisions


def read_profile_response(db: Session) -> CandidateProfile:
    with db.no_autoflush:
        record = get_candidate_profile(db)
        if record is None:
            raise SourceProfileMissingError("candidate profile is missing")
        return to_candidate_profile_response(record)
