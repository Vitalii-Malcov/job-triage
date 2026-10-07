"""Stage 9D: Gmail draft handoff under GENUINELY OVERLAPPING transactions
against a REAL PostgreSQL server -- the acceptance evidence the Stage 9D
architecture requires (sequential SQLite is not concurrency evidence).

Every scenario runs the REAL Stage 9D/9C/6E/9B code in threads with
independent sessions/connections. No Gmail, IMAP or Telegram is ever
contacted: the draft provider is a thread-safe recording fake.

Two kinds of test live here (S9D-CODEX-001):

A. OUTCOME / IDEMPOTENCY assertions never require one particular
   transient report where the schedule is left free. They accept an
   explicitly ENUMERATED set of valid reports (e.g. `ONE_CREATION_REPORTS`)
   and then assert the durable invariants: one business row, the exact
   final state and attempt, and the provider invocation count.

B. FORCED-CONCURRENCY tests claim a specific overlap (lock waiting, a
   stale takeover race, a begin_append race, a classifier/finalizer race,
   a retry race, a reconcile race, a 9B/9C boundary). They PROVE it --
   nothing here depends on sleeps, elapsed time or finish timestamps:

   1. the holder runs the real repository/service function inside its
      open transaction, records its PostgreSQL backend pid and signals
      `held`, then BLOCKS on an explicit `release` event (`_Hold`/`_Gate`);
   2. the contender is started and the controller polls
      `pg_blocking_pids()` until PostgreSQL itself reports a backend
      waiting on the holder's lock (`_await_blocked_on`) -- the contender
      has provably reached the conflicting statement while the lock is
      still held (a contender that finishes without blocking FAILS);
   3. only then is the holder released, and the durable results are
      asserted.

   Where a scenario instead requires that a reader does NOT wait for the
   holder, the reader is joined while the holder is still held.

Scenarios:

1. simultaneous first create (+ UNIQUE(link_id) as the DB arbiter)
2. begin_append race on the same claim
3. stale pre-fence takeover vs the old worker's begin_append (both orders)
4. stale post-fence classifier vs a late CREATED finalize (both orders)
5. FAILED retry race
6. UNCERTAIN positive reconcile race (+ vs retained-OK)
7. Stage 9B package replacement vs create (three interleavings)
8. Stage 9C/6E decision boundary vs create (approve / reject)
9. a ledger-first reader/locker vs the normal full lock order
10. DB commit-acknowledgment loss after a REAL commit

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set (the CI
`scheduler-postgres` job always sets it). Run `alembic upgrade head`
against that DEDICATED test database first (no `create_all` fallback).
"""

import asyncio
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text

import app.services.gmail_application_draft as gd
import app.services.telegram_bewerbung_approval as ta
from app.core.config import Settings
from app.db import gmail_application_draft_repository as repo
from app.db.models import GmailApplicationDraftRecord, TelegramBewerbungPreparationRecord
from app.db.telegram_bewerbung_repository import claim_preparation
from app.providers.email.draft_base import (
    DraftCreateRejectedError,
    DraftCreateResult,
    DraftLookupResult,
)
from tests.integration import test_telegram_bewerbung_approval_postgres as s9c_pg
from tests.integration.test_telegram_bewerbung_approval_postgres import (
    JOIN_TIMEOUT,
    _decide,
    _join,
    _link_for,
    _replace_preparation,
    _request,
    _seed,
)

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pg = s9c_pg.pg  # the Stage 9C PostgreSQL seed/cleanup fixture

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally.",
)

POLL_SECONDS = 0.01  # lock-wait polling cadence only -- never a correctness bound

# Every valid (creator, observer) report pair for ONE durable creation: the
# observer either saw the creator's attempt still running (IN_PROGRESS) or
# reported only after it finalized (ALREADY_CREATED -- a historical report).
ONE_CREATION_REPORTS = {("CREATED", "IN_PROGRESS"), ("CREATED", "ALREADY_CREATED")}


def _settings(**overrides) -> Settings:
    data = dict(
        telegram_bot_token="test-token",
        telegram_chat_id="4242",
        telegram_bewerbung_draft_enabled=True,
        telegram_bewerbung_approval_enabled=True,
        telegram_gmail_draft_enabled=True,
        gmail_username="me@example.com",
        gmail_app_password="app-pw",
        gmail_drafts_mailbox="[Gmail]/Drafts",
        gmail_draft_attempt_budget_seconds=60,
        gmail_draft_reconcile_min_age_seconds=120,
    )
    data.update(overrides)
    return Settings(**data)


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.now

    def advance(self, seconds):
        with self._lock:
            self.now = self.now + timedelta(seconds=seconds)


class Provider:
    """Thread-safe recording fake. Never touches the network."""

    def __init__(self):
        self._lock = threading.Lock()
        self.creates = 0
        self.lookups = 0
        self.create_exc = None
        self.lookup_result = DraftLookupResult(7, (42,))

    def create_draft(self, message, target, deadline_at):
        with self._lock:
            self.creates += 1
        if self.create_exc is not None:
            raise self.create_exc
        return DraftCreateResult(7, 42)

    def find_by_message_id(self, reconcile_target, deadline_at):
        with self._lock:
            self.lookups += 1
        return self.lookup_result


def _backend_pid(db) -> int:
    """The PostgreSQL backend serving `db`'s CURRENT transaction."""
    return db.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar_one()


class _Gate:
    """A holder's open transaction: `held` once its locks are taken (with
    its backend `pid`), kept open until the controller sets `release`."""

    def __init__(self):
        self.held = threading.Event()
        self.release = threading.Event()
        self.pid = None
        self.release_timed_out = False

    def hold(self, db):
        self.pid = _backend_pid(db)
        self.held.set()
        if not self.release.wait(JOIN_TIMEOUT):
            self.release_timed_out = True

    def wait_held(self):
        assert self.held.wait(JOIN_TIMEOUT), "holder never reached its lock"
        assert self.pid is not None


class _Hold(_Gate):
    """Wrap `module.name` (first argument: the Session): when called in a
    thread named `thread_name`, run the real function, then hold that
    caller's open transaction (and its locks) until released."""

    def __init__(self, monkeypatch, module, name, thread_name):
        super().__init__()
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            if threading.current_thread().name == thread_name and not self.held.is_set():
                self.hold(args[0])
            return result

        monkeypatch.setattr(module, name, wrapper)


def _await_blocked_on(factory, holder_pid, *contenders):
    """Return once PostgreSQL reports a backend WAITING on `holder_pid`'s
    lock -- proof that a contender reached the conflicting statement while
    the holder still holds it. A contender that finishes first fails."""
    deadline = time.monotonic() + JOIN_TIMEOUT
    probe = factory()
    try:
        while True:
            waiting = probe.execute(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE CAST(:holder AS integer) = ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            ).all()
            probe.rollback()
            if waiting:
                return [row.pid for row in waiting]
            for thread, box in contenders:
                assert thread.is_alive(), (
                    f"contender {thread.name} finished WITHOUT waiting for the holder: {box}"
                )
            assert time.monotonic() < deadline, "contender never blocked on the holder"
            time.sleep(POLL_SECONDS)
    finally:
        probe.close()


def _overlap(factory, gate, *contenders):
    """Forced overlap: the contenders provably wait on the holder, THEN the
    holder is released (always released, even if the proof fails)."""
    try:
        _await_blocked_on(factory, gate.pid, *contenders)
    finally:
        gate.release.set()
    assert not gate.release_timed_out


def _start(name, target, *args):
    """Run `target` in a thread that carries `name` BEFORE it starts."""
    box = {}

    def runner():
        try:
            box["result"] = target(*args)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=runner, name=name)
    thread.start()
    return thread, box


@pytest.fixture()
def env(pg, monkeypatch):
    factory, job_ids = pg
    clock = Clock()
    monkeypatch.setattr(gd, "_now", clock)
    link_ids: list[int] = []
    yield factory, job_ids, clock, link_ids
    db = factory()
    try:
        db.execute(
            delete(GmailApplicationDraftRecord).where(
                GmailApplicationDraftRecord.link_id.in_(link_ids)
            )
        )
        db.commit()
    finally:
        db.close()


def _approved(env) -> dict:
    factory, job_ids, _, link_ids = env
    seed = _seed(factory, job_ids)
    assert _request(factory, seed["token"]).code == "REVIEW_SHOWN"
    link = _link_for(factory, seed["prep_id"])
    link_ids.append(link.id)
    return {**seed, "link_id": link.id, "cap": link.approval_capability}


def _approve(env, package):
    assert _decide(env[0], package["cap"], "f").code == "APPROVED"


def _create(factory, cap, provider, **settings):
    return asyncio.run(gd.handle_create(factory, _settings(**settings), cap, provider=provider))


def _reconcile(factory, cap, provider):
    return asyncio.run(gd.handle_reconcile(factory, _settings(), cap, provider=provider))


def _rows(factory, link_id) -> list[GmailApplicationDraftRecord]:
    db = factory()
    try:
        return list(
            db.scalars(
                select(GmailApplicationDraftRecord).where(
                    GmailApplicationDraftRecord.link_id == link_id
                )
            )
        )
    finally:
        db.close()


def _row(factory, link_id) -> GmailApplicationDraftRecord:
    (row,) = _rows(factory, link_id)
    return row


def _assert_one_creation(factory, package, provider, *, attempt=1, creates=1):
    """The durable invariants of exactly one successful authorized APPEND."""
    rows = _rows(factory, package["link_id"])
    assert len(rows) == 1, f"expected one business ledger row, got {len(rows)}"
    (row,) = rows
    assert (row.state, row.attempt_count) == ("CREATED", attempt)
    assert row.marker_message_id is not None and row.claim_token is None
    assert provider.creates == creates


def _config():
    return gd._static_config(_settings())


def _claim(factory, cap):
    claimed = gd._run_db(factory, gd._phase_claim(cap, _config()))
    assert isinstance(claimed, gd._Claimed), claimed
    return claimed


def _arm(factory, claimed):
    return gd._run_db(factory, gd._phase_arm(claimed, _config()))


# --- 1 ------------------------------------------------------------------------------


def test_1_simultaneous_first_create_one_row_one_append(env, monkeypatch):
    """FORCED: B provably waits on A's open first-claim transaction. After A
    is released B may observe A still creating OR already finalized -- both
    valid; the durable result must be exactly one creation."""
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "insert_claim", "A")

    first = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    second = _start("B", _create, factory, package["cap"], provider)
    _overlap(factory, hold, second)
    a, b = _join(*first), _join(*second)

    assert (a["result"].code, b["result"].code) in ONE_CREATION_REPORTS
    _assert_one_creation(factory, package, provider)


def test_1_observer_during_the_armed_attempt_reports_in_progress(env, monkeypatch):
    """FORCED: B runs to completion while A provably still holds its armed,
    unfinalized attempt -- the only valid report is IN_PROGRESS."""
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "begin_append", "A")

    first = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    try:
        b = _join(*_start("B", _create, factory, package["cap"], provider))
        assert provider.creates == 0  # A has not appended yet: still creating
    finally:
        hold.release.set()
    a = _join(*first)

    assert not hold.release_timed_out
    assert (a["result"].code, b["result"].code) == ("CREATED", "IN_PROGRESS")
    _assert_one_creation(factory, package, provider)


def test_1_observer_after_finalize_reports_already_created(env, monkeypatch):
    """FORCED (the Codex S9D-CODEX-001 schedule): B starts while A holds its
    claim, but reaches fresh validation only after A FINALIZED. The valid
    report is the historical ALREADY_CREATED, never a second APPEND."""
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "insert_claim", "A")
    b_reached, a_done = threading.Event(), threading.Event()
    real_revalidate = gd.revalidate_approved_link_locked

    def revalidate(db, link_id):
        if threading.current_thread().name == "B":
            b_reached.set()
            assert a_done.wait(JOIN_TIMEOUT)
        return real_revalidate(db, link_id)

    monkeypatch.setattr(gd, "revalidate_approved_link_locked", revalidate)

    first = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    second = _start("B", _create, factory, package["cap"], provider)
    try:
        assert b_reached.wait(JOIN_TIMEOUT)  # B passed its unlocked read: no row yet
    finally:
        hold.release.set()
    try:
        a = _join(*first)
    finally:
        a_done.set()
    b = _join(*second)

    assert not hold.release_timed_out
    assert (a["result"].code, b["result"].code) == ("CREATED", "ALREADY_CREATED")
    _assert_one_creation(factory, package, provider)


def test_1_free_schedule_first_create_is_one_creation(env):
    """OUTCOME: two creates released together by a barrier, schedule left
    free. Either thread may be the creator; the other's report must be one
    of the enumerated valid observations of that single creation."""
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(JOIN_TIMEOUT)
        return _create(factory, package["cap"], provider)

    first, second = _start("A", racer), _start("B", racer)
    codes = (_join(*first)["result"].code, _join(*second)["result"].code)

    assert codes in ONE_CREATION_REPORTS or codes[::-1] in ONE_CREATION_REPORTS, codes
    _assert_one_creation(factory, package, provider)


def test_1_unique_link_is_the_database_arbiter(env):
    """Two INSERTs of the same link in overlapping transactions: the
    second provably blocks on the unique index and loses with the KNOWN
    conflict."""
    factory, _, clock, _ = env
    package = _approved(env)
    handoff = repo.FrozenHandoff(1, 1, 1, 1, 1, 1, 1, "P" * 16, "i" * 64)
    holder = factory()
    repo.insert_claim(
        holder, link_id=package["link_id"], account_key="a", handoff=handoff, now=clock()
    )
    holder_pid = _backend_pid(holder)

    def contender():
        db = factory()
        try:
            repo.insert_claim(
                db, link_id=package["link_id"], account_key="a", handoff=handoff, now=clock()
            )
            db.commit()
            return None
        except Exception as exc:  # noqa: BLE001 -- classified below
            db.rollback()
            return exc
        finally:
            db.close()

    thread, box = _start("B", contender)
    try:
        _await_blocked_on(factory, holder_pid, (thread, box))
    finally:
        holder.commit()
        holder.close()
    result = _join(thread, box)
    assert repo.classify_ledger_conflict(result["result"]) == repo.LINK_CONFLICT
    assert len(_rows(factory, package["link_id"])) == 1


# --- 2 ------------------------------------------------------------------------------


def test_2_begin_append_race_grants_exactly_one_permit(env, monkeypatch):
    factory, _, _, _ = env
    package = _approved(env)
    _approve(env, package)
    claimed = _claim(factory, package["cap"])
    hold = _Hold(monkeypatch, repo, "begin_append", "A")

    first = _start("A", _arm, factory, claimed)
    hold.wait_held()
    second = _start("B", _arm, factory, claimed)
    _overlap(factory, hold, second)
    a, b = _join(*first), _join(*second)

    permits = [r["result"] for r in (a, b) if isinstance(r["result"], gd._Permit)]
    assert len(permits) == 1 and isinstance(a["result"], gd._Permit)
    assert b["result"].code == "IN_PROGRESS"
    row = _row(factory, package["link_id"])
    assert row.marker_message_id == permits[0].marker and row.attempt_count == 1


# --- 3 ------------------------------------------------------------------------------


def test_3_takeover_first_then_old_begin_append_is_fenced(env, monkeypatch):
    factory, _, clock, _ = env
    package = _approved(env)
    _approve(env, package)
    old = _claim(factory, package["cap"])
    clock.advance(gd.CLAIM_LEASE_SECONDS + 1)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "takeover_pre_fence", "B")

    taker = _start("B", _create, factory, package["cap"], provider)
    hold.wait_held()
    stale = _start("A", _arm, factory, old)
    _overlap(factory, hold, stale)
    b, a = _join(*taker), _join(*stale)

    assert b["result"].code == "CREATED"
    assert not isinstance(a["result"], gd._Permit)  # the old token can never arm
    _assert_one_creation(factory, package, provider, attempt=2)


def test_3_old_begin_append_first_then_takeover_is_refused(env, monkeypatch):
    factory, _, clock, _ = env
    package = _approved(env)
    _approve(env, package)
    old = _claim(factory, package["cap"])
    clock.advance(gd.CLAIM_LEASE_SECONDS + 1)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "begin_append", "A")

    stale = _start("A", _arm, factory, old)
    hold.wait_held()
    taker = _start("B", _create, factory, package["cap"], provider)
    _overlap(factory, hold, taker)
    a, b = _join(*stale), _join(*taker)

    assert isinstance(a["result"], gd._Permit)
    assert b["result"].code == "IN_PROGRESS" and provider.creates == 0
    row = _row(factory, package["link_id"])
    assert (row.state, row.attempt_count) == ("CREATING", 1)
    assert row.marker_message_id == a["result"].marker


# --- 4 ------------------------------------------------------------------------------


def _armed_permit(env, package):
    factory = env[0]
    permit = _arm(factory, _claim(factory, package["cap"]))
    assert isinstance(permit, gd._Permit)
    env[2].advance(60 + gd.STALE_MARGIN_SECONDS + 1)
    return permit


def test_4_stale_classifier_first_then_retained_ok_creates(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    permit = _armed_permit(env, package)
    evidence = gd._evidence(permit, repo.CREATED, uid_validity=7, draft_uid=42)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "classify_stale_armed", "C")

    classifier = _start("C", _create, factory, package["cap"], provider)
    hold.wait_held()
    finalizer = _start("F", gd._finalize, factory, permit, evidence)
    _overlap(factory, hold, finalizer)
    c, f = _join(*classifier), _join(*finalizer)

    assert c["result"].code == "UNCERTAIN" and f["result"].code == "CREATED"
    row = _row(factory, package["link_id"])
    assert (row.state, row.reconciled, row.uid_validity, row.draft_uid) == (
        "CREATED",
        False,
        7,
        42,
    )
    assert row.claim_token is None and row.attempt_count == 1
    assert provider.creates == 0  # the classifier never gained APPEND authority


def test_4_late_finalize_first_then_classifier_is_a_no_op(env, monkeypatch):
    """FORCED: the classifier provably waits on the CREATED finalize; once
    it commits, the classifier's CAS misses and it reports history."""
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    permit = _armed_permit(env, package)
    evidence = gd._evidence(permit, repo.CREATED, uid_validity=7, draft_uid=42)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "finalize_created", "F")

    finalizer = _start("F", gd._finalize, factory, permit, evidence)
    hold.wait_held()
    classifier = _start("C", _create, factory, package["cap"], provider)
    _overlap(factory, hold, classifier)
    f, c = _join(*finalizer), _join(*classifier)

    assert f["result"].code == "CREATED"
    assert c["result"].code == "ALREADY_CREATED"
    row = _row(factory, package["link_id"])
    assert (row.state, row.attempt_count) == ("CREATED", 1)
    assert provider.creates == 0


# --- 5 ------------------------------------------------------------------------------


def test_5_failed_retry_race_is_one_new_attempt(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    provider.create_exc = DraftCreateRejectedError("x")
    assert _create(factory, package["cap"], provider).code == "FAILED"
    provider.create_exc = None
    hold = _Hold(monkeypatch, repo, "retry_failed", "A")

    first = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    second = _start("B", _create, factory, package["cap"], provider)
    _overlap(factory, hold, second)
    a, b = _join(*first), _join(*second)

    assert (a["result"].code, b["result"].code) in ONE_CREATION_REPORTS
    # the definite failure + exactly one retry
    _assert_one_creation(factory, package, provider, attempt=2, creates=2)


# --- 6 ------------------------------------------------------------------------------


def _uncertain(env, package, provider):
    factory, _, clock, _ = env
    provider.create_exc = TimeoutError("ambiguous")
    assert _create(factory, package["cap"], provider).code == "UNCERTAIN"
    provider.create_exc = None
    clock.advance(500)


def test_6_concurrent_positive_reconciliations_have_one_winner(env, monkeypatch):
    """FORCED: B's positive CAS provably waits on A's; B's CAS then misses
    and it reports A's identical reconciliation."""
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    _uncertain(env, package, provider)
    hold = _Hold(monkeypatch, repo, "reconcile_created", "A")

    first = _start("A", _reconcile, factory, package["cap"], provider)
    hold.wait_held()
    second = _start("B", _reconcile, factory, package["cap"], provider)
    _overlap(factory, hold, second)
    a, b = _join(*first), _join(*second)

    assert (a["result"].code, b["result"].code) == ("RECONCILED", "RECONCILED")
    row = _row(factory, package["link_id"])
    assert (row.state, row.reconciled, row.draft_uid) == ("CREATED", True, 42)
    assert provider.creates == 1 and provider.lookups == 2


def test_6_retained_ok_never_overwrites_a_concurrent_reconciliation(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    _uncertain(env, package, provider)
    row = _row(factory, package["link_id"])
    hold = _Hold(monkeypatch, repo, "reconcile_created", "A")

    def retained():
        db = factory()
        try:
            won = repo.retained_ok_created(
                db,
                ledger_id=row.id,
                attempt_count=row.attempt_count,
                marker=row.marker_message_id,
                uid_validity=None,
                draft_uid=None,
                now=datetime.now(UTC),
            )
            db.commit()
            return won
        finally:
            db.close()

    first = _start("A", _reconcile, factory, package["cap"], provider)
    hold.wait_held()
    second = _start("B", retained)
    _overlap(factory, hold, second)
    a, b = _join(*first), _join(*second)

    assert a["result"].code == "RECONCILED" and b["result"] is False
    final = _row(factory, package["link_id"])
    assert (final.reconciled, final.uid_validity, final.draft_uid) == (True, 7, 42)
    assert provider.creates == 1


# --- 7 ------------------------------------------------------------------------------


def _replace_preparation_holding(factory, prep_id, gate: _Gate):
    """Stage 9B's real reclaim CAS, but its transaction first locks the
    preparation row and holds it until `gate.release`."""
    db = factory()
    try:
        record = db.get(TelegramBewerbungPreparationRecord, prep_id)
        db.execute(
            select(TelegramBewerbungPreparationRecord)
            .where(TelegramBewerbungPreparationRecord.id == prep_id)
            .with_for_update()
        )
        gate.hold(db)
        return claim_preparation(db, record, input_identity="e" * 64)
    finally:
        db.close()


def test_7_replacement_waits_for_an_armed_create(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "begin_append", "A")

    creator = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    replacer = _start("B", _replace_preparation, factory, package["prep_id"])
    _overlap(factory, hold, replacer)
    a, b = _join(*creator), _join(*replacer)

    assert a["result"].code == "CREATED"
    assert b["result"] is not None  # 9B reclaimed only after the arm committed
    _assert_one_creation(factory, package, provider)


def test_7_create_waits_for_a_replacement_and_refuses(env):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    gate = _Gate()

    replacer = _start("B", _replace_preparation_holding, factory, package["prep_id"], gate)
    gate.wait_held()
    creator = _start("A", _create, factory, package["cap"], provider)
    _overlap(factory, gate, creator)
    b, a = _join(*replacer), _join(*creator)

    assert b["result"] is not None
    assert a["result"].code == "PACKAGE_REPLACED"
    assert _rows(factory, package["link_id"]) == [] and provider.creates == 0


def test_7_replacement_between_claim_and_arm_releases_without_append(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "insert_claim", "A")

    creator = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    replacer = _start("B", _replace_preparation, factory, package["prep_id"])
    _overlap(factory, hold, replacer)
    b, a = _join(*replacer), _join(*creator)

    assert b["result"] is not None
    assert a["result"].code == "PACKAGE_REPLACED" and provider.creates == 0
    row = _row(factory, package["link_id"])
    assert (row.state, row.append_started_at) == ("FAILED", None)


# --- 8 ------------------------------------------------------------------------------


@pytest.mark.parametrize(("action", "expected"), [("f", "CREATED"), ("x", "ALREADY_REJECTED")])
def test_8_create_waits_for_an_overlapping_9c_decision(env, monkeypatch, action, expected):
    factory = env[0]
    package = _approved(env)  # PENDING review: the decision races the create
    provider = Provider()
    hold = _Hold(monkeypatch, ta, "lock_review_header_fresh", "D")

    decider = _start("D", _decide, factory, package["cap"], action)
    hold.wait_held()
    creator = _start("A", _create, factory, package["cap"], provider)
    _overlap(factory, hold, creator)
    d, a = _join(*decider), _join(*creator)

    assert d["result"].code == ("APPROVED" if action == "f" else "REJECTED")
    assert a["result"].code == expected
    assert provider.creates == (1 if action == "f" else 0)
    assert len(_rows(factory, package["link_id"])) == (1 if action == "f" else 0)


# --- 9 ------------------------------------------------------------------------------


def test_9_ledger_first_reader_and_locker_never_deadlock_or_gain_authority(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    provider.create_exc = DraftCreateRejectedError("x")
    _create(factory, package["cap"], provider)  # FAILED row exists
    provider.create_exc = None
    hold = _Hold(monkeypatch, repo, "lock_by_link", "A")

    def ledger_first_locker():
        db = factory()
        try:
            db.execute(
                select(GmailApplicationDraftRecord)
                .where(GmailApplicationDraftRecord.link_id == package["link_id"])
                .with_for_update()
            )
            db.commit()  # acquires nothing earlier: no lock-order inversion
            return True
        finally:
            db.close()

    retrier = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    try:
        # The unlocked status read completes while A still HOLDS the ledger
        # lock: it never waited for, nor raced, the holder.
        r = _join(*_start("R", _reconcile, factory, package["cap"], provider))
        assert r["result"].code == "FAILED" and provider.lookups == 0
        locker = _start("L", ledger_first_locker)
        _await_blocked_on(factory, hold.pid, locker)  # the ledger locker DOES wait
    finally:
        hold.release.set()
    a, lock = _join(*retrier), _join(*locker)

    assert not hold.release_timed_out
    assert a["result"].code == "CREATED"
    assert lock["result"] is True
    _assert_one_creation(factory, package, provider, attempt=2, creates=2)


# --- 10 -----------------------------------------------------------------------------


class _AckLoss:
    """PostgreSQL session factory whose commit REALLY commits, then raises
    a client-facing OperationalError once `predicate(rows)` holds."""

    def __init__(self, factory, predicate):
        from sqlalchemy.exc import OperationalError

        self.factory = factory
        self.predicate = predicate
        self.fired = 0
        self._error = OperationalError

    def __call__(self):
        db = self.factory()
        real_commit = db.commit

        def commit():
            real_commit()
            if not self.fired:
                probe = self.factory()
                try:
                    rows = probe.scalars(select(GmailApplicationDraftRecord)).all()
                finally:
                    probe.close()
                if self.predicate(rows):
                    self.fired += 1
                    raise self._error("COMMIT", {}, Exception("connection lost after commit"))

        db.commit = commit
        return db


def test_10_begin_append_ack_lost_never_appends(env):
    factory, _, clock, _ = env
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    lossy = _AckLoss(
        factory,
        lambda rows: any(
            r.link_id == package["link_id"] and r.append_started_at is not None for r in rows
        ),
    )
    outcome = _create(lossy, package["cap"], provider)
    assert lossy.fired == 1 and outcome.code == "STATUS_UNSAVED"
    assert provider.creates == 0
    row = _row(factory, package["link_id"])  # fresh independent session
    assert row.state == "CREATING" and row.append_started_at is not None
    clock.advance(60 + gd.STALE_MARGIN_SECONDS + 1)
    assert _create(factory, package["cap"], provider).code == "UNCERTAIN"
    assert provider.creates == 0 and _row(factory, package["link_id"]).attempt_count == 1


def test_10_created_finalize_ack_lost_is_recovered_db_only(env):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    lossy = _AckLoss(
        factory,
        lambda rows: any(r.link_id == package["link_id"] and r.state == "CREATED" for r in rows),
    )
    outcome = _create(lossy, package["cap"], provider)
    assert lossy.fired == 1 and outcome.code == "CREATED"
    assert provider.creates == 1 and _row(factory, package["link_id"]).state == "CREATED"
