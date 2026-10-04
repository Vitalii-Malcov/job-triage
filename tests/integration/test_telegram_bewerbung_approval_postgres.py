"""Stage 9C: Telegram review/approval under GENUINELY OVERLAPPING
transactions against a REAL PostgreSQL server -- the acceptance evidence
the Stage 9C architecture requires and that SQLite (no row locks, a
single writer) cannot provide.

Every scenario runs the REAL Stage 9C/6E/9B code in two threads with
independent sessions/connections. A hook pauses the first worker while it
HOLDS its row locks (`hold`), so the second worker's transaction is open and
blocked on the same rows at the same time; the test then asserts both the
durable outcome and that the blocked worker really waited for the holder's
commit (timestamps), i.e. the transactions overlapped:

A. two simultaneous "Zur Prüfung" creations -> exactly one link, one
   Telegram-linked review, one revision 1;
B. approve vs approve -> one terminal transition, one decided_at;
C. approve vs reject, BOTH winner orders -> exactly one terminal winner;
D. profile / job update overlapping an approval -> lock-through-commit
   (the update waits for the approval, or the approval waits and then sees
   the committed change and refuses);
E. Stage 9B preparation replacement overlapping review creation and
   approval -> serialized, or a stale refusal.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set (e.g.
`postgresql+psycopg://user:password@localhost:5432/dbname`). Run
`alembic upgrade head` against that database first -- there is no
`create_all` fallback. Use a DEDICATED test database: the candidate profile
is a singleton row that these tests (re)write. Rows created here are
removed by job id afterwards.

Telegram is never contacted: a fake sender records calls.
"""

import asyncio
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.orm import sessionmaker

import app.services.telegram_bewerbung_approval as ta
from app.core.config import Settings
from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
)
from app.db.models import (
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    CandidateJobMatchRecord,
    JobRecord,
    TelegramBewerbungApprovalRecord,
    TelegramBewerbungPreparationRecord,
    TelegramVacancyReviewRecord,
)
from app.db.repositories import upsert_job
from app.db.telegram_bewerbung_repository import claim_preparation
from app.db.telegram_vacancy_review_repository import claim_for_sending, ensure_review, mark_sent
from app.models.candidate_profile import (
    CandidateProfilePatchRequest,
    CandidateProject,
    CandidateSkill,
)
from app.models.job import Job, JobScore
from app.services import telegram_bewerbung as tb
from app.services.telegram import TelegramSendOutcome, TelegramSendResult

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally.",
)

HOLD_SECONDS = 1.5
JOIN_TIMEOUT = 60


class _Sender:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    async def __call__(self, bot_token, chat_id, text_, *, reply_markup=None, timeout_seconds=5.0):
        with self._lock:
            self.calls.append(text_)
            return TelegramSendResult(TelegramSendOutcome.SENT, 9000 + len(self.calls))


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        telegram_chat_id="4242",
        telegram_bewerbung_draft_enabled=True,
        telegram_bewerbung_approval_enabled=True,
    )


@pytest.fixture()
def pg():
    engine = create_engine(TEST_POSTGRES_URL, pool_pre_ping=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    job_ids: list[int] = []
    yield factory, job_ids
    db = factory()
    try:
        review_ids = list(
            db.scalars(
                select(ApplicationPackageReviewRecord.id).where(
                    ApplicationPackageReviewRecord.job_id.in_(job_ids)
                )
            )
        )
        db.execute(
            delete(TelegramBewerbungApprovalRecord).where(
                TelegramBewerbungApprovalRecord.review_id.in_(review_ids)
            )
        )
        db.execute(
            delete(ApplicationPackageReviewRevisionRecord).where(
                ApplicationPackageReviewRevisionRecord.review_id.in_(review_ids)
            )
        )
        db.execute(
            delete(ApplicationPackageReviewRecord).where(
                ApplicationPackageReviewRecord.id.in_(review_ids)
            )
        )
        db.execute(  # cascades to the Stage 9B ledger
            delete(TelegramVacancyReviewRecord).where(
                TelegramVacancyReviewRecord.job_id.in_(job_ids)
            )
        )
        for model in (BewerbungDraftRecord, CandidateCVDraftRecord, CandidateJobMatchRecord):
            db.execute(delete(model).where(model.job_id.in_(job_ids)))
        db.execute(delete(JobRecord).where(JobRecord.id.in_(job_ids)))
        db.commit()
    finally:
        db.close()
    engine.dispose()


def _seed(factory, job_ids) -> dict:
    """A candidate profile, a job, a sent Stage 9A card, and a PREPARED
    Stage 9B package (real 6B/6C/6D services)."""
    db = factory()
    try:
        current = get_or_create_candidate_profile(db).profile_version
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=current,
                first_name="Anna",
                last_name="Muster",
                skills=[CandidateSkill(name="Python"), CandidateSkill(name="SQL")],
                projects=[CandidateProject(name="ChallengeMatch API", technologies=["Python"])],
            ),
        )
        suffix = uuid.uuid4().hex
        job, _ = upsert_job(
            db,
            Job(
                source="test",
                title="Junior Python Developer",
                company="Example GmbH",
                url=f"https://example.com/s9c-pg/{suffix}",
                description="Python, SQL und Kubernetes.",
                must_have_skills=["Python", "SQL", "Kubernetes"],
            ),
            JobScore(score=90, recommendation="APPLY"),
        )
        db.commit()
        job_ids.append(job.id)
        review = ensure_review(db, job.id, eligible=True)
        mark_sent(db, review, claim_token=claim_for_sending(db, review), message_id=1)
        review_id, job_id = review.id, job.id
    finally:
        db.close()
    outcome = asyncio.run(tb.handle_apply(factory, _settings(), review_id, send=_Sender()))
    assert outcome.code == "PREVIEW_SENT", outcome
    db = factory()
    try:
        prep = db.scalar(
            select(TelegramBewerbungPreparationRecord).where(
                TelegramBewerbungPreparationRecord.review_id == review_id
            )
        )
        return {"job_id": job_id, "prep_id": prep.id, "token": prep.package_token}
    finally:
        db.close()


def _request(factory, token):
    return asyncio.run(ta.handle_request_review(factory, _settings(), token, send=_Sender()))


def _decide(factory, capability, action):
    return asyncio.run(ta.handle_decision(factory, _settings(), capability, action))


def _link_for(factory, prep_id):
    db = factory()
    try:
        return db.scalar(
            select(TelegramBewerbungApprovalRecord).where(
                TelegramBewerbungApprovalRecord.preparation_id == prep_id
            )
        )
    finally:
        db.close()


def _review(factory, review_id):
    db = factory()
    try:
        return db.get(ApplicationPackageReviewRecord, review_id)
    finally:
        db.close()


class _Hold:
    """Wrap a Stage 9C lock helper: the FIRST thread to acquire it records
    the time, signals `held`, and keeps holding its transaction's locks for
    HOLD_SECONDS. Later callers pass straight through (and block in the
    database while the holder sleeps)."""

    def __init__(self, monkeypatch, name):
        self.held = threading.Event()
        self.acquired_at = None
        self._first = threading.Lock()
        self._taken = False
        real = getattr(ta, name)

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            with self._first:
                first = not self._taken
                self._taken = True
            if first:
                self.acquired_at = time.monotonic()
                self.held.set()
                time.sleep(HOLD_SECONDS)
            return result

        monkeypatch.setattr(ta, name, wrapper)


def _run(target, *args):
    box = {}

    def runner():
        try:
            box["result"] = target(*args)
        except BaseException as exc:  # surfaced by _join
            box["error"] = exc
        box["finished_at"] = time.monotonic()

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, box


def _join(thread, box):
    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive(), "worker deadlocked"
    if "error" in box:
        raise box["error"]
    return box


# --- A: simultaneous review creation -----------------------------------------


def test_a_simultaneous_review_creation_commits_exactly_one(pg, monkeypatch):
    factory, job_ids = pg
    seed = _seed(factory, job_ids)
    hold = _Hold(monkeypatch, "lock_preparation_fresh")
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        return _request(factory, seed["token"])

    first = _run(worker)
    second = _run(worker)
    a, b = _join(*first), _join(*second)

    assert {a["result"].code, b["result"].code} == {"REVIEW_SHOWN"}
    db = factory()
    try:
        links = list(
            db.scalars(
                select(TelegramBewerbungApprovalRecord).where(
                    TelegramBewerbungApprovalRecord.preparation_id == seed["prep_id"]
                )
            )
        )
        reviews = list(
            db.scalars(
                select(ApplicationPackageReviewRecord).where(
                    ApplicationPackageReviewRecord.job_id == seed["job_id"]
                )
            )
        )
        revisions = list(
            db.scalars(
                select(ApplicationPackageReviewRevisionRecord).where(
                    ApplicationPackageReviewRevisionRecord.review_id.in_([r.id for r in reviews])
                )
            )
        )
    finally:
        db.close()
    assert len(links) == 1 and len(reviews) == 1 and len(revisions) == 1
    assert revisions[0].revision_number == 1 and links[0].review_id == reviews[0].id
    # The two transactions overlapped: the second finished only after the
    # holder released its locks.
    assert hold.acquired_at is not None
    assert max(a["finished_at"], b["finished_at"]) >= hold.acquired_at + HOLD_SECONDS


# --- B / C: overlapping decisions ----------------------------------------------


def _requested(factory, job_ids):
    seed = _seed(factory, job_ids)
    assert _request(factory, seed["token"]).code == "REVIEW_SHOWN"
    link = _link_for(factory, seed["prep_id"])
    return seed, link


def test_b_approve_vs_approve_is_one_transition(pg, monkeypatch):
    factory, job_ids = pg
    _, link = _requested(factory, job_ids)
    hold = _Hold(monkeypatch, "lock_review_header_fresh")
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        return _decide(factory, link.approval_capability, "f")

    first = _run(worker)
    second = _run(worker)
    codes = sorted([_join(*first)["result"].code, _join(*second)["result"].code])

    assert codes == ["ALREADY_APPROVED", "APPROVED"]
    review = _review(factory, link.review_id)
    assert review.status == "APPROVED" and review.decided_at is not None
    assert hold.acquired_at is not None


@pytest.mark.parametrize("winner", ["approve", "reject"])
def test_c_approve_vs_reject_has_exactly_one_winner(pg, monkeypatch, winner):
    factory, job_ids = pg
    _, link = _requested(factory, job_ids)
    hold = _Hold(monkeypatch, "lock_review_header_fresh")
    first_action, second_action = ("f", "x") if winner == "approve" else ("x", "f")

    holder = _run(_decide, factory, link.approval_capability, first_action)
    assert hold.held.wait(JOIN_TIMEOUT)
    waiter = _run(_decide, factory, link.approval_capability, second_action)
    first, second = _join(*holder), _join(*waiter)

    review = _review(factory, link.review_id)
    if winner == "approve":
        assert (first["result"].code, second["result"].code) == ("APPROVED", "ALREADY_APPROVED")
        assert review.status == "APPROVED"
    else:
        assert (first["result"].code, second["result"].code) == ("REJECTED", "ALREADY_REJECTED")
        assert review.status == "REJECTED"
    assert second["finished_at"] >= hold.acquired_at + HOLD_SECONDS


# --- D: profile / job updates overlapping an approval ---------------------------


def _update_in_own_transaction(factory, statement, *, hold_seconds=0.0, held=None):
    db = factory()
    try:
        db.execute(statement)
        if held is not None:
            held.set()
        time.sleep(hold_seconds)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("target", ["job", "profile"])
def test_d_update_waits_for_an_approval_holding_the_lock(pg, monkeypatch, target):
    factory, job_ids = pg
    seed, link = _requested(factory, job_ids)
    hold = _Hold(monkeypatch, "lock_review_header_fresh")  # all approval locks are held here
    statement = (
        update(JobRecord).where(JobRecord.id == seed["job_id"]).values(company="Changed GmbH")
        if target == "job"
        else text("UPDATE candidate_profiles SET profile_version = profile_version + 1")
    )

    approver = _run(_decide, factory, link.approval_capability, "f")
    assert hold.held.wait(JOIN_TIMEOUT)
    updater = _run(_update_in_own_transaction, factory, statement)
    approved, updated = _join(*approver), _join(*updater)

    assert approved["result"].code == "APPROVED"
    # The update could not commit underneath the approval: it waited for it.
    assert updated["finished_at"] >= hold.acquired_at + HOLD_SECONDS


@pytest.mark.parametrize("target", ["job", "profile"])
def test_d_approval_waits_for_an_update_and_sees_it(pg, target):
    factory, job_ids = pg
    seed, link = _requested(factory, job_ids)
    statement = (
        update(JobRecord).where(JobRecord.id == seed["job_id"]).values(company="Changed GmbH")
        if target == "job"
        else text("UPDATE candidate_profiles SET profile_version = profile_version + 1")
    )
    held = threading.Event()

    updater = _run(
        lambda: _update_in_own_transaction(factory, statement, hold_seconds=HOLD_SECONDS, held=held)
    )
    assert held.wait(JOIN_TIMEOUT)
    started = time.monotonic()
    approver = _run(_decide, factory, link.approval_capability, "f")
    _join(*updater)
    approved = _join(*approver)

    assert approved["result"].code == "PACKAGE_STALE"  # fresh value seen after the wait
    assert approved["finished_at"] - started >= HOLD_SECONDS * 0.5
    assert _review(factory, link.review_id).status == "PENDING_REVIEW"


# --- E: Stage 9B replacement overlapping creation / approval --------------------


def _replace_preparation(factory, prep_id, *, hold_seconds=0.0, held=None):
    """Stage 9B's real reclaim CAS (PREPARED -> PREPARING, generation + 1)
    for a changed input identity, in its own transaction."""
    db = factory()
    try:
        record = db.get(TelegramBewerbungPreparationRecord, prep_id)
        if hold_seconds:
            db.execute(
                select(TelegramBewerbungPreparationRecord)
                .where(TelegramBewerbungPreparationRecord.id == prep_id)
                .with_for_update()
            )
            if held is not None:
                held.set()
            time.sleep(hold_seconds)
        return claim_preparation(db, record, input_identity="e" * 64)
    finally:
        db.close()


def test_e_replacement_waits_for_review_creation(pg, monkeypatch):
    factory, job_ids = pg
    seed = _seed(factory, job_ids)
    hold = _Hold(monkeypatch, "lock_preparation_fresh")

    requester = _run(_request, factory, seed["token"])
    assert hold.held.wait(JOIN_TIMEOUT)
    replacer = _run(_replace_preparation, factory, seed["prep_id"])
    requested, replaced = _join(*requester), _join(*replacer)

    assert requested["result"].code == "REVIEW_SHOWN"
    assert replaced["result"] is not None  # 9B reclaimed after the review committed
    assert replaced["finished_at"] >= hold.acquired_at + HOLD_SECONDS
    link = _link_for(factory, seed["prep_id"])
    assert link.generation == 1
    # The replaced generation-1 review can no longer be approved.
    assert _decide(factory, link.approval_capability, "f").code == "PACKAGE_REPLACED"


def test_e_review_creation_waits_for_replacement_and_refuses(pg):
    factory, job_ids = pg
    seed = _seed(factory, job_ids)
    held = threading.Event()

    replacer = _run(
        lambda: _replace_preparation(factory, seed["prep_id"], hold_seconds=HOLD_SECONDS, held=held)
    )
    assert held.wait(JOIN_TIMEOUT)
    requester = _run(_request, factory, seed["token"])
    _join(*replacer)
    requested = _join(*requester)

    assert requested["result"].code == "PACKAGE_EXPIRED"
    assert _link_for(factory, seed["prep_id"]) is None


def test_e_replacement_waits_for_approval(pg, monkeypatch):
    factory, job_ids = pg
    seed, link = _requested(factory, job_ids)
    hold = _Hold(monkeypatch, "lock_review_header_fresh")

    approver = _run(_decide, factory, link.approval_capability, "f")
    assert hold.held.wait(JOIN_TIMEOUT)
    replacer = _run(_replace_preparation, factory, seed["prep_id"])
    approved, replaced = _join(*approver), _join(*replacer)

    assert approved["result"].code == "APPROVED"
    assert replaced["finished_at"] >= hold.acquired_at + HOLD_SECONDS


def test_e_approval_waits_for_replacement_and_refuses(pg):
    factory, job_ids = pg
    seed, link = _requested(factory, job_ids)
    held = threading.Event()

    replacer = _run(
        lambda: _replace_preparation(factory, seed["prep_id"], hold_seconds=HOLD_SECONDS, held=held)
    )
    assert held.wait(JOIN_TIMEOUT)
    approver = _run(_decide, factory, link.approval_capability, "f")
    _join(*replacer)
    approved = _join(*approver)

    assert approved["result"].code == "PACKAGE_REPLACED"
    assert _review(factory, link.review_id).status == "PENDING_REVIEW"
