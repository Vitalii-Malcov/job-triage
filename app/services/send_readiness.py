"""Read-only READY_FOR_SEND_PACKAGE gate (Stage 14A groundwork for 14B).

Answers "could a Stage 14B send-ready package be assembled for this job?"
with one boolean and an explicit, ordered list of checks. It never sends,
never writes, never changes a flag: `evaluate_send_readiness` is a pure
function and `load_send_readiness_inputs` only reads through existing
repositories.

"Ready for send package" means ready for a DRAFT package with a real
recipient and CV -- not ready to send. Outbound sending being DISABLED is
itself one of the required checks: if `OUTBOUND_SENDING_ENABLED` is ever
true the gate reports NOT ready, so this gate can never be read as a reason
to turn outbound on.

Stage 14B must consume this module rather than re-implement any check.
"""

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agents.profile_readiness import (
    AREA_EDUCATION,
    AREA_LANGUAGES,
    AREA_NAME,
    AREA_TITLE,
    ProfileReadinessReport,
    evaluate_profile_readiness,
)
from app.core.config import Settings
from app.db.candidate_cv_draft_repository import get_draft_by_id, to_tailored_cv_draft
from app.db.candidate_profile_repository import get_candidate_profile, to_candidate_profile_response
from app.db.models import JobRecord
from app.db.review_package_repository import (
    get_latest_approved_review_for_job,
    get_revision_by_id,
    to_review_package,
)
from app.models.application_destination import (
    ApplicationDestination,
    is_channel_verified,
    is_email_send_ready,
)
from app.models.cv_draft import TailoredCVDraft
from app.models.review_package import ReviewPackage

CHECK_TRUSTED_TITLE = "TRUSTED_CANDIDATE_TITLE"
CHECK_RELEVANT_EVIDENCE = "RELEVANT_STRUCTURED_EVIDENCE"
CHECK_REQUIRED_CV_DATA = "REQUIRED_CV_DATA"
CHECK_APPROVED_REVISION = "APPROVED_BEWERBUNG_REVISION"
CHECK_APPROVED_IS_CURRENT = "APPROVED_PACKAGE_MATCHES_CURRENT_PROFILE"
CHECK_CHANNEL_VERIFIED = "APPLICATION_CHANNEL_VERIFIED"
CHECK_RECIPIENT_TYPE = "RECIPIENT_TYPE_KNOWN"
CHECK_LOCATION_CONSISTENT = "NO_CONTRADICTORY_LOCATION_EVIDENCE"
CHECK_OUTBOUND_DISABLED = "OUTBOUND_STILL_DISABLED"


class LocationEvidence(BaseModel):
    """What one source says about where the job is. `locations` are
    city names as published; comparison is casefolded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1, max_length=50)
    source_url: str | None = None
    locations: list[str] = Field(default_factory=list)
    authoritative: bool = False
    note: str | None = Field(default=None, max_length=500)
    checked_at: datetime | None = None


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
    profile_report: ProfileReadinessReport
    outbound_sending_enabled: bool
    approved_package: ReviewPackage | None = None
    approved_cv: TailoredCVDraft | None = None
    destination: ApplicationDestination | None = None
    location_evidence: list[LocationEvidence] = field(default_factory=list)


def _norm(city: str) -> str:
    return " ".join(city.split()).casefold()


def location_conflicts(job_location: str | None, evidence: list[LocationEvidence]) -> list[str]:
    """Contradictions between the persisted job location and the
    authoritative (employer) evidence. No authoritative evidence at all is
    itself reported -- absence of a check is not consistency."""
    authoritative = [e for e in evidence if e.authoritative and e.locations]
    if not authoritative:
        return ["NO_AUTHORITATIVE_LOCATION_EVIDENCE"]
    allowed = {_norm(city) for e in authoritative for city in e.locations}
    conflicts: list[str] = []
    if job_location and _norm(job_location) not in allowed:
        conflicts.append("JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS")
    for item in evidence:
        if item.authoritative or not item.locations:
            continue
        if {_norm(city) for city in item.locations} - allowed:
            conflicts.append(f"SOURCE_LISTS_OTHER_LOCATIONS:{item.source_type}")
    return conflicts


def evaluate_send_readiness(inputs: SendReadinessInputs) -> SendReadinessResult:
    report = inputs.profile_report
    package = inputs.approved_package
    cv = inputs.approved_cv
    destination = inputs.destination
    checks: list[ReadinessCheck] = []

    def add(code: str, passed: bool, detail: str) -> None:
        checks.append(ReadinessCheck(code=code, passed=passed, detail=detail))

    title_in_package = bool(
        package is not None and (package.reviewed_cv.professional_title.value or "").strip()
    )
    add(
        CHECK_TRUSTED_TITLE,
        report.status(AREA_TITLE) == "VERIFIED" and title_in_package,
        f"profile title={report.status(AREA_TITLE)}, approved package has title="
        f"{'yes' if title_in_package else 'no'}",
    )

    cv_evidence = bool(cv is not None and (cv.experience or cv.projects))
    add(
        CHECK_RELEVANT_EVIDENCE,
        report.has_relevant_structured_evidence and cv_evidence,
        f"profile experience/project verified={report.has_relevant_structured_evidence}, "
        f"approved CV cites experience/project={cv_evidence}",
    )

    missing = [
        area
        for area in (AREA_NAME, AREA_EDUCATION, AREA_LANGUAGES)
        if report.status(area) != "VERIFIED"
    ]
    add(
        CHECK_REQUIRED_CV_DATA,
        not missing,
        "missing: " + ", ".join(missing) if missing else "name, education, languages verified",
    )

    approved = package is not None and package.status == "APPROVED"
    add(
        CHECK_APPROVED_REVISION,
        approved and package.approved_revision_id is not None,
        f"approved review id={package.id if package else None}, "
        f"revision id={package.approved_revision_id if package else None}",
    )
    current = approved and package.candidate_profile_version == report.profile_version
    add(
        CHECK_APPROVED_IS_CURRENT,
        current,
        f"package profile_version={package.candidate_profile_version if package else None}, "
        f"current={report.profile_version}",
    )

    add(
        CHECK_CHANNEL_VERIFIED,
        is_channel_verified(destination),
        "no destination"
        if destination is None
        else f"{destination.kind}/{destination.verification_status}/"
        f"{destination.source_type}/confidence={destination.confidence}/"
        f"vacancy_specific={destination.vacancy_specific}",
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


def load_send_readiness_inputs(
    db: Session,
    job: JobRecord,
    *,
    settings: Settings,
    destination: ApplicationDestination | None,
    location_evidence: list[LocationEvidence],
) -> SendReadinessInputs:
    """Read-only assembly through existing repositories. Never flushes,
    never commits; the caller owns (and should roll back) the session."""
    record = get_candidate_profile(db)
    if record is None:
        raise ValueError("no candidate profile exists")
    profile = to_candidate_profile_response(record)
    package: ReviewPackage | None = None
    cv: TailoredCVDraft | None = None
    review = get_latest_approved_review_for_job(db, job.id)
    if review is not None and review.approved_revision_id is not None:
        revision = get_revision_by_id(db, review.approved_revision_id)
        if revision is not None:
            package = to_review_package(review, revision)
            cv_record = get_draft_by_id(db, review.cv_draft_id)
            cv = to_tailored_cv_draft(cv_record) if cv_record is not None else None
    if destination is not None and destination.job_id != job.id:
        raise ValueError("destination belongs to a different job")
    return SendReadinessInputs(
        job_id=job.id,
        job_location=job.location or None,
        profile_report=evaluate_profile_readiness(profile),
        outbound_sending_enabled=settings.outbound_sending_enabled,
        approved_package=package,
        approved_cv=cv,
        destination=destination,
        location_evidence=list(location_evidence),
    )


def render_send_readiness(result: SendReadinessResult) -> str:
    lines = [
        f"READY_FOR_SEND_PACKAGE = {'YES' if result.ready else 'NO'} (job_id={result.job_id})",
        f"EMAIL_SEND_READY = {'YES' if result.email_send_ready else 'NO'}",
    ]
    for check in result.checks:
        lines.append(f"[{'PASS' if check.passed else 'FAIL'}] {check.code}: {check.detail}")
    return "\n".join(lines)
