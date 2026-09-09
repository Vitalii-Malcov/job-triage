"""S8A-002 (Codex re-review, crash recovery) regression tests for
`AutomationRunRecord`'s ownership-aware lease
(`lease_holder`/`lease_expires_at`) — real two-Session/connection
proofs, mirroring tests/test_gmail_repository.py's and
tests/test_follow_up_send_service.py::TestLeaseRenewalHeartbeat's own
patterns for Stage 7E's identical `GmailThreadRecord` lock.

Covers exactly the required scenarios:
* a live run blocks a duplicate claim
* the heartbeat keeps a slow run alive past its initial TTL
* the heartbeat (standing in for a crashed process) stopping lets the
  lease expire on schedule
* a later request reconciles a stale RUNNING row and succeeds
* an expired lease can never be silently renewed
"""

import asyncio
import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.automation_repository import (
    create_running_run,
    get_running_run_for_account,
    renew_run_lease,
)
from app.db.base import Base
from app.db.models import AutomationRunRecord, JobRecord
from app.services.automation import (
    AutomationRunAlreadyInProgressError,
    AutomationRunLeaseLostError,
    run_automation_cycle,
)

ACCOUNT = "me@example.com"


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_automation_lease.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class TestLiveLeaseBlocksDuplicate:
    def test_second_claim_while_lease_is_live_fails_closed(self, session_factory):
        session_a = session_factory()
        session_b = session_factory()
        try:
            run_a, created_a = create_running_run(
                session_a, account_key=ACCOUNT, holder="holder-A", ttl_seconds=30.0
            )
            assert created_a is True

            run_b, created_b = create_running_run(
                session_b, account_key=ACCOUNT, holder="holder-B", ttl_seconds=30.0
            )

            assert created_b is False
            assert run_b.id == run_a.id
            assert run_b.status == "RUNNING"
        finally:
            session_a.close()
            session_b.close()


class TestExpiredLeaseCannotBeSilentlyRenewed:
    def test_renewal_fails_once_the_lease_has_expired_even_if_nobody_else_claimed_it(
        self, session_factory
    ):
        db = session_factory()
        try:
            run, created = create_running_run(
                db, account_key=ACCOUNT, holder="holder-A", ttl_seconds=0.05
            )
            assert created is True

            time.sleep(0.15)  # let the tiny lease genuinely expire

            # Nobody else has touched this row -- renewal must STILL fail,
            # because acquire-style "nobody else has it" is NOT the bar
            # for a renewal (see renew_run_lease's own docstring / the
            # S7E-016 lesson this mirrors).
            renewed = renew_run_lease(db, run.id, holder="holder-A", ttl_seconds=30.0)
            assert renewed is False

            # And the row is genuinely still just sitting there RUNNING
            # with its now-expired lease -- confirming this is a real
            # expiry check, not a "someone else took it" check.
            current = get_running_run_for_account(db, ACCOUNT)
            assert current is not None
            assert current.id == run.id
            assert current.status == "RUNNING"
        finally:
            db.close()


class TestStaleRunIsReconciledAndRecovered:
    def test_later_request_reconciles_stale_running_and_succeeds(self, session_factory):
        session_a = session_factory()
        session_b = session_factory()
        try:
            stale_run, created = create_running_run(
                session_a, account_key=ACCOUNT, holder="crashed-holder", ttl_seconds=0.05
            )
            assert created is True

            time.sleep(0.15)  # the crashed holder's lease genuinely expires

            # A later request (a fully independent session) must succeed
            # -- the stale row is atomically reconciled to FAILED first.
            new_run, created_2 = create_running_run(
                session_b, account_key=ACCOUNT, holder="new-holder", ttl_seconds=30.0
            )
            assert created_2 is True
            assert new_run.id != stale_run.id

            # The old row is now a terminal, honestly-labeled FAILED
            # record -- never silently deleted or merged.
            session_a.expire_all()
            reloaded_stale = session_a.get(type(stale_run), stale_run.id)
            assert reloaded_stale.status == "FAILED"
            assert "expired" in reloaded_stale.error_summary.lower()

            # And the new run is the one now live for this account.
            current = get_running_run_for_account(session_b, ACCOUNT)
            assert current is not None
            assert current.id == new_run.id
        finally:
            session_a.close()
            session_b.close()


class TestHeartbeatKeepsSlowRunAlive:
    def test_heartbeat_keeps_the_lease_alive_past_the_initial_ttl(
        self, session_factory, monkeypatch
    ):
        db = session_factory()
        session_b = session_factory()
        try:
            acquire_attempts: list[tuple[bool, int, str]] = []  # (created, run_id, run_status)
            stop_polling = threading.Event()

            def _poll_session_b():
                time.sleep(0.2)  # let run_automation_cycle get past its setup
                while not stop_polling.is_set():
                    _run, created = create_running_run(
                        session_b, account_key=ACCOUNT, holder="session-B-writer"
                    )
                    acquire_attempts.append((created, _run.id, _run.status))
                    time.sleep(0.05)

            async def _slow_run_bundesagentur(db, settings, *, touched_jobs=None):
                await asyncio.sleep(2.0)
                return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

            async def _slow_run_xing(db, settings, *, touched_jobs=None):
                return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

            monkeypatch.setattr(
                "app.services.automation.run_bundesagentur", _slow_run_bundesagentur
            )
            monkeypatch.setattr("app.services.automation.run_xing", _slow_run_xing)

            poller = threading.Thread(target=_poll_session_b, daemon=True)
            poller.start()
            try:
                # lease_ttl_seconds (1.0s) is far shorter than the 2.0s
                # "step time" -- without the heartbeat, the lease would
                # lapse partway through and session B would succeed.
                run = asyncio.run(
                    run_automation_cycle(
                        db,
                        account_key=ACCOUNT,
                        settings=Settings(),
                        lease_ttl_seconds=1.0,
                        heartbeat_interval_seconds=0.1,
                    )
                )
            finally:
                stop_polling.set()
                poller.join(timeout=3)

            assert run.status == "COMPLETED"
            assert len(acquire_attempts) > 3

            # Session B's own write can be left queued behind db's SQLite
            # write lock right as the original run finishes -- so it may
            # legitimately claim a brand-new row the instant AFTER
            # completion, not a steal of the live one. What must NEVER
            # happen is B reconciling the ORIGINAL row (id == run.id) as
            # stale while the heartbeat was supposed to be keeping it
            # alive -- that would mean the heartbeat failed at its one
            # job. Every attempt that observed the original row must
            # therefore report it as still RUNNING and never actually
            # claim it (created=False).
            original_row_attempts = [
                (created, status)
                for created, claimed_run_id, status in acquire_attempts
                if claimed_run_id == run.id
            ]
            assert original_row_attempts, "expected at least one poll to observe the original row"
            for created, claimed_status in original_row_attempts:
                assert claimed_status == "RUNNING", (
                    "the original run must never be observed as reconciled/stolen "
                    "while the heartbeat should still be renewing it"
                )
                assert created is False

            # "Account usable again once a run reaches a terminal status"
            # is a separate property, already proven deterministically
            # (without a concurrent poller thread racing against it) by
            # TestStaleRunIsReconciledAndRecovered and
            # TestConcurrentDuplicateAtServiceLevel above.
        finally:
            db.close()
            session_b.close()


class TestHeartbeatStoppingLetsLeaseExpire:
    def test_process_crash_is_recovered_once_the_ttl_elapses(self, session_factory, monkeypatch):
        """Simulates a process crashing mid-run: the heartbeat's interval
        is set far longer than both the tiny TTL and the step's own
        duration, so it never gets a chance to renew before the lease
        genuinely lapses -- standing in for "the heartbeat/process died".
        A concurrent, fully independent session (playing the role of a
        later request) notices the expiry, reconciles the stale RUNNING
        row, and claims the account for itself while the original
        run_automation_cycle call is still mid-flight. The original call
        must then fail closed (AutomationRunLeaseLostError) instead of
        overwriting the new owner's state, and the account must be usable
        again afterward.
        """
        db = session_factory()
        session_b = session_factory()
        try:
            recovery_result: dict = {}

            def _recover_once_expired():
                time.sleep(0.15)  # well past the 0.05s TTL, well before the 0.3s step ends
                run, created = create_running_run(
                    session_b, account_key=ACCOUNT, holder="recovery-holder"
                )
                recovery_result["created"] = created
                recovery_result["run_id"] = run.id

            async def _slow_run_bundesagentur(db, settings, *, touched_jobs=None):
                await asyncio.sleep(0.3)
                return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

            async def _slow_run_xing(db, settings, *, touched_jobs=None):
                return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

            monkeypatch.setattr(
                "app.services.automation.run_bundesagentur", _slow_run_bundesagentur
            )
            monkeypatch.setattr("app.services.automation.run_xing", _slow_run_xing)

            recoverer = threading.Thread(target=_recover_once_expired, daemon=True)
            recoverer.start()
            try:
                with pytest.raises(AutomationRunLeaseLostError):
                    asyncio.run(
                        run_automation_cycle(
                            db,
                            account_key=ACCOUNT,
                            settings=Settings(),
                            lease_ttl_seconds=0.05,
                            heartbeat_interval_seconds=1.0,
                        )
                    )
            finally:
                recoverer.join(timeout=3)

            assert recovery_result.get("created") is True, (
                "the independent session must have been able to reconcile "
                "the stale RUNNING row and claim the account while the "
                "original call was still executing"
            )

            # The account is left usable afterward -- not permanently
            # blocked by the crashed run.
            current = get_running_run_for_account(session_b, ACCOUNT)
            assert current is not None
            assert current.id == recovery_result["run_id"]
            assert current.status == "RUNNING"
        finally:
            db.close()
            session_b.close()


class TestConcurrentDuplicateAtServiceLevel:
    def test_run_automation_cycle_raises_when_already_in_progress(self, session_factory):
        db = session_factory()
        session_b = session_factory()
        try:
            create_running_run(session_b, account_key=ACCOUNT, holder="other-holder")

            with pytest.raises(AutomationRunAlreadyInProgressError):
                asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=Settings()))
        finally:
            db.close()
            session_b.close()


class TestFinishRunRejectsAnExpiredLeaseEvenIfUncontested:
    """Follow-up lease-correctness fix: `finish_run`'s CAS must require
    `lease_expires_at >= now` on top of holder/status, not just
    holder/status. Without it, a run whose steps happen to finish after
    the TTL lapsed but before the heartbeat's first renewal tick would
    finalize as COMPLETED even though its ownership window had already
    expired and nobody was actively renewing it — an "expired but
    uncontested" gap distinct from (and not covered by) the
    already-tested "someone else reconciled/reclaimed it" scenarios.
    """

    def test_run_does_not_complete_when_steps_finish_after_ttl_but_before_first_heartbeat(
        self, session_factory, monkeypatch
    ):
        async def _bundesagentur_outlives_the_ttl(db, settings, *, touched_jobs=None):
            # Tiny TTL (0.05s) expires well before this step returns,
            # and the heartbeat interval (1.0s) is deliberately longer
            # than both the TTL and this step -- so no renewal attempt
            # happens at all before the run tries to finalize. Nobody
            # else touches the row either: this isolates the "expired
            # but uncontested" gap specifically.
            await asyncio.sleep(0.15)
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        async def _xing_noop(db, settings, *, touched_jobs=None):
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        monkeypatch.setattr(
            "app.services.automation.run_bundesagentur", _bundesagentur_outlives_the_ttl
        )
        monkeypatch.setattr("app.services.automation.run_xing", _xing_noop)

        db = session_factory()
        try:
            with pytest.raises(AutomationRunLeaseLostError):
                asyncio.run(
                    run_automation_cycle(
                        db,
                        account_key=ACCOUNT,
                        settings=Settings(),
                        lease_ttl_seconds=0.05,
                        heartbeat_interval_seconds=1.0,
                    )
                )

            # The row must NOT have been silently finalized as COMPLETED
            # -- it is either still RUNNING (with its lapsed lease) or
            # was reconciled, but it is never COMPLETED from this call.
            current = get_running_run_for_account(db, ACCOUNT)
            assert current is not None
            assert current.status != "COMPLETED"
        finally:
            db.close()


class TestHeartbeatRenewalExceptionFailsClosed:
    """Follow-up lease-correctness fix: an exception raised by
    `renew_run_lease` inside the heartbeat thread (e.g. a transient DB
    error) must never kill the heartbeat silently while
    `run_automation_cycle` carries on believing it still owns the lease.
    It must be treated exactly like a failed renewal: `lease_lost` set,
    heartbeat stops, the main call fails closed with
    `AutomationRunLeaseLostError` -- and the raw exception text must
    never leak into logs, the API/response layer, or persisted state.
    """

    def test_renewal_exception_sets_lease_lost_and_fails_the_run_closed(
        self, session_factory, monkeypatch, caplog
    ):
        def _boom(db, run_id, *, holder, ttl_seconds):
            raise RuntimeError("secret-db-detail")

        monkeypatch.setattr("app.services.automation.renew_run_lease", _boom)

        async def _slow_run_bundesagentur(db, settings, *, touched_jobs=None):
            # Long enough for the heartbeat's first tick (interval below)
            # to fire and hit the patched, always-raising renew_run_lease
            # before this step returns.
            await asyncio.sleep(0.3)
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        async def _xing_noop(db, settings, *, touched_jobs=None):
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        monkeypatch.setattr("app.services.automation.run_bundesagentur", _slow_run_bundesagentur)
        monkeypatch.setattr("app.services.automation.run_xing", _xing_noop)

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                with pytest.raises(AutomationRunLeaseLostError):
                    asyncio.run(
                        run_automation_cycle(
                            db,
                            account_key=ACCOUNT,
                            settings=Settings(),
                            lease_ttl_seconds=30.0,
                            heartbeat_interval_seconds=0.1,
                        )
                    )

            assert "secret-db-detail" not in caplog.text
            assert "RuntimeError" in caplog.text
        finally:
            db.close()


def _make_stub_heartbeat_class(captured: dict, *, start_sets_lease_lost: bool = False):
    """NEW-004 (Astra R4A) test helper: a `_RunLeaseHeartbeat` stand-in
    that never spawns a real background thread — `start()`/`stop()` are
    no-ops (or, with `start_sets_lease_lost=True`, `start()` marks the
    lease lost immediately, simulating the heartbeat's very first tick
    having already failed before the run even begins) — so a test can
    flip `.lease_lost` deterministically at an exact point in the step
    sequence instead of racing real timing. The real class is subclassed
    (not reimplemented) so its real, unmodified `threading.Event`-backed
    `lease_lost` attribute is exactly what
    `_raise_if_lease_lost`/`run_automation_cycle` actually check.
    `captured["heartbeat"]` lets a test's fake step functions reach back
    into the SAME instance `run_automation_cycle` is holding.
    """
    import app.services.automation as automation_module

    class _StubHeartbeat(automation_module._RunLeaseHeartbeat):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            captured["heartbeat"] = self

        def start(self) -> None:
            if start_sets_lease_lost:
                self.lease_lost.set()

        def stop(self) -> None:
            pass

    return _StubHeartbeat


class TestLeaseLossStopsFurtherWorkImmediately:
    """NEW-004 (Astra R4A): once lease ownership is confirmed lost,
    `run_automation_cycle` must stop launching NEW work immediately —
    checked before every step, not only once at the very end after every
    step had already run (the old behavior this replaces).
    """

    def test_lease_already_lost_before_first_step_invokes_no_collectors(
        self, session_factory, monkeypatch
    ):
        captured: dict = {}
        # The lease is already lost by the time the FIRST step would be
        # launched -- simulates the heartbeat's very first tick having
        # already failed before run_automation_cycle's own step loop
        # even started.
        monkeypatch.setattr(
            "app.services.automation._RunLeaseHeartbeat",
            _make_stub_heartbeat_class(captured, start_sets_lease_lost=True),
        )

        bundesagentur_calls = {"count": 0}
        xing_calls = {"count": 0}

        async def _bundesagentur_should_never_run(db, settings, *, touched_jobs=None):
            bundesagentur_calls["count"] += 1
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        async def _xing_should_never_run(db, settings, *, touched_jobs=None):
            xing_calls["count"] += 1
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        monkeypatch.setattr(
            "app.services.automation.run_bundesagentur", _bundesagentur_should_never_run
        )
        monkeypatch.setattr("app.services.automation.run_xing", _xing_should_never_run)

        db = session_factory()
        try:
            with pytest.raises(AutomationRunLeaseLostError):
                asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=Settings()))

            assert bundesagentur_calls["count"] == 0
            assert xing_calls["count"] == 0
        finally:
            db.close()

    def test_lease_lost_between_steps_stops_later_steps(self, session_factory, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            "app.services.automation._RunLeaseHeartbeat", _make_stub_heartbeat_class(captured)
        )

        xing_calls = {"count": 0}

        async def _bundesagentur_then_lease_is_lost(db, settings, *, touched_jobs=None):
            # Simulates the heartbeat discovering lease loss WHILE this
            # step was running -- by the time it returns, ownership is
            # already gone.
            captured["heartbeat"].lease_lost.set()
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        async def _xing_should_never_run(db, settings, *, touched_jobs=None):
            xing_calls["count"] += 1
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        monkeypatch.setattr(
            "app.services.automation.run_bundesagentur", _bundesagentur_then_lease_is_lost
        )
        monkeypatch.setattr("app.services.automation.run_xing", _xing_should_never_run)

        db = session_factory()
        try:
            with pytest.raises(AutomationRunLeaseLostError):
                asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=Settings()))

            assert xing_calls["count"] == 0
        finally:
            db.close()

    def test_lease_lost_after_one_completed_step_preserves_its_real_result(
        self, session_factory, monkeypatch
    ):
        """The completed step's own durable work (here: a real
        `JobRecord` it persisted) must survive even though the run's own
        bookkeeping row is never finalized -- only THIS run's summary is
        abandoned, never the underlying data a step already committed.
        """
        captured: dict = {}
        monkeypatch.setattr(
            "app.services.automation._RunLeaseHeartbeat", _make_stub_heartbeat_class(captured)
        )

        xing_calls = {"count": 0}

        async def _bundesagentur_persists_a_job_then_lease_is_lost(
            db, settings, *, touched_jobs=None
        ):
            now = datetime.now(UTC)
            db.add(
                JobRecord(
                    fingerprint="fp-new-004",
                    source="bundesagentur",
                    title="Backend Engineer",
                    company="Acme GmbH",
                    location="Berlin",
                    url="https://example.com/jobs/new-004",
                    description="",
                    score=80,
                    recommendation="APPLY",
                    status="NEW",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            db.commit()
            captured["heartbeat"].lease_lost.set()
            return {"fetched": 1, "created": 1, "updated": 0, "skipped_invalid": 0, "failed": 0}

        async def _xing_should_never_run(db, settings, *, touched_jobs=None):
            xing_calls["count"] += 1
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        monkeypatch.setattr(
            "app.services.automation.run_bundesagentur",
            _bundesagentur_persists_a_job_then_lease_is_lost,
        )
        monkeypatch.setattr("app.services.automation.run_xing", _xing_should_never_run)

        db = session_factory()
        try:
            run_id_before = None
            with pytest.raises(AutomationRunLeaseLostError):
                asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=Settings()))

            assert xing_calls["count"] == 0

            # The job bundesagentur's step persisted is durable.
            db.expire_all()
            job = db.query(JobRecord).filter_by(fingerprint="fp-new-004").one_or_none()
            assert job is not None
            assert job.title == "Backend Engineer"

            # But the run's own bookkeeping was never finalized as
            # COMPLETED -- exclusivity for that summary was already lost.
            run = get_running_run_for_account(db, ACCOUNT)
            if run is not None:
                run_id_before = run.id
            assert run_id_before is None or run.status != "COMPLETED"
        finally:
            db.close()


class TestReplacementWorkerAfterLeaseLoss:
    """NEW-004 (Astra R4A) requirement #4: after the original worker
    fails closed on confirmed lease loss, a REPLACEMENT worker must
    still be able to claim the account and run to a real completion --
    the early fail-closed checks must not leave the account permanently
    stuck, and must not change the existing stale-reconciliation/CAS
    safety already proven by TestStaleRunIsReconciledAndRecovered.
    """

    def test_replacement_worker_completes_after_original_fails_closed_immediately(
        self, session_factory, monkeypatch
    ):
        captured: dict = {}
        monkeypatch.setattr(
            "app.services.automation._RunLeaseHeartbeat",
            _make_stub_heartbeat_class(captured, start_sets_lease_lost=True),
        )

        async def _noop_step(db, settings, *, touched_jobs=None):
            return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_step)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_step)

        db = session_factory()
        try:
            with pytest.raises(AutomationRunLeaseLostError):
                # A short TTL so the abandoned row genuinely expires
                # quickly (the stub heartbeat never renews it for real).
                asyncio.run(
                    run_automation_cycle(
                        db, account_key=ACCOUNT, settings=Settings(), lease_ttl_seconds=0.05
                    )
                )

            original_run = get_running_run_for_account(db, ACCOUNT)
            assert original_run is not None
            assert original_run.status == "RUNNING"  # abandoned, never finalized
        finally:
            db.close()

        time.sleep(0.15)  # let the abandoned lease genuinely expire

        # Restore the REAL heartbeat/collector steps for the replacement
        # worker -- proves this is a normal, fully successful run, not
        # another stubbed-out short-circuit.
        monkeypatch.undo()

        db2 = session_factory()
        try:
            replacement = asyncio.run(
                run_automation_cycle(db2, account_key=ACCOUNT, settings=Settings())
            )

            assert replacement.id != original_run.id
            assert replacement.status in ("COMPLETED", "PARTIAL", "FAILED")

            # The original, abandoned row was reconciled to a terminal
            # state (never silently deleted/merged) -- exactly the same
            # CAS-based recovery TestStaleRunIsReconciledAndRecovered
            # already proves at the repository level.
            db2.expire_all()
            reloaded_original = db2.get(AutomationRunRecord, original_run.id)
            assert reloaded_original.status == "FAILED"
        finally:
            db2.close()
