"""Read-only READY_FOR_SEND_PACKAGE gate (Stage 14A groundwork for 14B).

Answers "could a Stage 14B send-ready package be assembled for this job?"
with one boolean and an explicit, ordered list of checks. It never sends,
never writes, never changes a flag: `evaluate_send_readiness` is a pure
function and `load_send_readiness_inputs` only reads through existing
repositories, refuses a session with pending ORM changes and reads under
`no_autoflush`.

"Ready for send package" means ready for a DRAFT package with a real
recipient and CV -- not ready to send. Outbound sending being DISABLED is
itself one of the required checks: if `OUTBOUND_SENDING_ENABLED` is ever
true the gate reports NOT ready, so this gate can never be read as a reason
to turn outbound on.

**Vacancy-specific, fail closed (Codex 14A review).**

- Destination: only a human-operator-verified, vacancy-specific portal or
  email passes (`is_channel_verified`; an email must pass the strict email
  rule).
- Package currency: the approved package must still be exactly current --
  job fingerprint and display context, profile version, matcher / CV-adapter
  / generator versions, the exact pinned match/CV/letter triple, and the
  approved revision being the review's own current revision. Old packages
  generated under older semantic versions fail closed. The letter check
  reuses `bewerbung_draft_is_current` (the Stage 8C/9B currency authority).
- Evidence: an experience or project counts only if the PINNED match marks
  it relevant (non-empty `matched_skills`), it is still trusted in the
  current profile, and the approved CV cites it. Generic profile readiness
  (`ProfileReadinessReport`) stays vacancy-independent and is advisory.
- Title: the approved package's reviewed title must equal the current
  trusted profile title. There is no override path.
- Education / language: required only where the pinned match carries a
  binding (non-PREFERRED) requirement of that type; then only trusted
  evidence satisfying it counts (a completed degree; a stated, sufficient
  level -- UNKNOWN never satisfies a required level).
- Location: authoritative (employer) sources are evaluated independently;
  any disagreement between them, a missing persisted job location, or a
  persisted / board location outside the authoritative set is a conflict.
  No operator resolution is modeled in Stage 14A: a conflict stays NOT
  ready until the underlying data or evidence changes.
- Contact data (Stage 14D): operator-confirmed `ApplicationIdentity` with
  e-mail and phone whose name equals the trusted profile name; absent ->
  `CONTACT_DATA_MISSING`.
- Research binding (Stage 14D): a destination / location evidence item that
  carries a stable `VacancyIdentity` binds by it (never by a database-local
  job id); a mismatch raises `VacancyEvidenceMismatchError` (typed, so the
  CLI reports BLOCKED instead of a traceback).

Stage 14B must consume this module rather than re-implement any check.
"""

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.agents.bewerbung_generator import BEWERBUNG_GENERATOR_VERSION
from app.agents.candidate_job_matcher import ALGORITHM_VERSION
from app.agents.cv_adapter import CV_ADAPTER_VERSION
from app.agents.profile_readiness import (
    AREA_NAME,
    AREA_TITLE,
    ProfileReadinessReport,
    evaluate_profile_readiness,
)
from app.core.config import Settings
from app.db.bewerbung_repository import get_bewerbung_draft_by_id
from app.db.candidate_cv_draft_repository import get_draft_by_id, to_tailored_cv_draft
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    get_match_by_id,
    to_candidate_job_match,
)
from app.db.candidate_profile_repository import get_candidate_profile, to_candidate_profile_response
from app.db.models import (
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    CandidateJobMatchRecord,
    JobRecord,
)
from app.db.review_package_repository import (
    get_latest_approved_review_for_job,
    get_latest_review_for_job,
    get_latest_revision,
    get_revision_by_id,
    to_review_package,
)
from app.models.application_destination import (
    EMPLOYER_SOURCES,
    ApplicationDestination,
    DestinationSourceType,
    VacancyIdentity,
    is_channel_verified,
    is_email_send_ready,
    is_strict_https_url,
    vacancy_identity_matches,
)
from app.models.application_identity import ApplicationIdentity, contact_data_problems
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.candidate_profile import (
    CandidateProfile,
    is_top_level_fact_usable_for_generation,
    is_usable_for_generation,
)
from app.models.cv_draft import TailoredCVDraft
from app.models.review_package import ReviewPackage
from app.services.bewerbung_reuse import bewerbung_draft_is_current
from app.services.email_matching import extract_reference_tokens

CHECK_TRUSTED_TITLE = "TRUSTED_CANDIDATE_TITLE"
CHECK_RELEVANT_EVIDENCE = "RELEVANT_STRUCTURED_EVIDENCE"
CHECK_REQUIRED_CV_DATA = "REQUIRED_CV_DATA"
CHECK_CONTACT_DATA = "APPLICATION_CONTACT_DATA_COMPLETE"
CHECK_EDUCATION_REQUIREMENT = "EDUCATION_REQUIREMENT_SATISFIED"
CHECK_LANGUAGE_REQUIREMENT = "LANGUAGE_REQUIREMENT_SATISFIED"
CHECK_APPROVED_REVISION = "APPROVED_BEWERBUNG_REVISION"
CHECK_APPROVED_IS_CURRENT = "APPROVED_PACKAGE_MATCHES_CURRENT_PROFILE"
CHECK_CHANNEL_VERIFIED = "APPLICATION_CHANNEL_VERIFIED"
CHECK_RECIPIENT_TYPE = "RECIPIENT_TYPE_KNOWN"
CHECK_LOCATION_CONSISTENT = "NO_CONTRADICTORY_LOCATION_EVIDENCE"
CHECK_OUTBOUND_DISABLED = "OUTBOUND_STILL_DISABLED"

# Package-currency problem codes (`package_currency_problems`).
PACKAGE_CURRENCY_NOT_CHECKED = "PACKAGE_CURRENCY_NOT_CHECKED"
NO_APPROVED_REVIEW = "NO_APPROVED_REVIEW"
REVIEW_NOT_APPROVED = "REVIEW_NOT_APPROVED"
REVIEW_FOR_OTHER_JOB = "REVIEW_FOR_OTHER_JOB"
SUPERSEDED_BY_NEWER_REVIEW = "SUPERSEDED_BY_NEWER_REVIEW"
NO_APPROVED_REVISION = "NO_APPROVED_REVISION"
REVISION_NOT_OWNED_BY_REVIEW = "APPROVED_REVISION_NOT_OWNED_BY_REVIEW"
REVISION_NOT_CURRENT = "APPROVED_REVISION_NOT_CURRENT"
PACKAGE_UNAVAILABLE = "PINNED_DRAFT_MISSING"
PINS_INCONSISTENT = "PINNED_DRAFTS_INCONSISTENT"
STALE_PROFILE = "STALE_PROFILE_VERSION"
STALE_JOB_FINGERPRINT = "STALE_JOB_FINGERPRINT"
STALE_MATCHER_VERSION = "STALE_MATCHER_VERSION"
STALE_CV_ADAPTER_VERSION = "STALE_CV_ADAPTER_VERSION"
STALE_GENERATOR_VERSION = "STALE_BEWERBUNG_GENERATOR_VERSION"
STALE_LETTER = "BEWERBUNG_DRAFT_NOT_CURRENT"

# Requirement importance that does NOT bind the gate. UNKNOWN importance is
# treated as binding (fail closed).
_ADVISORY_IMPORTANCE = "PREFERRED"


class SessionNotCleanError(ValueError):
    """The caller's session carries pending ORM changes; reading through it
    could flush them, so the read-only loader refuses."""


class NoCandidateProfileError(ValueError):
    """There is no candidate profile to evaluate."""


DESTINATION_VACANCY_MISMATCH = "DESTINATION_VACANCY_MISMATCH"
LOCATION_EVIDENCE_VACANCY_MISMATCH = "LOCATION_EVIDENCE_VACANCY_MISMATCH"


class VacancyEvidenceMismatchError(ValueError):
    """Stage 14D: a research artifact names a different vacancy than the
    job being evaluated. Expected operator-input failure; `code` is one of
    the *_VACANCY_MISMATCH constants."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class LocationEvidence(BaseModel):
    """What one source says about where the job is. `locations` are
    city names as published; comparison is casefolded.

    Only an employer source (`EMPLOYER_SOURCES`) with an https
    `source_url` and a `checked_at` may be `authoritative`; a job board or
    aggregator can never overrule the employer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: DestinationSourceType
    source_url: str | None = None
    locations: list[str] = Field(default_factory=list)
    authoritative: bool = False
    # Stage 14D: stable vacancy binding (see VacancyIdentity). Optional for
    # backward compatibility; when present it must match the job.
    vacancy: VacancyIdentity | None = None
    note: str | None = Field(default=None, max_length=500)
    checked_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> "LocationEvidence":
        if self.source_url is not None and not is_strict_https_url(self.source_url):
            raise ValueError("source_url must be a valid https:// URL with a hostname")
        if self.authoritative and not _authority_ok(self):
            raise ValueError(
                "only an employer source with https source_url and checked_at can be authoritative"
            )
        return self


def _authority_ok(item: LocationEvidence) -> bool:
    return (
        item.source_type in EMPLOYER_SOURCES
        and is_strict_https_url(item.source_url)
        # isinstance, not `is not None`: model_construct can smuggle a str.
        and isinstance(item.checked_at, datetime)
    )


@dataclass(frozen=True)
class ReadinessCheck:
    code: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SendReadinessResult:
    job_id: int
    checks: tuple[ReadinessCheck, ...]
    email_send_ready: bool = False

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def blockers(self) -> list[ReadinessCheck]:
        return [check for check in self.checks if not check.passed]


@dataclass(frozen=True)
class SendReadinessInputs:
    job_id: int
    job_location: str | None
    profile: CandidateProfile
    outbound_sending_enabled: bool
    approved_package: ReviewPackage | None = None
    approved_cv: TailoredCVDraft | None = None
    match: CandidateJobMatch | None = None
    # Output of `package_currency_problems`; the default fails closed so an
    # input assembled without the currency check can never pass.
    package_problems: tuple[str, ...] = (PACKAGE_CURRENCY_NOT_CHECKED,)
    destination: ApplicationDestination | None = None
    location_evidence: list[LocationEvidence] = field(default_factory=list)
    # Stage 14D: operator-confirmed contact data (None -> CONTACT_DATA_MISSING).
    application_identity: ApplicationIdentity | None = None

    @property
    def profile_report(self) -> ProfileReadinessReport:
        return evaluate_profile_readiness(self.profile)


# --- package currency --------------------------------------------------------


def package_currency_problems(
    *,
    job: JobRecord,
    profile_version: int,
    review: ApplicationPackageReviewRecord | None,
    newest_review_id: int | None,
    revision: ApplicationPackageReviewRevisionRecord | None,
    latest_revision: ApplicationPackageReviewRevisionRecord | None,
    match: CandidateJobMatchRecord | None,
    cv: CandidateCVDraftRecord | None,
    letter: BewerbungDraftRecord | None,
) -> tuple[str, ...]:
    """Every reason the approved package is NOT exactly current; empty
    means current. Pure over already-loaded records."""
    if review is None:
        return (NO_APPROVED_REVIEW,)
    problems: list[str] = []
    if review.status != "APPROVED":
        problems.append(REVIEW_NOT_APPROVED)
    if review.job_id != job.id:
        problems.append(REVIEW_FOR_OTHER_JOB)
    if newest_review_id != review.id:
        problems.append(SUPERSEDED_BY_NEWER_REVIEW)

    # The exact approved revision: owned by this review, the review's
    # current revision, and the newest revision it has.
    if review.approved_revision_id is None:
        problems.append(NO_APPROVED_REVISION)
    elif (
        revision is None
        or revision.id != review.approved_revision_id
        or revision.review_id != review.id
    ):
        problems.append(REVISION_NOT_OWNED_BY_REVIEW)
    elif (
        revision.revision_number != review.review_version
        or latest_revision is None
        or latest_revision.id != revision.id
    ):
        problems.append(REVISION_NOT_CURRENT)

    if match is None or cv is None or letter is None:
        problems.append(PACKAGE_UNAVAILABLE)
        return tuple(problems)

    consistent = (
        review.match_id == match.id
        and review.cv_draft_id == cv.id
        and review.bewerbung_draft_id == letter.id
        and match.job_id == job.id
        and cv.job_id == job.id
        and letter.job_id == job.id
        and cv.match_id == match.id
        and letter.cv_draft_id == cv.id
        and letter.match_id == match.id
        and review.candidate_profile_version == cv.candidate_profile_version
        and review.candidate_profile_version == match.candidate_profile_version
        and review.job_snapshot_fingerprint == cv.job_snapshot_fingerprint
        and review.job_snapshot_fingerprint == match.job_snapshot_fingerprint
        and review.match_algorithm_version == cv.match_algorithm_version
        and review.match_algorithm_version == match.algorithm_version
        and review.cv_adapter_version == cv.cv_adapter_version
        and review.bewerbung_generator_version == letter.bewerbung_generator_version
    )
    if not consistent:
        problems.append(PINS_INCONSISTENT)

    if review.candidate_profile_version != profile_version:
        problems.append(STALE_PROFILE)
    if review.job_snapshot_fingerprint != compute_job_snapshot_fingerprint(job):
        problems.append(STALE_JOB_FINGERPRINT)
    if review.match_algorithm_version != ALGORITHM_VERSION:
        problems.append(STALE_MATCHER_VERSION)
    if review.cv_adapter_version != CV_ADAPTER_VERSION:
        problems.append(STALE_CV_ADAPTER_VERSION)
    if review.bewerbung_generator_version != BEWERBUNG_GENERATOR_VERSION:
        problems.append(STALE_GENERATOR_VERSION)
    # The shared currency authority: letter pins vs. the CV, current
    # generator/provider, current fingerprint, exact title/company context.
    if not bewerbung_draft_is_current(letter, cv, job):
        problems.append(STALE_LETTER)
    return tuple(problems)


# --- location ----------------------------------------------------------------


def _norm(city: str) -> str:
    return " ".join(city.split()).casefold()


def location_conflicts(job_location: str | None, evidence: list[LocationEvidence]) -> list[str]:
    """Contradictions between the persisted job location and the evidence.

    Authoritative sources are evaluated independently, never unioned: two
    authoritative sources that disagree are an unresolved conflict. No
    valid authoritative evidence, or no persisted job location, is itself a
    conflict -- absence of a check is not consistency."""
    conflicts: list[str] = []
    authoritative: list[frozenset[str]] = []
    for item in evidence:
        if not item.authoritative:
            continue
        if not _authority_ok(item):
            conflicts.append(f"AUTHORITY_NOT_ALLOWED:{item.source_type}")
        elif not item.locations:
            conflicts.append(f"AUTHORITATIVE_SOURCE_WITHOUT_LOCATIONS:{item.source_type}")
        else:
            authoritative.append(frozenset(_norm(city) for city in item.locations))
    if not authoritative:
        conflicts.append("NO_AUTHORITATIVE_LOCATION_EVIDENCE")
        return conflicts
    if len(set(authoritative)) > 1:
        conflicts.append("AUTHORITATIVE_SOURCES_DISAGREE")
    if not (job_location or "").strip():
        conflicts.append("JOB_LOCATION_MISSING")
    elif any(_norm(job_location) not in locations for locations in authoritative):
        conflicts.append("JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS")
    for item in evidence:
        if item.authoritative or not item.locations:
            continue
        listed = {_norm(city) for city in item.locations}
        if any(listed - locations for locations in authoritative):
            conflicts.append(f"SOURCE_LISTS_OTHER_LOCATIONS:{item.source_type}")
    return conflicts


# --- vacancy-specific profile checks ----------------------------------------


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _trusted_title(profile: CandidateProfile) -> str:
    title = _clean(profile.professional_title)
    if title and is_top_level_fact_usable_for_generation(profile, "professional_title"):
        return title
    return ""


def _trusted_ids(items: list) -> set[int]:
    return {
        item.id
        for item in items
        if item.id is not None and is_usable_for_generation(item.source, item.confidence)
    }


def relevant_evidence_ids(
    profile: CandidateProfile, match: CandidateJobMatch | None, cv: TailoredCVDraft | None
) -> tuple[set[int], set[int]]:
    """(experience ids, project ids) that the PINNED match marks relevant to
    this vacancy, that are still trusted in the current profile, and that
    the approved CV cites as matched."""
    if match is None or cv is None or cv.match_id != match.id:
        return set(), set()
    experiences = (
        _trusted_ids(profile.experiences)
        & {r.experience_id for r in match.relevant_experiences if r.matched_skills}
        & {item.source_id for item in cv.experience if item.matched_skills}
    )
    projects = (
        _trusted_ids(profile.projects)
        & {r.project_id for r in match.relevant_projects if r.matched_skills}
        & {item.source_id for item in cv.projects if item.matched_skills}
    )
    return experiences, projects


def _binding_requirements(
    match: CandidateJobMatch, requirement_type: str
) -> list[RequirementMatch]:
    return [
        req
        for req in (
            *match.matched_requirements,
            *match.partial_requirements,
            *match.missing_requirements,
            *match.unknown_requirements,
        )
        if req.requirement_type == requirement_type and req.importance != _ADVISORY_IMPORTANCE
    ]


def _requirement_satisfied(req: RequirementMatch, entity_type: str, usable: set[int]) -> bool:
    refs = [ref.entity_id for ref in req.candidate_evidence if ref.entity_type == entity_type]
    return req.match_status == "MATCH" and bool(refs) and all(ref in usable for ref in refs)


def education_problems(
    profile: CandidateProfile, match: CandidateJobMatch | None, cv: TailoredCVDraft | None
) -> list[str]:
    if match is None:
        return ["NO_PINNED_MATCH"]
    completed = {
        edu.id
        for edu in profile.education
        if edu.id is not None
        and edu.completed
        and is_usable_for_generation(edu.source, edu.confidence)
    }
    problems = [
        f"REQUIREMENT_NOT_SATISFIED:{req.normalized_requirement}"
        for req in _binding_requirements(match, "EDUCATION")
        if not _requirement_satisfied(req, "EDUCATION", completed)
    ]
    # Independently of any requirement: an unfinished degree is never
    # presented as completed.
    if cv is not None and any(
        item.completed and item.source_id not in completed for item in cv.education
    ):
        problems.append("CV_CLAIMS_UNFINISHED_EDUCATION_AS_COMPLETED")
    return problems


def language_problems(profile: CandidateProfile, match: CandidateJobMatch | None) -> list[str]:
    if match is None:
        return ["NO_PINNED_MATCH"]
    # A trusted language with a stated level. The pinned match (current
    # profile version + current matcher, enforced by package currency)
    # already compared that level against the required one.
    leveled = {
        lang.id
        for lang in profile.languages
        if lang.id is not None
        and lang.level != "UNKNOWN"
        and is_usable_for_generation(lang.source, lang.confidence)
    }
    return [
        f"REQUIREMENT_NOT_SATISFIED:{req.normalized_requirement}"
        for req in _binding_requirements(match, "LANGUAGE")
        if not _requirement_satisfied(req, "LANGUAGE", leveled)
    ]


# --- gate --------------------------------------------------------------------


def evaluate_send_readiness(inputs: SendReadinessInputs) -> SendReadinessResult:
    profile = inputs.profile
    report = inputs.profile_report
    package = inputs.approved_package
    cv = inputs.approved_cv
    match = inputs.match
    destination = inputs.destination
    checks: list[ReadinessCheck] = []

    def add(code: str, passed: bool, detail: str) -> None:
        checks.append(ReadinessCheck(code=code, passed=passed, detail=detail))

    pinned = (
        package is not None
        and cv is not None
        and match is not None
        and package.cv_draft_id == cv.id
        and package.match_id == match.id
        and cv.match_id == match.id
    )

    trusted_title = _trusted_title(profile)
    reviewed_title = _clean(package.reviewed_cv.professional_title.value) if package else ""
    title_equal = bool(trusted_title) and reviewed_title == trusted_title
    add(
        CHECK_TRUSTED_TITLE,
        title_equal,
        f"profile title={report.status(AREA_TITLE)}, reviewed package title equals trusted "
        f"title={'yes' if title_equal else 'no'}",
    )

    experiences, projects = relevant_evidence_ids(profile, match, cv) if pinned else (set(), set())
    add(
        CHECK_RELEVANT_EVIDENCE,
        bool(experiences or projects),
        f"match-relevant trusted experiences cited={len(experiences)}, "
        f"projects cited={len(projects)}, pinned match/CV={'yes' if pinned else 'no'}",
    )

    add(
        CHECK_REQUIRED_CV_DATA,
        report.status(AREA_NAME) == "VERIFIED",
        f"name={report.status(AREA_NAME)}",
    )

    contact = contact_data_problems(inputs.application_identity, profile)
    add(
        CHECK_CONTACT_DATA,
        not contact,
        ", ".join(contact) if contact else "operator-confirmed e-mail and phone present",
    )

    edu = education_problems(profile, match if pinned else None, cv)
    add(
        CHECK_EDUCATION_REQUIREMENT,
        not edu,
        ", ".join(edu) if edu else "no unmet binding education requirement",
    )
    lang = language_problems(profile, match if pinned else None)
    add(
        CHECK_LANGUAGE_REQUIREMENT,
        not lang,
        ", ".join(lang) if lang else "no unmet binding language requirement",
    )

    approved = (
        package is not None
        and package.status == "APPROVED"
        and package.approved_revision_id is not None
        and package.current_revision_id == package.approved_revision_id
    )
    add(
        CHECK_APPROVED_REVISION,
        approved,
        f"approved review id={package.id if package else None}, "
        f"revision id={package.approved_revision_id if package else None}",
    )
    problems = list(inputs.package_problems)
    if package is not None and package.candidate_profile_version != profile.profile_version:
        problems.append(STALE_PROFILE)
    current = approved and not problems
    add(
        CHECK_APPROVED_IS_CURRENT,
        current,
        f"package profile_version={package.candidate_profile_version if package else None}, "
        f"current={profile.profile_version}"
        + (f", problems: {', '.join(dict.fromkeys(problems))}" if problems else ""),
    )

    add(
        CHECK_CHANNEL_VERIFIED,
        is_channel_verified(destination),
        "no destination"
        if destination is None
        else f"{destination.kind}/{destination.verification_status}/"
        f"verified_by={destination.verified_by}/{destination.source_type}/"
        f"confidence={destination.confidence}/vacancy_specific={destination.vacancy_specific}",
    )
    add(
        CHECK_RECIPIENT_TYPE,
        destination is not None and destination.kind != "UNKNOWN",
        f"kind={destination.kind if destination else None}",
    )

    conflicts = location_conflicts(inputs.job_location, inputs.location_evidence)
    add(
        CHECK_LOCATION_CONSISTENT,
        not conflicts,
        ", ".join(conflicts) if conflicts else "consistent",
    )

    add(
        CHECK_OUTBOUND_DISABLED,
        not inputs.outbound_sending_enabled,
        f"OUTBOUND_SENDING_ENABLED={inputs.outbound_sending_enabled}",
    )

    return SendReadinessResult(
        job_id=inputs.job_id,
        checks=tuple(checks),
        email_send_ready=is_email_send_ready(destination),
    )


# --- read-only loader --------------------------------------------------------


def _identity_matches_job(identity: VacancyIdentity, job: JobRecord) -> bool:
    return vacancy_identity_matches(
        identity,
        source=job.source,
        # Stage 22 P0-1: the stored full reference too -- URL tokens miss a
        # BA reference with "_"/"." and every posting with an external URL.
        reference_tokens=extract_reference_tokens(job.title, job.url)
        | ({job.source_reference} if job.source_reference else set()),
        fingerprint=job.fingerprint,
    )


def destination_matches_job(destination: ApplicationDestination, job: JobRecord) -> bool:
    """Stage 14D: a destination with a stable `vacancy` identity binds by
    that identity only (its `job_id`, if any, is a database-local hint from
    the database it was researched in and is ignored); a legacy destination
    without one still binds by `job_id`."""
    if destination.vacancy is not None:
        return _identity_matches_job(destination.vacancy, job)
    return destination.job_id == job.id


def load_send_readiness_inputs(
    db: Session,
    job: JobRecord,
    *,
    settings: Settings,
    destination: ApplicationDestination | None,
    location_evidence: list[LocationEvidence],
    application_identity: ApplicationIdentity | None = None,
) -> SendReadinessInputs:
    """Read-only assembly through existing repositories.

    Refuses (`SessionNotCleanError`) a session with pending new/dirty/
    deleted ORM state BEFORE any read, and performs every read under
    `no_autoflush`. Never flushes, never commits, never mutates a record;
    the caller owns (and should roll back) the session."""
    if db.new or db.dirty or db.deleted:
        raise SessionNotCleanError(
            "session has pending ORM changes; the send-readiness loader reads only "
            "through a clean session"
        )
    if destination is not None and not destination_matches_job(destination, job):
        raise VacancyEvidenceMismatchError(
            DESTINATION_VACANCY_MISMATCH, "the destination names a different vacancy"
        )
    for item in location_evidence:
        if item.vacancy is not None and not _identity_matches_job(item.vacancy, job):
            raise VacancyEvidenceMismatchError(
                LOCATION_EVIDENCE_VACANCY_MISMATCH,
                "location evidence names a different vacancy",
            )

    package: ReviewPackage | None = None
    cv: TailoredCVDraft | None = None
    match: CandidateJobMatch | None = None
    with db.no_autoflush:
        record = get_candidate_profile(db)
        if record is None:
            raise NoCandidateProfileError("no candidate profile exists")
        profile = to_candidate_profile_response(record)
        review = get_latest_approved_review_for_job(db, job.id)
        if review is None:
            problems: tuple[str, ...] = (NO_APPROVED_REVIEW,)
        else:
            newest = get_latest_review_for_job(db, job.id)
            revision = (
                get_revision_by_id(db, review.approved_revision_id)
                if review.approved_revision_id is not None
                else None
            )
            latest_revision = get_latest_revision(db, review.id)
            match_record = get_match_by_id(db, review.match_id)
            cv_record = get_draft_by_id(db, review.cv_draft_id)
            letter = get_bewerbung_draft_by_id(db, review.bewerbung_draft_id)
            problems = package_currency_problems(
                job=job,
                profile_version=record.profile_version,
                review=review,
                newest_review_id=newest.id if newest is not None else None,
                revision=revision,
                latest_revision=latest_revision,
                match=match_record,
                cv=cv_record,
                letter=letter,
            )
            if revision is not None and revision.review_id == review.id:
                package = to_review_package(review, revision)
            if cv_record is not None:
                cv = to_tailored_cv_draft(cv_record)
            if match_record is not None:
                match = to_candidate_job_match(match_record)
    return SendReadinessInputs(
        job_id=job.id,
        job_location=job.location or None,
        profile=profile,
        outbound_sending_enabled=settings.outbound_sending_enabled,
        approved_package=package,
        approved_cv=cv,
        match=match,
        package_problems=problems,
        destination=destination,
        location_evidence=list(location_evidence),
        application_identity=application_identity,
    )


def render_send_readiness(result: SendReadinessResult) -> str:
    lines = [
        f"READY_FOR_SEND_PACKAGE = {'YES' if result.ready else 'NO'} (job_id={result.job_id})",
        f"EMAIL_SEND_READY = {'YES' if result.email_send_ready else 'NO'}",
    ]
    for check in result.checks:
        lines.append(f"[{'PASS' if check.passed else 'FAIL'}] {check.code}: {check.detail}")
    return "\n".join(lines)
