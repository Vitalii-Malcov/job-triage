"""Stage 55: automatic preparation must not re-prepare a vacancy that already has an application.

Two different things are kept apart here:

* technical re-processing (a second run, a retry, a restart over the same vacancy) -- allowed when
  it is idempotent: it reuses the existing CV / Bewerbung draft and never creates a second
  application;
* a second real Bewerbung -- never automatic. A vacancy with an application that was already sent
  is NOT_SUITABLE; a vacancy with a prepared-but-unsent application (`duplicate = IN_PREPARATION`)
  is the operator's controlled work and the automatic path must leave it alone.

The fit assessment is the REAL `duplicate_criterion` over a real application row; only the other
criteria are fixed (a good, confirmed full-time remote match), so the duplicate state alone decides.
"""

import asyncio
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import ApplicationRecord, BewerbungDraftRecord, CandidateCVDraftRecord, JobRecord
from app.models.application_lifecycle import (
    ApplicationChannel,
    ApplicationEventOrigin,
)
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_tracker import ApplicationTracker
from app.services.automation_shortlist import prepare_shortlist_drafts
from app.services.job_fit_assessment import Criterion, duplicate_criterion
from tests.test_automation_shortlist import (
    TIER_60,
    _seed_job,
    _settings,
    _touch,
)
from tests.test_remote_work_eligibility import _assessment


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'idem.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _real_duplicate_criterion_over_a_fixed_good_match(monkeypatch):
    """The gate runs for real; only the assessment it receives is fixed apart from `duplicate`."""

    def assess(session, job_id):
        job = session.get(JobRecord, job_id)
        a = _assessment("Vollzeit, 100% Remote.", title="Junior Python Developer (m/w/d)")
        a.job_id, a.company, a.title = job_id, job.company, job.title
        a.criteria = [c for c in a.criteria if c.name != "duplicate"]
        a.criteria.append(duplicate_criterion(session, job))
        return a

    monkeypatch.setattr("app.services.shortlist_gate.assess_job", assess)


def _apply(db, job: JobRecord, status: S, *, reference: str | None = None) -> int:
    tracker = ApplicationTracker()
    initial = status if status not in {S.SUBMITTED, S.RECEIVED, S.REJECTED} else S.DISCOVERED
    outcome = tracker.create_application(
        db,
        company_name=job.company,
        role_title=job.title,
        source=job.source,
        source_reference=reference or f"ref-{job.id}",
        application_channel=ApplicationChannel.EMAIL,
        initial_status=initial,
        origin=ApplicationEventOrigin.OPERATOR,
        actor="test",
        job_id=job.id,
        vacancy_fingerprint=job.fingerprint,
    )
    app_id = outcome.application.id
    if initial != status:
        record = db.get(ApplicationRecord, app_id)
        record.current_status = status.value  # direct: this test is about the gate, not transitions
        db.commit()
    return app_id


def _run(db, jobs):
    return asyncio.run(
        prepare_shortlist_drafts(db, touched_jobs=[_touch(j) for j in jobs], settings=_settings())
    )


def _drafts(db):
    return db.query(CandidateCVDraftRecord).count(), db.query(BewerbungDraftRecord).count()


def test_a_vacancy_without_an_application_is_prepared(session_factory):
    db = session_factory()
    try:
        job = _seed_job(db, tier=TIER_60)
        _run(db, [job])
        assert _drafts(db) == (1, 1)
    finally:
        db.close()


@pytest.mark.parametrize("status", [S.DISCOVERED, S.MATCHED, S.PACKAGE_READY, S.APPROVED])
def test_an_unsent_application_for_the_vacancy_holds_automatic_preparation(session_factory, status):
    db = session_factory()
    try:
        job = _seed_job(db, tier=TIER_60)
        _apply(db, job, status)
        result = _run(db, [job])
        assert _drafts(db) == (0, 0), "automatic preparation re-prepared a vacancy in preparation"
        assert result["counters"].get("blocked_by_fit", 0) >= 1
    finally:
        db.close()


@pytest.mark.parametrize("status", [S.SUBMITTED, S.RECEIVED, S.REJECTED])
def test_a_sent_application_for_the_vacancy_is_never_prepared_again(session_factory, status):
    db = session_factory()
    try:
        job = _seed_job(db, tier=TIER_60)
        _apply(db, job, status)
        _run(db, [job])
        assert _drafts(db) == (0, 0)
    finally:
        db.close()


def test_another_vacancy_of_the_same_employer_is_not_held(session_factory):
    db = session_factory()
    try:
        applied = _seed_job(db, tier=TIER_60, title="Backend Entwickler (m/w/d)")
        other = _seed_job(db, tier=TIER_60, title="Python Developer (m/w/d)")
        _apply(db, applied, S.PACKAGE_READY)
        _run(db, [other])
        assert _drafts(db) == (1, 1)
    finally:
        db.close()


def test_the_same_vacancy_under_a_second_job_row_is_held_by_its_source_reference(session_factory):
    db = session_factory()
    try:
        first = _seed_job(db, tier=TIER_60)
        second = _seed_job(
            db,
            tier=TIER_60,
            source_reference="shared-ref",
            url="https://careers.example.com/other-url",
        )
        _apply(db, first, S.PACKAGE_READY, reference="shared-ref")
        _run(db, [second])
        assert _drafts(db) == (0, 0)
    finally:
        db.close()


def test_a_repeated_run_reuses_the_drafts_and_creates_nothing_new(session_factory):
    db = session_factory()
    try:
        job = _seed_job(db, tier=TIER_60)
        _run(db, [job])
        before = _drafts(db)
        second = _run(db, [job])
        assert _drafts(db) == before == (1, 1)
        assert second["counters"]["bewerbung_created"] == 0
    finally:
        db.close()


def test_two_parallel_runs_leave_one_cv_and_never_an_application(
    session_factory,
):
    """Two workers pass the same checks at the same time (the automation lease normally prevents
    this; a manual API call is the realistic second worker)."""
    seed = session_factory()
    job = _seed_job(seed, tier=TIER_60)
    job_id = job.id
    seed.close()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        db = session_factory()
        try:
            touched = [_touch(db.get(JobRecord, job_id))]
            barrier.wait(timeout=10)
            asyncio.run(prepare_shortlist_drafts(db, touched_jobs=touched, settings=_settings()))
        except BaseException as exc:  # noqa: BLE001 -- the test reports it
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    check = session_factory()
    try:
        cv, letters = _drafts(check)
        applications = check.query(ApplicationRecord).count()
    finally:
        check.close()
    assert not errors, errors
    assert cv == 1
    assert applications == 0  # preparation never creates an application
    assert letters in (1, 2)  # see the deterministic variant below


def test_the_duplicate_criterion_is_part_of_the_automatic_gate():
    from app.services.shortlist_gate import (
        BLOCK_APPLICATION_IN_PROGRESS,
        automatic_block_reason,
    )

    def gate(duplicate: str | None):
        a = _assessment("Vollzeit, 100% Remote.")
        a.criteria = [c for c in a.criteria if c.name != "duplicate"]
        if duplicate is not None:
            a.criteria.append(Criterion("duplicate", duplicate, "x"))
        return automatic_block_reason(a)

    assert gate("CLEAR") is None
    assert gate("IN_PREPARATION") == BLOCK_APPLICATION_IN_PROGRESS
    assert gate("SOMETHING_NEW") == BLOCK_APPLICATION_IN_PROGRESS  # unknown value: fail closed
    assert gate(None) == BLOCK_APPLICATION_IN_PROGRESS  # criterion missing: fail closed
    assert gate("POSSIBLE_DUPLICATE") == "FIT_TIER_NEEDS_OPERATOR"
    assert gate("ALREADY_APPLIED") == "FIT_TIER_NOT_SUITABLE"


def test_two_workers_that_saw_no_letter_leave_only_local_drafts(session_factory, monkeypatch):
    """Deterministic version of the race: both workers read "no letter yet" before either writes."""
    import app.services.automation_shortlist as shortlist

    seed = session_factory()
    job_id = _seed_job(seed, tier=TIER_60).id
    seed.close()
    barrier = threading.Barrier(2)
    original = shortlist.get_latest_bewerbung_draft

    def synced(db, jid):
        found = original(db, jid)
        try:
            barrier.wait(timeout=5)  # both have looked; neither has written
        except threading.BrokenBarrierError:
            pass
        return found

    monkeypatch.setattr(shortlist, "get_latest_bewerbung_draft", synced)
    errors: list[BaseException] = []

    def worker():
        db = session_factory()
        try:
            touched = [_touch(db.get(JobRecord, job_id))]
            asyncio.run(prepare_shortlist_drafts(db, touched_jobs=touched, settings=_settings()))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    check = session_factory()
    try:
        letters = check.query(BewerbungDraftRecord).filter_by(job_id=job_id).count()
        applications = check.query(ApplicationRecord).count()
    finally:
        check.close()
    assert not errors, errors
    # KNOWN, ACCEPTED (docs/APPLICATION_IDEMPOTENCY_AUDIT.md): `bewerbung_drafts` has no unique
    # identity on purpose (regenerating a letter always inserts a new row), so two workers that
    # both saw "no letter" can each insert one. A letter draft is local text: neither an
    # application nor a send. What must hold: the race creates no application, and the next run
    # reuses the newest draft instead of adding a third.
    assert applications == 0
    assert letters in (1, 2)
    again = session_factory()
    try:
        asyncio.run(
            prepare_shortlist_drafts(
                again,
                touched_jobs=[_touch(again.get(JobRecord, job_id))],
                settings=_settings(),
            )
        )
        assert again.query(BewerbungDraftRecord).filter_by(job_id=job_id).count() == letters
        assert again.query(ApplicationRecord).count() == 0
    finally:
        again.close()


# --- the application itself: created only by an operator, and only once per vacancy ------------


def _create(db, reference: str | None, *, role: str = "Backend Entwickler"):
    return ApplicationTracker().create_application(
        db,
        company_name="Example GmbH",
        role_title=role,
        source="bundesagentur",
        source_reference=reference,
        application_channel=ApplicationChannel.EMAIL,
        initial_status=S.DISCOVERED,
        origin=ApplicationEventOrigin.OPERATOR,
        actor="test",
    )


def test_creating_the_same_application_twice_returns_the_existing_one(session_factory):
    db = session_factory()
    try:
        first = _create(db, "ref-1")
        second = _create(db, "ref-1")
        assert first.duplicate is False and second.duplicate is True
        assert second.application.id == first.application.id
        assert db.query(ApplicationRecord).count() == 1
    finally:
        db.close()


def test_parallel_creation_of_one_application_yields_exactly_one_row(session_factory):
    barrier = threading.Barrier(4)
    outcomes: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        db = session_factory()
        try:
            barrier.wait(timeout=10)
            result = _create(db, "ref-parallel")
            with lock:
                outcomes.append(result.duplicate)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    check = session_factory()
    try:
        assert check.query(ApplicationRecord).count() == 1
    finally:
        check.close()
    assert not errors, errors
    assert sorted(outcomes) == [False, True, True, True]


def test_an_application_never_starts_in_a_terminal_status(session_factory):
    from app.services.application_tracker import TerminalStatusNotPermittedError

    db = session_factory()
    try:
        with pytest.raises(TerminalStatusNotPermittedError):
            ApplicationTracker().create_application(
                db,
                company_name="X",
                role_title="Y",
                source="bundesagentur",
                source_reference="ref-t",
                application_channel=ApplicationChannel.EMAIL,
                initial_status=S.REJECTED,
                origin=ApplicationEventOrigin.OPERATOR,
                actor="test",
            )
    finally:
        db.close()


# --- Codex round 1 (Stage 55): identity spelling, NULL reference, unverified vacancy, manual CV ---


def test_source_spelling_is_one_identity(session_factory):
    db = session_factory()
    try:
        first = ApplicationTracker().create_application(
            db,
            company_name="Example GmbH",
            role_title="Backend",
            source="Bundesagentur",
            source_reference="12345-42-S",
            application_channel=ApplicationChannel.EMAIL,
            initial_status=S.DISCOVERED,
            origin=ApplicationEventOrigin.OPERATOR,
            actor="test",
        )
        second = _create(db, "12345-42-S")  # "bundesagentur"
        assert second.duplicate is True
        assert second.application.id == first.application.id
        assert db.query(ApplicationRecord).count() == 1
        assert db.query(ApplicationRecord).one().source == "bundesagentur"
        found = ApplicationTracker().find_by_source_reference(db, " BUNDESAGENTUR ", "12345-42-S")
        assert found is not None and found.id == first.application.id
    finally:
        db.close()


def test_without_a_reference_the_job_row_is_the_identity(session_factory):
    db = session_factory()
    try:
        job_a = _seed_job(db, tier=TIER_60)
        job_b = _seed_job(db, tier=TIER_60)

        def create(job_id):
            return ApplicationTracker().create_application(
                db,
                company_name="Example GmbH",
                role_title="Backend",
                source="bundesagentur",
                source_reference=None,
                application_channel=ApplicationChannel.EMAIL,
                initial_status=S.DISCOVERED,
                origin=ApplicationEventOrigin.OPERATOR,
                actor="test",
                job_id=job_id,
            )

        first, again, other = create(job_a.id), create(job_a.id), create(job_b.id)
        assert again.duplicate is True and again.application.id == first.application.id
        assert other.duplicate is False and other.application.id != first.application.id
        assert db.query(ApplicationRecord).count() == 2
    finally:
        db.close()


def test_a_sent_application_blocks_the_manual_cv_endpoint_too(session_factory):
    from app.services.application_guard import AlreadyAppliedError
    from app.services.candidate_preparation import prepare_candidate_cv_draft

    db = session_factory()
    try:
        job = _seed_job(db, tier=TIER_60)
        _apply(db, job, S.SUBMITTED)
        with pytest.raises(AlreadyAppliedError):
            prepare_candidate_cv_draft(db, job.id, 1, force_recompute=False)
        assert db.query(CandidateCVDraftRecord).count() == 0
    finally:
        db.close()


def test_a_job_without_an_application_is_not_stopped_by_the_manual_cv_guard(session_factory):
    from app.agents.cv_adapter import CVDraftMatchNotFoundError
    from app.services.candidate_preparation import prepare_candidate_cv_draft

    db = session_factory()
    try:
        job = _seed_job(db, tier=TIER_60)
        with pytest.raises(CVDraftMatchNotFoundError):  # got past the guard, no such match
            prepare_candidate_cv_draft(db, job.id, 999, force_recompute=False)
    finally:
        db.close()


def test_telegram_approval_needs_a_verified_vacancy(tmp_path):
    """An application with no job row would approve whatever package it points to."""
    from sqlalchemy.pool import NullPool

    from app.db.session_engine import enable_sqlite_foreign_keys
    from app.services.application_tracker import ApplicationTracker as Tracker
    from app.services.telegram_operator_control import assess_package
    from tests.stage16_fixtures import build_target

    engine = create_engine(
        f"sqlite:///{tmp_path / 'tg.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    db = sessionmaker(engine)()
    try:
        target = build_target(db)
        summary, _ = assess_package(db, Tracker().get_application(db, target.application_id))
        assert summary.ready, summary.reasons

        record = db.get(ApplicationRecord, target.application_id)
        record.job_id = None
        db.commit()
        summary, _ = assess_package(db, Tracker().get_application(db, target.application_id))
        assert not summary.ready
        assert "NO_VERIFIED_VACANCY" in summary.reasons
    finally:
        db.close()
        engine.dispose()
