"""Audit FLOW-02: job status and Stage 15 application status both count."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.models import Base, JobRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_tracker import ApplicationTracker
from app.services.follow_up import compute_fresh_follow_up_state

AT = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
NOW = AT + timedelta(days=30)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'status_models.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _job(db, job_id=1, status="NEW", ref="12345-678-S"):
    job = JobRecord(
        id=job_id,
        fingerprint=f"fp-{job_id}",
        source="bundesagentur",
        source_reference=ref,
        title="Backend Developer (m/w/d)",
        company="Muster GmbH",
        location="Hanau",
        url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
        description="Python, FastAPI",
        status=status,
        score=80,
        recommendation="APPLY",
        first_seen_at=AT,
        last_seen_at=AT,
    )
    db.add(job)
    db.commit()
    return job


def _application(db, job, final_status):
    tracker = ApplicationTracker()
    app = tracker.create_application(
        db,
        company_name=job.company,
        role_title=job.title,
        source=job.source,
        source_reference=job.source_reference,
        application_channel=C.EMPLOYER_PORTAL,
        initial_status=S.SUBMITTED,
        origin=O.OPERATOR,
        actor="test",
        occurred_at=AT,
        job_id=job.id,
        correlation_key=f"submission:{job.id}",
    ).application
    if final_status != S.SUBMITTED:
        tracker.transition_application(
            db, app.id, final_status, origin=O.OPERATOR, actor="test", reason="test"
        )
    db.commit()
    return app


# ---- follow-up ---------------------------------------------------------------


def _fresh(db, job):
    return compute_fresh_follow_up_state(
        db, "me@example.com", job, settings=Settings(_env_file=None), now=NOW
    )


@pytest.mark.parametrize(
    "outcome", [S.REJECTED, S.WITHDRAWN, S.INTERVIEW, S.TECHNICAL_INTERVIEW, S.OFFER]
)
def test_follow_up_is_blocked_after_a_confirmed_stage15_outcome(db, outcome):
    job = _job(db, status="APPLIED")  # JobRecord.status still says APPLIED
    app = _application(db, job, S.SUBMITTED)
    ApplicationTracker().transition_application(
        db, app.id, outcome, origin=O.OPERATOR, actor="test", reason="test"
    )
    db.commit()
    state = _fresh(db, job)
    assert state.eligibility.eligibility == "NOT_ELIGIBLE"
    assert str(outcome.value) in state.eligibility.reason


def test_follow_up_is_not_blocked_by_a_plain_submitted_application(db):
    job = _job(db, status="APPLIED")
    _application(db, job, S.SUBMITTED)
    state = _fresh(db, job)
    # no Gmail thread exists, so it is NOT_ELIGIBLE for the old reason, not the new one
    assert "No Stage 7B-matched" in state.eligibility.reason


def test_a_rejection_for_another_vacancy_of_the_same_employer_does_not_block(db):
    job_a = _job(db, job_id=1, status="APPLIED", ref="11111-1-S")
    _application(db, job_a, S.SUBMITTED)
    job_b = _job(db, job_id=2, ref="22222-2-S")  # same employer and role title, other vacancy
    job_b.url = "https://www.arbeitsagentur.de/jobsuche/jobdetail/22222-2-S"
    job_b.fingerprint = "fp-other"
    db.commit()
    app_b = _application(db, job_b, S.SUBMITTED)
    ApplicationTracker().transition_application(
        db, app_b.id, S.REJECTED, origin=O.OPERATOR, actor="test", reason="test"
    )
    db.commit()
    state = _fresh(db, job_a)
    assert "No Stage 7B-matched" in state.eligibility.reason  # the old reason, not the new one
    assert "REJECTED" not in state.eligibility.reason


def test_the_job_status_alone_still_does_not_change_the_guard(db):
    from app.services.application_guard import ensure_not_already_applied

    ensure_not_already_applied(db, _job(db, status="APPLIED"))  # unchanged, operator decision
