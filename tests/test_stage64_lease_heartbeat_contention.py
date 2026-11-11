"""Stage 64: a transient SQLite lock must not be read as "the lease was lost".

Stage 63 rehearsal: `stage22_refresh_ba --apply --create-new` returned exit 3 after 83 created
vacancies. The create loop commits one transaction right after another; the heartbeat's renewal
(its own connection) waited the whole 5 s SQLite busy timeout, got SQLITE_BUSY and the heartbeat
declared the lease lost although it was still valid. Reproduced with a back-to-back writer.
"""

import sqlite3
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.services.automation as automation
from app.db.automation_repository import create_running_run
from app.db.models import AutomationRunRecord
from app.services.automation import _RunLeaseHeartbeat, is_transient_lock_error


class _Orig(Exception):
    def __init__(self, code):
        super().__init__("database is locked")
        self.sqlite_errorcode = code


def _busy(code=5):
    return OperationalError("UPDATE ...", {}, _Orig(code))


class _Session:
    def rollback(self):
        pass

    def close(self):
        pass


def _heartbeat(monkeypatch, outcomes, *, ttl, interval=0.05):
    """A heartbeat whose renewals follow `outcomes` (an exception to raise or a bool)."""
    calls = iter(outcomes)

    def fake_renew(session, run_id, *, holder, ttl_seconds):
        item = next(calls, True)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(automation, "renew_run_lease", fake_renew)
    monkeypatch.setattr(_RunLeaseHeartbeat, "__init__", _init_without_engine(ttl, interval))
    hb = _RunLeaseHeartbeat.__new__(_RunLeaseHeartbeat)
    hb.__init__()
    return hb


def _init_without_engine(ttl, interval):
    def init(self):
        self._session_factory = _Session
        self._run_id = 1
        self._holder = "h"
        self._ttl_seconds = ttl
        self._interval_seconds = interval
        self._stop_event = threading.Event()
        self.lease_lost = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    return init


@pytest.mark.parametrize("code,expected", [(5, True), (6, True), (261, True), (1, False)])
def test_only_busy_and_locked_are_transient(code, expected):
    assert is_transient_lock_error(_busy(code)) is expected
    assert is_transient_lock_error(RuntimeError("x")) is False


def test_a_busy_renewal_is_retried_while_the_lease_is_alive(monkeypatch):
    hb = _heartbeat(monkeypatch, [_busy(), _busy(), True, True], ttl=5.0)
    hb.start()
    time.sleep(0.4)
    assert not hb.lease_lost.is_set()
    hb.stop()


def test_busy_past_the_ttl_is_a_lost_lease(monkeypatch):
    # every renewal stays busy: once ttl has passed without a success the lease IS lost
    hb = _heartbeat(monkeypatch, [_busy()] * 1000, ttl=0.3, interval=0.05)
    hb.start()
    assert hb.lease_lost.wait(3.0)
    hb.stop()


def test_a_refused_renewal_is_still_an_immediate_loss(monkeypatch):
    hb = _heartbeat(monkeypatch, [False], ttl=30.0)
    hb.start()
    assert hb.lease_lost.wait(2.0)
    hb.stop()


def test_a_non_lock_error_is_still_an_immediate_loss(monkeypatch):
    hb = _heartbeat(monkeypatch, [_busy(code=1)], ttl=30.0)
    hb.start()
    assert hb.lease_lost.wait(2.0)
    hb.stop()


def test_real_sqlite_lock_held_by_another_connection_is_survived(tmp_path):
    """Real two-connection proof: another connection holds the write lock for ~1 s while the
    heartbeat renews with a 0.2 s busy timeout. Before Stage 64 the first BUSY ended the run."""
    path = tmp_path / "lease.db"
    engine = create_engine(
        f"sqlite:///{path.as_posix()}", poolclass=NullPool, connect_args={"timeout": 0.2}
    )
    AutomationRunRecord.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        run, _ = create_running_run(db, account_key="a", holder="h", ttl_seconds=30)
        hb = _RunLeaseHeartbeat(db, run.id, holder="h", ttl_seconds=30, interval_seconds=0.1)
        raw = sqlite3.connect(path, isolation_level=None, timeout=1)
        raw.execute("create table scratch(x)")
        hb.start()
        raw.execute("BEGIN IMMEDIATE")
        raw.execute("insert into scratch values (1)")
        time.sleep(1.0)  # several renewal attempts hit SQLITE_BUSY meanwhile
        assert not hb.lease_lost.is_set()
        raw.execute("COMMIT")
        raw.close()
        time.sleep(0.6)  # the next renewal goes through
        assert not hb.lease_lost.is_set()
        hb.stop()
        assert hb.is_alive() is False
