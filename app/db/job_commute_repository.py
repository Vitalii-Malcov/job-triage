"""Stage 17C: verified car-route measurements per job (insert-only)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobCommuteCheckRecord, JobRecord

COMMUTE_METHODS = frozenset({"MANUAL", "OSRM", "MANUAL_APPROX", "OSRM_APPROX"})
# Night audit 2026-10-27: a route measured only to a city centre, postcode or
# street (not the building) is stored with an `_APPROX` method and never
# counts as the verified workplace route (no schema change: `method` carries
# it).
APPROXIMATE_METHODS = frozenset({"MANUAL_APPROX", "OSRM_APPROX"})
ROUTE_CACHE_MARKER = "dest="


def is_approximate(method: str | None) -> bool:
    return method in APPROXIMATE_METHODS


def destination_marker(lat: float, lon: float) -> str:
    """Stable text written into an OSRM check's source note so the same
    coordinate pair is never routed twice (see `find_cached_route`)."""
    return f"{ROUTE_CACHE_MARKER}{lat:.5f},{lon:.5f}"


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
    origin_lat: float | None = None,
    origin_lon: float | None = None,
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
        origin_lat=origin_lat,
        origin_lon=origin_lon,
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


def find_cached_route(
    db: Session, *, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
) -> JobCommuteCheckRecord | None:
    """A stored OSRM route between exactly these coordinates (any job), so a
    repeated check needs no network request."""
    marker = destination_marker(dest_lat, dest_lon)
    return db.scalars(
        select(JobCommuteCheckRecord)
        .where(
            JobCommuteCheckRecord.method.in_(("OSRM", "OSRM_APPROX")),
            JobCommuteCheckRecord.origin_lat == origin_lat,
            JobCommuteCheckRecord.origin_lon == origin_lon,
            JobCommuteCheckRecord.source_note.contains(marker),
        )
        .order_by(JobCommuteCheckRecord.checked_at.desc(), JobCommuteCheckRecord.id.desc())
        .limit(1)
    ).first()


def latest_commute_check(db: Session, job_id: int) -> JobCommuteCheckRecord | None:
    return db.scalars(
        select(JobCommuteCheckRecord)
        .where(JobCommuteCheckRecord.job_id == job_id)
        .order_by(JobCommuteCheckRecord.checked_at.desc(), JobCommuteCheckRecord.id.desc())
        .limit(1)
    ).first()
