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
            acquire_attempts = {"total": 0, "succeeded": 0}
            stop_polling = threading.Event()

            def _poll_session_b():
                time.sleep(0.2)  # let run_automation_cycle get past its setup
                while not stop_polling.is_set():
                    acquire_attempts["total"] += 1
                    _run, created = create_running_run(
                        session_b, account_key=ACCOUNT, holder="session-B-writer"
                    )
                    if created:
                        acquire_attempts["succeeded"] += 1
                    time.sleep(0.05)

            async def _slow_run_bundesagentur(db, settings):
                await asyncio.sleep(2.0)
                return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

            async def _slow_run_xing(db, settings):
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
            assert acquire_attempts["total"] > 3
            assert acquire_attempts["succeeded"] == 0, (
                "Session B must never claim the run while the heartbeat is alive "
                "and renewing on the original holder's behalf"
            )

            # After the cycle has fully finished (lease released by virtue
            # of the run reaching a terminal status), a fresh claim works.
            fresh, created = create_running_run(session_b, account_key=ACCOUNT, holder="after")
            assert created is True
            assert fresh.id != run.id
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

            async def _slow_run_bundesagentur(db, settings):
                await asyncio.sleep(0.3)
                return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}

            async def _slow_run_xing(db, settings):
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
