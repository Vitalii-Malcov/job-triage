"""Stage 17C: verified car-route measurements per job (insert-only)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobCommuteCheckRecord, JobRecord

COMMUTE_METHODS = frozenset({"MANUAL", "OSRM"})


class CommuteCheckInputError(ValueError):
    """The measurement is not acceptable; nothing was written."""


def record_commute_check(
    db: Session,
    *,
    job_id: int,
    origin_label: str,
    destination_label: str,
    car_route_km: float,
    method: str,
    source_note: str,
    car_route_minutes: float | None = None,
    destination_region: str | None = None,
    now: datetime | None = None,
) -> JobCommuteCheckRecord:
    if db.get(JobRecord, job_id) is None:
        raise CommuteCheckInputError(f"job {job_id} does not exist")
    if method not in COMMUTE_METHODS:
        raise CommuteCheckInputError(f"method must be one of {sorted(COMMUTE_METHODS)}")
    if not (0 < car_route_km < 2000):
        raise CommuteCheckInputError("car_route_km must be a positive road distance")
    if car_route_minutes is not None and not (0 < car_route_minutes < 2000):
        raise CommuteCheckInputError("car_route_minutes is out of range")
    for name, value in (("origin", origin_label), ("destination", destination_label)):
        if not value or not value.strip():
            raise CommuteCheckInputError(f"{name} label is required")
    if not source_note or not source_note.strip():
        raise CommuteCheckInputError("source_note (how the route was measured) is required")
    record = JobCommuteCheckRecord(
        job_id=job_id,
        origin_label=origin_label.strip()[:300],
        destination_label=destination_label.strip()[:300],
        destination_region=(destination_region or None),
        car_route_km=float(car_route_km),
        car_route_minutes=car_route_minutes,
        method=method,
        source_note=source_note.strip(),
        checked_at=now or datetime.now(UTC),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def latest_commute_check(db: Session, job_id: int) -> JobCommuteCheckRecord | None:
    return db.scalars(
        select(JobCommuteCheckRecord)
        .where(JobCommuteCheckRecord.job_id == job_id)
        .order_by(JobCommuteCheckRecord.checked_at.desc(), JobCommuteCheckRecord.id.desc())
        .limit(1)
    ).first()
