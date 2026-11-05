"""Review 2.3 on real PostgreSQL: concurrent collector entry points exclude each
other through the shared RUNNING slot (partial unique index), and exactly one
contender wins while the winner still holds it.

Runs at head (no Alembic cycling). Skipped unless `TEST_POSTGRES_URL` is set
(CI's zero-skip gate sets it); run `alembic upgrade head` against that database
first. Test-owned rows (account key below) are deleted before and after.
"""

import asyncio
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.automation_repository import create_running_run, finish_run
from app.db.models import (
    AutomationRunRecord,
    JobRecord,
    ProcessedEmailMessage,
    XingScanProgressRecord,
)
from app.services import automation
from app.services.automation import AutomationRunAlreadyInProgressError, run_automation_cycle
from app.services.collector_exclusion import (
    CollectorRunInProgressError,
    hold_collector_slots,
    run_collector_exclusively,
)

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL not set")

ACCOUNT = "r23-exclusion-test@example.com"
SCHEDULER_KEY = "R23-Scheduler-Test@Example.com"  # a different, case-preserved spelling
STATS = {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}


@pytest.fixture()
def factory():
    engine = create_engine(POSTGRES_URL)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def cleanup():
        with session_factory() as db:
            db.execute(
                delete(AutomationRunRecord).where(
                    AutomationRunRecord.account_key.in_([ACCOUNT, SCHEDULER_KEY])
                )
            )
            db.commit()

    cleanup()
    try:
        yield session_factory
    finally:
        cleanup()
        engine.dispose()


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        **overrides,
        gmail_username=ACCOUNT,
        xing_mailbox_username="xing@example.com",
        xing_mailbox_app_password="pw",
        bundesagentur_api_key="ba",
    )


def test_exactly_one_of_many_concurrent_manual_runs_wins(factory):
    holding = threading.Event()  # the winner is inside its run
    release = threading.Event()  # let the winner finish
    barrier = threading.Barrier(6, timeout=30)
    outcomes: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    async def hold(db, settings, *, is_lease_lost=None):
        holding.set()
        while not release.is_set():
            await asyncio.sleep(0.05)
        return STATS

    def contender():
        try:
            with factory() as db:
                barrier.wait()
                try:
                    asyncio.run(run_collector_exclusively(db, _settings(), "xing", hold))
                    result = "won"
                except CollectorRunInProgressError:
                    result = "refused"
            with guard:
                outcomes.append(result)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=contender) for _ in range(6)]
    for t in threads:
        t.start()
    assert holding.wait(timeout=30)
    # while the winner still holds the slot, every other contender is refused
    deadline_threads = [t for t in threads]
    for t in deadline_threads:
        t.join(timeout=0.5)
    with guard:
        refused_while_held = outcomes.count("refused")
    release.set()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert sorted(outcomes) == ["refused"] * 5 + ["won"]
    assert refused_while_held == 5
    with factory() as db:
        rows = db.scalars(
            select(AutomationRunRecord).where(AutomationRunRecord.account_key == ACCOUNT)
        ).all()
        assert [r.status for r in rows if r.status == "RUNNING"] == []
        assert sum(1 for r in rows if r.status == "COMPLETED") == 1


def test_slot_is_free_again_after_the_winner_finishes(factory):
    async def quick(db, settings, *, is_lease_lost=None):
        return STATS

    for _ in range(2):
        with factory() as db:
            assert asyncio.run(run_collector_exclusively(db, _settings(), "bundesagentur", quick))


class _Overlap:
    """Counts how many collector bodies run at the same moment."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def enter(self):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self):
        with self.guard:
            self.active -= 1


def _patch_steps(monkeypatch, overlap, hold_seconds=0.4):
    async def step(db, settings, *, touched_jobs=None, is_lease_lost=None):
        overlap.enter()
        try:
            await asyncio.sleep(hold_seconds)
        finally:
            overlap.leave()
        return dict(STATS)

    monkeypatch.setattr(automation, "run_bundesagentur", step)
    monkeypatch.setattr(automation, "run_xing", step)


def _statuses(factory):
    with factory() as db:
        return [
            r.status
            for r in db.scalars(
                select(AutomationRunRecord).where(
                    AutomationRunRecord.account_key.in_([ACCOUNT, SCHEDULER_KEY])
                )
            )
        ]


def test_two_automation_cycles_under_both_key_spellings_never_run_together(factory, monkeypatch):
    """REST keys a cycle by the normalized Gmail identity, the scheduler by its configured
    spelling. Claim-then-verify allows BOTH to yield when they race (no fairness promise), but
    never lets both run; after the race a cycle must make progress. Several rounds so the
    interleavings differ."""
    overlap = _Overlap()
    _patch_steps(monkeypatch, overlap, hold_seconds=0.25)
    settings = _settings(automation_scheduler_account_key=SCHEDULER_KEY)
    results: list[str] = []
    errors: list[BaseException] = []

    for _round in range(6):
        barrier = threading.Barrier(2, timeout=30)

        def cycle(key, barrier=barrier):
            try:
                with factory() as db:
                    barrier.wait()
                    try:
                        asyncio.run(run_automation_cycle(db, account_key=key, settings=settings))
                        results.append("ran")
                    except AutomationRunAlreadyInProgressError:
                        results.append("refused")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=cycle, args=(k,)) for k in (ACCOUNT, SCHEDULER_KEY)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not any(thread.is_alive() for thread in threads)
        assert "RUNNING" not in _statuses(factory)
    assert errors == []
    assert overlap.max_active <= 1  # never both
    assert len(results) == 12
    # progress: with nothing else running a cycle always runs
    with factory() as db:
        asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=settings))
    assert overlap.max_active == 1


def test_a_manual_run_and_an_automation_cycle_under_the_other_key_never_overlap(
    factory, monkeypatch
):
    overlap = _Overlap()
    _patch_steps(monkeypatch, overlap)
    settings = _settings(automation_scheduler_account_key=SCHEDULER_KEY)

    async def body(db, s, *, is_lease_lost=None):
        overlap.enter()
        try:
            await asyncio.sleep(0.4)
        finally:
            overlap.leave()
        return dict(STATS)

    barrier = threading.Barrier(4, timeout=30)
    errors: list[BaseException] = []

    def manual():
        try:
            with factory() as db:
                barrier.wait()
                try:
                    asyncio.run(run_collector_exclusively(db, settings, "xing", body))
                except CollectorRunInProgressError:
                    pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def cycle(key):
        try:
            with factory() as db:
                barrier.wait()
                try:
                    asyncio.run(run_automation_cycle(db, account_key=key, settings=settings))
                except AutomationRunAlreadyInProgressError:
                    pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=manual),
        threading.Thread(target=manual),
        threading.Thread(target=cycle, args=(ACCOUNT,)),
        threading.Thread(target=cycle, args=(SCHEDULER_KEY,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    # a manual run holding the first key and a cycle holding the second may both yield
    # (no fairness promise); what must never happen is two bodies at once
    assert overlap.max_active <= 1
    assert "RUNNING" not in _statuses(factory)
    with factory() as db:  # progress once nothing contends
        asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=settings))
    assert overlap.max_active == 1


def test_a_crashed_holders_expired_lease_is_reclaimed_and_the_old_holder_cannot_finalize(factory):
    with factory() as db:
        old, _ = create_running_run(db, account_key=ACCOUNT, holder="crashed:1")
        old_id = old.id
        db.execute(
            update(AutomationRunRecord)
            .where(AutomationRunRecord.id == old_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        db.commit()

    async def quick(db, settings, *, is_lease_lost=None):
        return dict(STATS)

    with factory() as db:
        assert asyncio.run(run_collector_exclusively(db, _settings(), "xing", quick))
    with factory() as db:
        stale = db.get(AutomationRunRecord, old_id)
        assert stale.status == "FAILED"  # reconciled, audited
        # the crashed holder coming back cannot rewrite its already-reconciled run
        assert (
            finish_run(
                db, stale, holder="crashed:1", status="COMPLETED", results={}, error_summary=None
            )
            is None
        )


def test_a_cli_style_holder_excludes_manual_runs_and_frees_the_slot(factory):
    inside = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def cli_holder():
        try:
            with factory() as db:
                with hold_collector_slots(db, _settings(), "ba_refresh") as hold:
                    assert not hold.lease_lost()
                    inside.set()
                    assert release.wait(timeout=30)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=cli_holder)
    thread.start()
    assert inside.wait(timeout=30)

    async def quick(db, settings, *, is_lease_lost=None):
        return dict(STATS)

    with factory() as db, pytest.raises(CollectorRunInProgressError):
        asyncio.run(run_collector_exclusively(db, _settings(), "bundesagentur", quick))
    # a concurrent second CLI-style holder is refused too
    with factory() as db, pytest.raises(CollectorRunInProgressError):
        with hold_collector_slots(db, _settings(), "ba_refresh"):
            pass
    release.set()
    thread.join(timeout=30)
    assert errors == []
    time.sleep(0.05)
    with factory() as db:
        assert asyncio.run(run_collector_exclusively(db, _settings(), "bundesagentur", quick))
    assert "RUNNING" not in _statuses(factory)


XING_MARKER = "r22-imap-replay"
XING_MESSAGE_ID = f"<{XING_MARKER}@mail.xing.com>"


class _CountingNotifier:
    """No network: records the jobs a notification would have been sent for."""

    sent: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def send_job(self, job, score):
        _CountingNotifier.sent.append(job.title)
        return True


@pytest.fixture()
def xing_replay(factory, monkeypatch):
    from app.collectors.xing_email import XingEmailBatch
    from app.models.job import Job, JobScore

    def cleanup():
        with factory() as db:
            db.execute(delete(JobRecord).where(JobRecord.title.like(f"%{XING_MARKER}%")))
            db.execute(
                delete(ProcessedEmailMessage).where(
                    ProcessedEmailMessage.message_id == XING_MESSAGE_ID
                )
            )
            db.execute(
                delete(XingScanProgressRecord).where(XingScanProgressRecord.source == "xing")
            )
            db.commit()

    class _Collector:
        def __init__(self, **kwargs):
            self.skipped_invalid_count = 0
            self.deadline_exceeded = False
            self.uid_validity = 7_000_123
            self.confirmed_uids: list[int] = []
            self.candidate_uids: list[int] = [1]

        async def fetch_message_batches(self, since=None):
            job = Job(
                source="xing",
                title=f"Sachbearbeiter {XING_MARKER}",
                company="Replay Co",
                location="Leipzig",
                url="https://www.xing.com/m/RRRRRRRRRRRRRRRRRRRR1",
                description="Gehalt: 50.000 EUR",
            )
            return [XingEmailBatch(message_id=XING_MESSAGE_ID, jobs=(job,), uid=1)]

    class _Scorer:
        def __init__(self, profile_skills):
            pass

        def score(self, job):
            return JobScore(score=10, recommendation="SKIP", data_confidence=0.5)

    monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _Collector)
    monkeypatch.setattr("app.agents.job_score_evaluator.JobScorer", _Scorer)
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _CountingNotifier)
    _CountingNotifier.sent = []
    cleanup()
    try:
        yield factory
    finally:
        cleanup()


def _imap_state(factory):
    from app.db.repositories import is_message_processed

    with factory() as db:
        rows = db.scalars(select(JobRecord).where(JobRecord.title.like(f"%{XING_MARKER}%"))).all()
        progress = db.scalars(
            select(XingScanProgressRecord).where(XingScanProgressRecord.source == "xing")
        ).all()
        watermark = (progress[0].uid_validity, progress[0].confirmed_upto_uid) if progress else None
        return len(rows), is_message_processed(db, "xing", XING_MESSAGE_ID), watermark


def test_a_lost_lease_leaves_the_message_and_watermark_to_the_new_owner_who_replays_it(
    xing_replay,
):
    """Real PostgreSQL: a run that already lost its lease starts no new vacancy write,
    acknowledges nothing and moves no watermark; the next owner re-reads the SAME message,
    acknowledges it, advances the watermark once, and the vacancy exists exactly once."""
    from app.services.collector_runner import run_xing

    with xing_replay() as db:
        asyncio.run(run_xing(db, _settings(), is_lease_lost=lambda: True))
    assert _imap_state(xing_replay) == (0, False, None)

    with xing_replay() as db:
        asyncio.run(run_xing(db, _settings(), is_lease_lost=lambda: False))
    assert _imap_state(xing_replay) == (1, True, (7_000_123, 1))

    with xing_replay() as db:  # a third pass changes nothing
        asyncio.run(run_xing(db, _settings(), is_lease_lost=lambda: False))
    assert _imap_state(xing_replay) == (1, True, (7_000_123, 1))


def test_concurrent_replays_of_one_message_acknowledge_it_once_without_duplicates(xing_replay):
    from app.services.collector_runner import run_xing

    barrier = threading.Barrier(4, timeout=30)
    errors: list[BaseException] = []

    def replay():
        try:
            with xing_replay() as db:
                barrier.wait()
                asyncio.run(run_xing(db, _settings(), is_lease_lost=lambda: False))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=replay) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert _imap_state(xing_replay) == (1, True, (7_000_123, 1))


def test_a_real_heartbeat_loss_leaves_the_imap_state_to_the_takeover_run(xing_replay, monkeypatch):
    """Real PostgreSQL with the REAL heartbeat: another process steals the lease mid-run, the
    heartbeat notices, the run reports the loss (never COMPLETED), starts no new vacancy write
    and acknowledges nothing; the next owner replays the message and advances the watermark."""
    from app.services import collector_exclusion as ce
    from app.services.collector_runner import run_xing

    monkeypatch.setattr(ce, "AUTOMATION_RUN_LEASE_TTL_SECONDS", 1.5)

    async def stolen_then_run(db, settings, *, is_lease_lost=None):
        with xing_replay() as side:  # a takeover elsewhere rewrites the holder
            side.execute(
                update(AutomationRunRecord)
                .where(AutomationRunRecord.account_key.in_([ACCOUNT, SCHEDULER_KEY]))
                .values(lease_holder="stolen-by-another-process")
            )
            side.commit()
        deadline = time.monotonic() + 20
        while not is_lease_lost() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert is_lease_lost(), "the heartbeat must notice the stolen lease"
        return await run_xing(db, settings, is_lease_lost=is_lease_lost)

    with xing_replay() as db, pytest.raises(ce.CollectorRunLeaseLostError):
        asyncio.run(run_collector_exclusively(db, _settings(), "xing", stolen_then_run))
    # no new vacancy write was started, nothing acknowledged, no watermark moved
    assert _imap_state(xing_replay) == (0, False, None)
    with xing_replay() as db:
        statuses = {
            r.status
            for r in db.scalars(
                select(AutomationRunRecord).where(AutomationRunRecord.account_key == ACCOUNT)
            )
        }
    assert "COMPLETED" not in statuses

    with xing_replay() as db:  # the takeover owner (the stolen row expires; recovery reclaims it)
        time.sleep(1.8)
        asyncio.run(run_collector_exclusively(db, _settings(), "xing", run_xing_healthy))
    assert _imap_state(xing_replay) == (1, True, (7_000_123, 1))


async def run_xing_healthy(db, settings, *, is_lease_lost=None):
    from app.services.collector_runner import run_xing

    return await run_xing(db, settings, is_lease_lost=is_lease_lost)


def test_a_cancelled_manual_run_keeps_the_slot_held_for_its_worker(factory):
    """Cancellation must not release exclusion: the slot stays RUNNING (lease extended past the
    IMAP deadline) and a new manual run is refused until the lease would expire."""
    from app.services import collector_exclusion as ce

    async def cancelled(db, settings, *, is_lease_lost=None):
        raise asyncio.CancelledError

    with factory() as db, pytest.raises(asyncio.CancelledError):
        asyncio.run(run_collector_exclusively(db, _settings(), "xing", cancelled))
    with factory() as db:
        run = db.scalars(
            select(AutomationRunRecord).where(AutomationRunRecord.account_key == ACCOUNT)
        ).one()
        assert run.status == "RUNNING"
        expires = run.lease_expires_at
        expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
        assert expires > datetime.now(UTC) + timedelta(seconds=ce.IMAP_SESSION_DEADLINE_SECONDS)

    async def quick(db, settings, *, is_lease_lost=None):
        return dict(STATS)

    with factory() as db, pytest.raises(CollectorRunInProgressError):
        asyncio.run(run_collector_exclusively(db, _settings(), "bundesagentur", quick))


def test_an_automation_cycle_is_refused_while_a_cli_style_holder_runs(factory, monkeypatch):
    overlap = _Overlap()
    _patch_steps(monkeypatch, overlap, hold_seconds=0.1)
    settings = _settings(automation_scheduler_account_key=SCHEDULER_KEY)
    inside, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def cli_holder():
        try:
            with factory() as db, hold_collector_slots(db, settings, "ba_refresh"):
                inside.set()
                assert release.wait(timeout=30)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=cli_holder)
    thread.start()
    assert inside.wait(timeout=30)
    for key in (ACCOUNT, SCHEDULER_KEY):  # both spellings are excluded
        with factory() as db, pytest.raises(AutomationRunAlreadyInProgressError):
            asyncio.run(run_automation_cycle(db, account_key=key, settings=settings))
    assert overlap.max_active == 0
    release.set()
    thread.join(timeout=30)
    assert errors == []
    with factory() as db:
        asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=settings))
    assert overlap.max_active == 1
