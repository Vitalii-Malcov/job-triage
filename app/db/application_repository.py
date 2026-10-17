"""Stage 15: persistence primitives for applications and their events.

Deliberately thin: no business rules and no commits. The transaction and
the lifecycle policy live in app/services/application_tracker.py, which
calls `insert_event` and `cas_update_application` inside ONE transaction so
an event and the status change it explains are committed together or not
at all.

There is no function that updates or deletes an `ApplicationEventRecord`
(and the ORM refuses such a flush -- see the model's guards).
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.datetime_utils import ensure_utc
from app.db.models import ApplicationEventRecord, ApplicationRecord
from app.models.application_lifecycle import (
    Application,
    ApplicationChannel,
    ApplicationEvent,
    ApplicationEventOrigin,
    ApplicationEventType,
    ApplicationLifecycleStatus,
)


def _utc(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


def to_application(record: ApplicationRecord) -> Application:
    return Application(
        id=record.id,
        candidate_profile_id=record.candidate_profile_id,
        job_id=record.job_id,
        company_name=record.company_name,
        role_title=record.role_title,
        location=record.location,
        source=record.source,
        source_reference=record.source_reference,
        external_job_id=record.external_job_id,
        vacancy_fingerprint=record.vacancy_fingerprint,
        application_channel=ApplicationChannel(record.application_channel),
        destination_url=record.destination_url,
        employer_domains=tuple(json.loads(record.employer_domains_json or "[]")),
        review_package_id=record.review_package_id,
        current_status=ApplicationLifecycleStatus(record.current_status),
        submitted_at=_utc(record.submitted_at),
        received_at=_utc(record.received_at),
        last_status_changed_at=ensure_utc(record.last_status_changed_at),
        last_employer_contact_at=_utc(record.last_employer_contact_at),
        next_followup_at=_utc(record.next_followup_at),
        followup_required=record.followup_required,
        version=record.version,
        created_at=ensure_utc(record.created_at),
        updated_at=ensure_utc(record.updated_at),
    )


def to_application_event(record: ApplicationEventRecord) -> ApplicationEvent:
    return ApplicationEvent(
        id=record.id,
        application_id=record.application_id,
        sequence=record.sequence,
        event_type=ApplicationEventType(record.event_type),
        old_status=ApplicationLifecycleStatus(record.old_status) if record.old_status else None,
        new_status=ApplicationLifecycleStatus(record.new_status) if record.new_status else None,
        origin=ApplicationEventOrigin(record.origin),
        actor=record.actor,
        reason=record.reason,
        source_reference=record.source_reference,
        occurred_at=_utc(record.occurred_at),
        recorded_at=ensure_utc(record.recorded_at),
        evidence_hash=record.evidence_hash,
        evidence_summary=record.evidence_summary,
        correlation_key=record.correlation_key,
        metadata=json.loads(record.metadata_json or "{}"),
    )


def get_application(db: Session, application_id: int) -> ApplicationRecord | None:
    return db.get(ApplicationRecord, application_id)


def get_application_by_source_reference(
    db: Session, source: str, source_reference: str
) -> ApplicationRecord | None:
    return db.scalars(
        select(ApplicationRecord).where(
            ApplicationRecord.source == source,
            ApplicationRecord.source_reference == source_reference,
        )
    ).one_or_none()


def list_applications(
    db: Session,
    *,
    statuses: tuple[ApplicationLifecycleStatus, ...] | None = None,
    limit: int = 200,
) -> list[ApplicationRecord]:
    query = select(ApplicationRecord).order_by(ApplicationRecord.id)
    if statuses:
        query = query.where(ApplicationRecord.current_status.in_([s.value for s in statuses]))
    return list(db.scalars(query.limit(limit)))


def list_applications_for_company(db: Session, company_name: str) -> list[ApplicationRecord]:
    return list(
        db.scalars(
            select(ApplicationRecord)
            .where(func.lower(ApplicationRecord.company_name) == company_name.casefold())
            .order_by(ApplicationRecord.id)
        )
    )


def list_events(db: Session, application_id: int) -> list[ApplicationEventRecord]:
    return list(
        db.scalars(
            select(ApplicationEventRecord)
            .where(ApplicationEventRecord.application_id == application_id)
            .order_by(ApplicationEventRecord.sequence)
        )
    )


def list_events_with_correlation_prefix(db: Session, prefix: str) -> list[ApplicationEventRecord]:
    return list(
        db.scalars(
            select(ApplicationEventRecord).where(
                ApplicationEventRecord.correlation_key.startswith(prefix, autoescape=True)
            )
        )
    )


def get_event_by_correlation_key(
    db: Session, correlation_key: str
) -> ApplicationEventRecord | None:
    return db.scalars(
        select(ApplicationEventRecord).where(
            ApplicationEventRecord.correlation_key == correlation_key
        )
    ).one_or_none()


def next_event_sequence(db: Session, application_id: int) -> int:
    current = db.scalar(
        select(func.max(ApplicationEventRecord.sequence)).where(
            ApplicationEventRecord.application_id == application_id
        )
    )
    return (current or 0) + 1


def insert_application(db: Session, **values: Any) -> ApplicationRecord:
    record = ApplicationRecord(**values)
    db.add(record)
    db.flush()
    return record


def insert_event(db: Session, **values: Any) -> ApplicationEventRecord:
    metadata = values.pop("metadata", None) or {}
    record = ApplicationEventRecord(
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True), **values
    )
    db.add(record)
    db.flush()
    return record


def cas_update_application(
    db: Session,
    application_id: int,
    *,
    expected_status: str,
    expected_version: int,
    values: dict[str, Any],
) -> bool:
    """UPDATE the row only if it still has the status and version the
    caller decided on; bumps `version`. False means another writer got
    there first (the caller must roll back)."""
    result = db.execute(
        update(ApplicationRecord)
        .where(
            ApplicationRecord.id == application_id,
            ApplicationRecord.current_status == expected_status,
            ApplicationRecord.version == expected_version,
        )
        .values(version=expected_version + 1, **values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
