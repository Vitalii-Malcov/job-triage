import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobRecord, ProcessedEmailMessage, UserProfile
from app.domain.status_transitions import validate_transition
from app.models.application_status import ApplicationStatus
from app.models.job import Job, JobScore

DEFAULT_PROFILE_SKILLS = [
    "python",
    "fastapi",
    "flask",
    "mysql",
    "mongodb",
    "git",
    "pytest",
]

# Which Job fields feed the dedup fingerprint, per source. Default (used by
# every source not listed here, e.g. "bundesagentur" and manual
# /jobs/score calls) is unchanged from the original formula:
# source+company+title+url. This must stay exactly as-is for existing
# sources — it's the identity of every JobRecord already persisted.
#
# "xing" is the one deliberate exception: XING digest emails embed a
# per-recipient tracking redirect as the job's URL (see
# app/collectors/xing_email.py's module docstring), and that URL has been
# confirmed to differ across separate emails advertising the exact same
# real posting. Including it in the fingerprint would make the same
# real-world job dedup-unstable (new JobRecord created every time XING
# rotates the tracking URL) — the same failure class as the
# Bundesagentur title-fallback bug fixed earlier, just in a different
# field. location is used as a substitute distinguishing field instead.
_DEFAULT_FINGERPRINT_FIELDS: tuple[str, ...] = ("source", "company", "title", "url")
_FINGERPRINT_FIELDS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "xing": ("source", "company", "title", "location"),
}


def _fingerprint(job: Job) -> str:
    fields = _FINGERPRINT_FIELDS_BY_SOURCE.get(job.source, _DEFAULT_FINGERPRINT_FIELDS)
    values = []
    for field in fields:
        raw = getattr(job, field)
        text = str(raw).rstrip("/") if field == "url" else str(raw)
        values.append(text.strip().casefold())
    canonical = "|".join(values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_job_by_fingerprint(db: Session, job: Job) -> JobRecord | None:
    """Return the persisted record representing ``job``, if one exists."""
    return db.scalar(select(JobRecord).where(JobRecord.fingerprint == _fingerprint(job)))


def get_or_create_default_profile(db: Session) -> UserProfile:
    profile = db.scalar(select(UserProfile).where(UserProfile.name == "default"))
    if profile:
        return profile
    profile = UserProfile(name="default", skills_json=json.dumps(DEFAULT_PROFILE_SKILLS))
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def profile_skills(profile: UserProfile) -> set[str]:
    return set(json.loads(profile.skills_json))


def upsert_job(db: Session, job: Job, score: JobScore) -> tuple[JobRecord, bool]:
    fingerprint = _fingerprint(job)
    existing = get_job_by_fingerprint(db, job)
    now = datetime.now(UTC)
    if existing:
        existing.last_seen_at = now
        existing.score = score.score
        existing.recommendation = score.recommendation
        existing.skills_json = json.dumps(job.skills)
        existing.data_confidence = score.data_confidence
        existing.skill_source = job.skill_source
        existing.must_have_skills_json = json.dumps(job.must_have_skills)
        existing.nice_to_have_skills_json = json.dumps(job.nice_to_have_skills)
        if job.description.strip():
            existing.description = job.description
        db.commit()
        db.refresh(existing)
        return existing, False

    record = JobRecord(
        fingerprint=fingerprint,
        source=job.source,
        title=job.title,
        company=job.company,
        location=job.location,
        url=str(job.url),
        description=job.description,
        skills_json=json.dumps(job.skills),
        data_confidence=score.data_confidence,
        skill_source=job.skill_source,
        must_have_skills_json=json.dumps(job.must_have_skills),
        nice_to_have_skills_json=json.dumps(job.nice_to_have_skills),
        score=score.score,
        recommendation=score.recommendation,
        status="NEW",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record, True


def list_jobs(
    db: Session,
    status: ApplicationStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[JobRecord]:
    stmt = select(JobRecord).order_by(JobRecord.last_seen_at.desc())
    if status is not None:
        stmt = stmt.where(JobRecord.status == status.value)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt).all())


def get_job_by_id(db: Session, job_id: int) -> JobRecord | None:
    return db.get(JobRecord, job_id)


def update_job_status(db: Session, job_id: int, new_status: ApplicationStatus) -> JobRecord | None:
    """Update a job's status after validating the transition.

    Returns None if the job does not exist. Raises InvalidStatusTransitionError
    (from app.domain.status_transitions) if the transition is not allowed.
    """
    record = db.get(JobRecord, job_id)
    if record is None:
        return None

    current_status = ApplicationStatus(record.status)
    validate_transition(current_status, new_status)

    record.status = new_status.value
    db.commit()
    db.refresh(record)
    return record


def is_message_processed(db: Session, source: str, message_id: str) -> bool:
    """True if this source has already processed an email with this Message-ID.

    Used by email-based collectors (e.g. XingEmailCollector) to avoid
    re-parsing the same message on every fetch() without mutating the
    mailbox itself (no read/unread flag changes) — see
    app/collectors/xing_email.py.
    """
    return (
        db.scalar(
            select(ProcessedEmailMessage.id).where(
                ProcessedEmailMessage.source == source,
                ProcessedEmailMessage.message_id == message_id,
            )
        )
        is not None
    )


def mark_message_processed(db: Session, source: str, message_id: str) -> None:
    """Record that this source has processed an email with this Message-ID.

    Idempotent: safe to call even if already marked (e.g. a duplicate
    Message-ID seen twice in the same run), so it never raises a unique
    constraint violation.
    """
    if is_message_processed(db, source, message_id):
        return
    db.add(ProcessedEmailMessage(source=source, message_id=message_id))
    db.commit()
