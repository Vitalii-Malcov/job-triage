"""AUD-007 (Astra R2): update_job_status must not let a stale concurrent
writer move a Job back out of a terminal state (or into any other invalid
state) via a read -> validate-in-Python -> mutate -> commit race. See
app.db.repositories.update_job_status and its
_ALLOWED_SOURCE_STATUSES-driven atomic UPDATE predicate.
"""

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import JobRecord
from app.db.repositories import update_job_status
from app.domain.status_transitions import InvalidStatusTransitionError
from app.models.application_status import ApplicationStatus as S


def _make_job_record(*, status: str = "NEW") -> JobRecord:
    return JobRecord(
        fingerprint=f"fp-{status}-{id(object())}",
        source="xing",
        title="Python Developer",
        company="Acme",
        location="Berlin",
        url="https://acme.example.com/jobs/AAA111",
        score=80,
        recommendation="APPLY",
        status=status,
    )


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'job_status_race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    return engine


def test_valid_transition_succeeds(db):
    job = _make_job_record(status="NEW")
    db.add(job)
    db.commit()

    updated = update_job_status(db, job.id, S.APPLIED)

    assert updated is not None
    assert updated.status == "APPLIED"


def test_returns_none_for_missing_job(db):
    assert update_job_status(db, 999999, S.APPLIED) is None


def test_invalid_transition_from_fresh_read_raises_and_leaves_status_unchanged(db):
    job = _make_job_record(status="REJECTED")
    db.add(job)
    db.commit()
    job_id = job.id

    with pytest.raises(InvalidStatusTransitionError):
        update_job_status(db, job_id, S.APPLIED)

    db.expire_all()
    assert db.get(JobRecord, job_id).status == "REJECTED"


def test_stale_session_cannot_resurrect_terminal_status(file_engine):
    """Session A reads the job while it is still APPLIED (a legal source
    for -> INTERVIEW). Before A writes, Session B independently commits a
    transition of the SAME row to the terminal REJECTED status. A's
    subsequent update_job_status call -- based entirely on its now-stale
    in-memory belief that the row is still APPLIED -- must fail, and the
    terminal REJECTED status committed by B must remain intact.
    """
    session_factory = sessionmaker(bind=file_engine)

    setup = session_factory()
    job = _make_job_record(status="APPLIED")
    setup.add(job)
    setup.commit()
    job_id = job.id
    setup.close()

    session_a = session_factory()
    session_b = session_factory()
    try:
        # Session A "reads old state" -- loads the row while it is still
        # APPLIED, establishing its own (soon to be stale) view.
        record_a = session_a.get(JobRecord, job_id)
        assert record_a.status == "APPLIED"

        # Session B independently commits the terminal transition.
        updated_b = update_job_status(session_b, job_id, S.REJECTED)
        assert updated_b.status == "REJECTED"

        # Session A now attempts its own transition based on the stale
        # APPLIED -> INTERVIEW belief. The CAS predicate must see the
        # row's REAL current status (REJECTED) and refuse.
        with pytest.raises(InvalidStatusTransitionError) as exc:
            update_job_status(session_a, job_id, S.INTERVIEW)
        assert exc.value.current == S.REJECTED

        verify = session_factory()
        try:
            assert verify.get(JobRecord, job_id).status == "REJECTED"
        finally:
            verify.close()
    finally:
        session_a.close()
        session_b.close()


def test_concurrent_thread_cannot_overwrite_terminal_status_committed_mid_call(file_engine):
    """Real OS-thread version of the same race, using two independent
    Sessions/connections (never one Session shared across threads): thread A
    reads the job while still APPLIED, then -- deterministically, via an
    Event handoff rather than timing luck -- is made to block AFTER its read
    but BEFORE its own CAS UPDATE executes, while thread B independently
    commits the terminal REJECTED transition on a fully separate connection.
    Thread A's stale CAS must then fail and REJECTED must remain intact.
    """
    session_factory = sessionmaker(bind=file_engine)

    setup = session_factory()
    job = _make_job_record(status="APPLIED")
    setup.add(job)
    setup.commit()
    job_id = job.id
    setup.close()

    a_has_read = threading.Event()
    b_has_committed = threading.Event()
    results: dict[str, object] = {}

    def worker_a() -> None:
        session = session_factory()
        try:
            record = session.get(JobRecord, job_id)
            assert record.status == "APPLIED"
            a_has_read.set()
            # Wait for B's independent, terminal commit before attempting
            # our own (by-then-stale) transition.
            assert b_has_committed.wait(timeout=5)
            try:
                results["a"] = update_job_status(session, job_id, S.INTERVIEW)
            except InvalidStatusTransitionError as exc:
                results["a"] = exc
        finally:
            session.close()

    def worker_b() -> None:
        assert a_has_read.wait(timeout=5)
        session = session_factory()
        try:
            results["b"] = update_job_status(session, job_id, S.REJECTED)
        finally:
            b_has_committed.set()
            session.close()

    thread_a = threading.Thread(target=worker_a)
    thread_b = threading.Thread(target=worker_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=20)
    thread_b.join(timeout=20)

    assert isinstance(results["b"], JobRecord)
    assert results["b"].status == "REJECTED"
    assert isinstance(results["a"], InvalidStatusTransitionError)
    assert results["a"].current == S.REJECTED

    verify = session_factory()
    try:
        assert verify.get(JobRecord, job_id).status == "REJECTED"
    finally:
        verify.close()
