"""Stage 9D: Gmail draft handoff under GENUINELY OVERLAPPING transactions
against a REAL PostgreSQL server -- the acceptance evidence the Stage 9D
architecture requires (sequential SQLite is not concurrency evidence).

Every scenario runs the REAL Stage 9D/9C/6E/9B code in threads with
independent sessions/connections. A hook pauses one worker while it HOLDS
its transaction (`_Hold`); the other worker is started only after that
point, so both transactions are open at the same time. Each test asserts
the durable ledger, the exact attempt, one business row, the provider
invocation count, and (via timestamps) that the blocked worker really
waited for the holder's commit:

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

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set. Run
`alembic upgrade head` against that DEDICATED test database first (no
`create_all` fallback). No Gmail, IMAP or Telegram is ever contacted: the
draft provider is a thread-safe recording fake.
"""

import asyncio
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

import app.services.gmail_application_draft as gd
import app.services.telegram_bewerbung_approval as ta
from app.core.config import Settings
from app.db import gmail_application_draft_repository as repo
from app.db.models import GmailApplicationDraftRecord
from app.providers.email.draft_base import (
    DraftCreateRejectedError,
    DraftCreateResult,
    DraftLookupResult,
)
from tests.integration import test_telegram_bewerbung_approval_postgres as s9c_pg
from tests.integration.test_telegram_bewerbung_approval_postgres import (
    HOLD_SECONDS,
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


class _Hold:
    """Wrap `module.name`: when called in a thread named `thread_name`, run
    the real function, record the time, signal `held`, and keep the
    caller's open transaction (and its locks) alive for HOLD_SECONDS."""

    def __init__(self, monkeypatch, module, name, thread_name):
        self.held = threading.Event()
        self.acquired_at = None
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            if threading.current_thread().name == thread_name and not self.held.is_set():
                self.acquired_at = time.monotonic()
                self.held.set()
                time.sleep(HOLD_SECONDS)
            return result

        monkeypatch.setattr(module, name, wrapper)


def _start(name, target, *args):
    """Like `_run`, but the thread carries `name` BEFORE it starts."""
    box = {}

    def runner():
        try:
            box["result"] = target(*args)
        except BaseException as exc:
            box["error"] = exc
        box["finished_at"] = time.monotonic()

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
    factory, _, _, _ = env
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "insert_claim", "A")

    first = _start("A", _create, factory, package["cap"], provider)
    assert hold.held.wait(JOIN_TIMEOUT)
    second = _start("B", _create, factory, package["cap"], provider)
    a, b = _join(*first), _join(*second)

    assert {a["result"].code, b["result"].code} == {"CREATED", "IN_PROGRESS"}
    assert a["result"].code == "CREATED"
    row = _row(factory, package["link_id"])
    assert (row.state, row.attempt_count) == ("CREATED", 1)
    assert provider.creates == 1
    assert b["finished_at"] >= hold.acquired_at + HOLD_SECONDS  # B really waited


def test_1_unique_link_is_the_database_arbiter(env):
    """Two INSERTs of the same link in overlapping transactions: the
    second blocks on the unique index and loses with the KNOWN conflict."""
    factory, _, clock, _ = env
    package = _approved(env)
    handoff = repo.FrozenHandoff(1, 1, 1, 1, 1, 1, 1, "P" * 16, "i" * 64)
    holder = factory()
    repo.insert_claim(
        holder, link_id=package["link_id"], account_key="a", handoff=handoff, now=clock()
    )

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

    started = time.monotonic()
    thread, box = _start("B", contender)
    time.sleep(HOLD_SECONDS)
    holder.commit()
    holder.close()
    result = _join(thread, box)
    assert repo.classify_ledger_conflict(result["result"]) == repo.LINK_CONFLICT
    assert result["finished_at"] - started >= HOLD_SECONDS
    assert len(_rows(factory, package["link_id"])) == 1


# --- 2 ------------------------------------------------------------------------------


def test_2_begin_append_race_grants_exactly_one_permit(env, monkeypatch):
    factory, _, _, _ = env
    package = _approved(env)
    _approve(env, package)
    claimed = _claim(factory, package["cap"])
    hold = _Hold(monkeypatch, repo, "begin_append", "A")

    first = _start("A", _arm, factory, claimed)
    assert hold.held.wait(JOIN_TIMEOUT)
    second = _start("B", _arm, factory, claimed)
    a, b = _join(*first), _join(*second)

    permits = [r["result"] for r in (a, b) if isinstance(r["result"], gd._Permit)]
    assert len(permits) == 1 and isinstance(a["result"], gd._Permit)
    assert b["result"].code == "IN_PROGRESS"
    row = _row(factory, package["link_id"])
    assert row.marker_message_id == permits[0].marker and row.attempt_count == 1
    assert b["finished_at"] >= hold.acquired_at + HOLD_SECONDS


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
    assert hold.held.wait(JOIN_TIMEOUT)
    stale = _start("A", _arm, factory, old)
    b, a = _join(*taker), _join(*stale)

    assert b["result"].code == "CREATED"
    assert not isinstance(a["result"], gd._Permit)  # the old token can never arm
    row = _row(factory, package["link_id"])
    assert (row.state, row.attempt_count) == ("CREATED", 2)
    assert provider.creates == 1
    assert a["finished_at"] >= hold.acquired_at + HOLD_SECONDS


def test_3_old_begin_append_first_then_takeover_is_refused(env, monkeypatch):
    factory, _, clock, _ = env
    package = _approved(env)
    _approve(env, package)
    old = _claim(factory, package["cap"])
    clock.advance(gd.CLAIM_LEASE_SECONDS + 1)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "begin_append", "A")

    stale = _start("A", _arm, factory, old)
    assert hold.held.wait(JOIN_TIMEOUT)
    taker = _start("B", _create, factory, package["cap"], provider)
    a, b = _join(*stale), _join(*taker)

    assert isinstance(a["result"], gd._Permit)
    assert b["result"].code == "IN_PROGRESS" and provider.creates == 0
    row = _row(factory, package["link_id"])
    assert (row.state, row.attempt_count) == ("CREATING", 1)
    assert row.marker_message_id == a["result"].marker
    assert b["finished_at"] >= hold.acquired_at + HOLD_SECONDS


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
    assert hold.held.wait(JOIN_TIMEOUT)
    finalizer = _start("F", gd._finalize, factory, permit, evidence)
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
    assert f["finished_at"] >= hold.acquired_at + HOLD_SECONDS


def test_4_late_finalize_first_then_classifier_is_a_no_op(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    permit = _armed_permit(env, package)
    evidence = gd._evidence(permit, repo.CREATED, uid_validity=7, draft_uid=42)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "finalize_created", "F")

    finalizer = _start("F", gd._finalize, factory, permit, evidence)
    assert hold.held.wait(JOIN_TIMEOUT)
    classifier = _start("C", _create, factory, package["cap"], provider)
    f, c = _join(*finalizer), _join(*classifier)

    assert f["result"].code == "CREATED"
    assert c["result"].code in ("ALREADY_CREATED", "IN_PROGRESS")
    assert _row(factory, package["link_id"]).state == "CREATED"
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
    assert hold.held.wait(JOIN_TIMEOUT)
    second = _start("B", _create, factory, package["cap"], provider)
    a, b = _join(*first), _join(*second)

    assert a["result"].code == "CREATED"
    assert b["result"].code in ("IN_PROGRESS", "ALREADY_CREATED")
    row = _row(factory, package["link_id"])
    assert (row.state, row.attempt_count) == ("CREATED", 2)
    assert provider.creates == 2  # the definite failure + exactly one retry
    assert b["finished_at"] >= hold.acquired_at + HOLD_SECONDS


# --- 6 ------------------------------------------------------------------------------


def _uncertain(env, package, provider):
    factory, _, clock, _ = env
    provider.create_exc = TimeoutError("ambiguous")
    assert _create(factory, package["cap"], provider).code == "UNCERTAIN"
    provider.create_exc = None
    clock.advance(500)


def test_6_concurrent_positive_reconciliations_have_one_winner(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    _uncertain(env, package, provider)
    hold = _Hold(monkeypatch, repo, "reconcile_created", "A")

    first = _start("A", _reconcile, factory, package["cap"], provider)
    assert hold.held.wait(JOIN_TIMEOUT)
    second = _start("B", _reconcile, factory, package["cap"], provider)
    a, b = _join(*first), _join(*second)

    assert a["result"].code == "RECONCILED"
    assert b["result"].code in ("RECONCILED", "ALREADY_CREATED")
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
    assert hold.held.wait(JOIN_TIMEOUT)
    second = _start("B", retained)
    a, b = _join(*first), _join(*second)

    assert a["result"].code == "RECONCILED" and b["result"] is False
    final = _row(factory, package["link_id"])
    assert (final.reconciled, final.uid_validity, final.draft_uid) == (True, 7, 42)


# --- 7 ------------------------------------------------------------------------------


def test_7_replacement_waits_for_an_armed_create(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "begin_append", "A")

    creator = _start("A", _create, factory, package["cap"], provider)
    assert hold.held.wait(JOIN_TIMEOUT)
    replacer = _start("B", _replace_preparation, factory, package["prep_id"])
    a, b = _join(*creator), _join(*replacer)

    assert a["result"].code == "CREATED" and provider.creates == 1
    assert b["result"] is not None  # 9B reclaimed only after the arm committed
    assert b["finished_at"] >= hold.acquired_at + HOLD_SECONDS


def test_7_create_waits_for_a_replacement_and_refuses(env):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    held = threading.Event()

    replacer = _start(
        "B",
        lambda: _replace_preparation(
            factory, package["prep_id"], hold_seconds=HOLD_SECONDS, held=held
        ),
    )
    assert held.wait(JOIN_TIMEOUT)
    started = time.monotonic()
    creator = _start("A", _create, factory, package["cap"], provider)
    _join(*replacer)
    a = _join(*creator)

    assert a["result"].code == "PACKAGE_REPLACED"
    assert _rows(factory, package["link_id"]) == [] and provider.creates == 0
    assert a["finished_at"] - started >= HOLD_SECONDS * 0.5


def test_7_replacement_between_claim_and_arm_releases_without_append(env, monkeypatch):
    factory = env[0]
    package = _approved(env)
    _approve(env, package)
    provider = Provider()
    hold = _Hold(monkeypatch, repo, "insert_claim", "A")

    creator = _start("A", _create, factory, package["cap"], provider)
    assert hold.held.wait(JOIN_TIMEOUT)
    replacer = _start("B", _replace_preparation, factory, package["prep_id"])
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
    assert hold.held.wait(JOIN_TIMEOUT)
    creator = _start("A", _create, factory, package["cap"], provider)
    d, a = _join(*decider), _join(*creator)

    assert d["result"].code == ("APPROVED" if action == "f" else "REJECTED")
    assert a["result"].code == expected
    assert provider.creates == (1 if action == "f" else 0)
    assert len(_rows(factory, package["link_id"])) == (1 if action == "f" else 0)
    assert a["finished_at"] >= hold.acquired_at + HOLD_SECONDS


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
    assert hold.held.wait(JOIN_TIMEOUT)
    reader = _start("R", _reconcile, factory, package["cap"], provider)
    locker = _start("L", ledger_first_locker)
    a, r, lock = _join(*retrier), _join(*reader), _join(*locker)

    assert a["result"].code == "CREATED"
    # The unlocked status read never waited for, nor raced, the holder.
    assert r["result"].code == "FAILED" and r["finished_at"] < hold.acquired_at + HOLD_SECONDS
    assert provider.lookups == 0
    assert lock["result"] is True and lock["finished_at"] >= hold.acquired_at + HOLD_SECONDS
    row = _row(factory, package["link_id"])
    assert (row.state, row.attempt_count) == ("CREATED", 2) and provider.creates == 2


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
