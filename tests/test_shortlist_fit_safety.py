"""Shortlist safety: the fit tier is enforced on every AUTOMATIC path.

`NOT_SUITABLE` (and NEEDS_OPERATOR / STRETCH / unknown / unavailable) never gets an automatic
CV or Bewerbung draft, whatever `jobs.score` or the APPLY recommendation say; nothing here
sends anything or changes a status.
"""

import asyncio
import inspect

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    ApplicationRecord,
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    JobRecord,
)
from app.services import shortlist_gate
from app.services.job_fit_assessment import Criterion, FitAssessment, assess_job
from app.services.shortlist_gate import (
    AUTO_PREPARE_TIERS,
    automatic_block_reason,
    automatic_preparation_block,
    confirmed_barrier,
)
from tests.test_automation_shortlist import _run_shortlist, _seed_job, _settings

TIERS = ("NOT_SUITABLE", "NEEDS_OPERATOR", "STRETCH", "PARTIAL_MATCH", "GOOD_MATCH")


class _Fixed(FitAssessment):
    """An assessment with a forced tier (the real tier is derived from criteria)."""

    forced: str = "GOOD_MATCH"

    @property
    def fit_tier(self):
        return self.forced


def _fixed(tier: str, job_id: int = 1) -> FitAssessment:
    a = _Fixed(job_id=job_id, company="X", title="T")
    a.forced = tier
    # automatic preparation needs CONFIRMED full time (Stage 54): the fixture states it
    a.criteria = [Criterion("work_time", "FULL_TIME", "full-time stated")]
    return a


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fit_safety.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _tier_by_job(monkeypatch, mapping):
    def fake(session, job_id):
        tier = mapping if isinstance(mapping, str) else mapping[job_id]
        return _fixed(tier, job_id)

    monkeypatch.setattr(shortlist_gate, "assess_job", fake)


def _drafts(db):
    return (
        db.scalar(select(func.count()).select_from(CandidateCVDraftRecord)),
        db.scalar(select(func.count()).select_from(BewerbungDraftRecord)),
    )


# --- the pure rule ------------------------------------------------------------


@pytest.mark.parametrize("tier", TIERS)
def test_rule_allows_only_good_and_partial(tier):
    reason = automatic_block_reason(_fixed(tier))
    assert (reason is None) == (tier in AUTO_PREPARE_TIERS)
    assert {"GOOD_MATCH", "PARTIAL_MATCH"} == AUTO_PREPARE_TIERS
    if reason:
        assert reason == f"FIT_TIER_{tier}"


def test_unknown_tier_is_blocked():
    assert automatic_block_reason(_fixed("SOMETHING NEW!")) == "FIT_TIER_UNKNOWN"


def test_assessment_failure_is_fail_closed_and_isolated(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db)

    def boom(session, job_id):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(shortlist_gate, "assess_job", boom)
    assert automatic_preparation_block(db, job.id) == "ASSESSMENT_UNAVAILABLE"
    assert confirmed_barrier(db, job.id) == "ASSESSMENT_UNAVAILABLE"
    # the caller's session is untouched and still usable
    assert db.get(JobRecord, job.id) is not None
    db.close()


def test_gate_writes_nothing(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db)
    _tier_by_job(monkeypatch, "GOOD_MATCH")
    before = (job.status, job.score, job.recommendation)
    assert automatic_preparation_block(db, job.id) is None
    db.refresh(job)
    assert (job.status, job.score, job.recommendation) == before
    assert db.scalar(select(func.count()).select_from(ApplicationRecord)) == 0
    db.close()


def test_confirmed_barrier_blocks_only_not_suitable(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db)
    for tier in TIERS:
        _tier_by_job(monkeypatch, tier)
        expected = "FIT_TIER_NOT_SUITABLE" if tier == "NOT_SUITABLE" else None
        assert confirmed_barrier(db, job.id) == expected
    db.close()


def test_real_assessment_marks_it_administration_not_suitable(session_factory):
    db = session_factory()
    for title in ("IT-Administratoren (m/w/d)", "Systemadmin (m/w/d)", "Client Support (m/w/d)"):
        job = _seed_job(db, title=title, score=95, recommendation="APPLY")
        assert assess_job(db, job.id).fit_tier == "NOT_SUITABLE", title
        assert automatic_preparation_block(db, job.id) == "FIT_TIER_NOT_SUITABLE", title
    db.close()


def test_real_assessment_does_not_flag_developer_titles_as_off_role(session_factory):
    db = session_factory()
    for title in (
        "Junior Python Developer (m/w/d)",
        "Backend Entwickler mit Docker und Linux (m/w/d)",
        "Trainee Softwareentwicklung (m/w/d)",
    ):
        job = _seed_job(db, title=title)
        assert assess_job(db, job.id).criterion("role").status != "OFF_ROLE", title
    db.close()


# --- the automatic shortlist --------------------------------------------------


def test_apply_not_suitable_gets_no_draft_even_with_top_score(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db, score=99, recommendation="APPLY")
    _tier_by_job(monkeypatch, "NOT_SUITABLE")
    result = _run_shortlist(db, _settings(), [job])
    assert result["counters"]["blocked_by_fit"] == 1
    assert result["counters"]["shortlisted"] == 0
    assert result["counters"]["failed"] == 0 and result["status"] == "ok"
    assert _drafts(db) == (0, 0)
    db.close()


@pytest.mark.parametrize("tier", ["NEEDS_OPERATOR", "STRETCH"])
def test_apply_needs_operator_or_stretch_stops_before_manual_decision(
    session_factory, monkeypatch, tier
):
    db = session_factory()
    job = _seed_job(db, score=95)
    _tier_by_job(monkeypatch, tier)
    result = _run_shortlist(db, _settings(), [job])
    assert result["counters"]["blocked_by_fit"] == 1
    assert _drafts(db) == (0, 0)
    db.close()


@pytest.mark.parametrize("tier", ["GOOD_MATCH", "PARTIAL_MATCH"])
def test_apply_suitable_is_allowed(session_factory, monkeypatch, tier):
    db = session_factory()
    job = _seed_job(db)
    _tier_by_job(monkeypatch, tier)
    result = _run_shortlist(db, _settings(), [job])
    assert result["counters"]["blocked_by_fit"] == 0
    assert result["counters"]["shortlisted"] == 1
    assert _drafts(db) == (1, 1)
    db.close()


def test_blocked_jobs_do_not_consume_the_per_run_cap(session_factory, monkeypatch):
    db = session_factory()
    top = _seed_job(db, score=99)
    ok = _seed_job(db, score=60)
    _tier_by_job(monkeypatch, {top.id: "NOT_SUITABLE", ok.id: "GOOD_MATCH"})
    result = _run_shortlist(db, _settings(automation_shortlist_max_per_run=1), [top, ok])
    assert [i["job_id"] for i in result["items"]] == [ok.id]
    assert result["counters"]["blocked_by_fit"] == 1
    db.close()


def test_assessment_failure_blocks_without_failing_the_run(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db)

    def boom(session, job_id):
        raise RuntimeError("x")

    monkeypatch.setattr(shortlist_gate, "assess_job", boom)
    result = _run_shortlist(db, _settings(), [job])
    assert result["counters"]["blocked_by_fit"] == 1
    assert result["counters"]["failed"] == 0
    assert _drafts(db) == (0, 0)
    db.close()


def test_tier_change_between_selection_and_drafting_is_caught(session_factory, monkeypatch):
    """The gate is re-checked right before the drafts, not only at selection."""
    db = session_factory()
    job = _seed_job(db)
    calls = {"n": 0}

    def flipping(session, job_id):
        calls["n"] += 1
        return _fixed("GOOD_MATCH" if calls["n"] == 1 else "NOT_SUITABLE", job_id)

    monkeypatch.setattr(shortlist_gate, "assess_job", flipping)
    result = _run_shortlist(db, _settings(), [job])
    assert result["counters"]["blocked_by_fit"] == 1
    assert _drafts(db) == (0, 0)
    db.close()


def test_tier_change_after_cv_blocks_the_bewerbung(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db)
    calls = {"n": 0}

    def flipping(session, job_id):
        calls["n"] += 1
        return _fixed("GOOD_MATCH" if calls["n"] <= 2 else "NOT_SUITABLE", job_id)

    monkeypatch.setattr(shortlist_gate, "assess_job", flipping)
    result = _run_shortlist(db, _settings(), [job])
    assert result["counters"]["blocked_by_fit"] == 1
    assert result["counters"]["bewerbung_created"] == 0
    assert _drafts(db)[1] == 0
    db.close()


def test_job_status_and_applications_are_untouched(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db, status="SAVED")
    _tier_by_job(monkeypatch, "NOT_SUITABLE")
    _run_shortlist(db, _settings(), [job])
    db.refresh(job)
    assert job.status == "SAVED"
    assert db.scalar(select(func.count()).select_from(ApplicationRecord)) == 0
    db.close()


def test_cache_is_not_consulted(session_factory, monkeypatch):
    """A missing/stale job_fit_assessments cache can neither admit nor block."""
    import app.services.fit_cache as fit_cache

    def forbidden(*args, **kwargs):
        raise AssertionError("the gate must not read the assessment cache")

    monkeypatch.setattr(fit_cache, "cached_keys", forbidden)
    db = session_factory()
    job = _seed_job(db)
    _tier_by_job(monkeypatch, "GOOD_MATCH")
    assert automatic_preparation_block(db, job.id) is None
    db.close()


def test_gate_module_has_no_send_path():
    src = inspect.getsource(shortlist_gate).lower()
    for word in ("smtp", "send_telegram", "imap", "gmail"):
        assert word not in src


def test_tier_change_blocks_even_an_existing_current_bewerbung(session_factory, monkeypatch):
    """Reuse of an existing Bewerbung draft is gated like creation."""
    db = session_factory()
    job = _seed_job(db)
    _tier_by_job(monkeypatch, "GOOD_MATCH")
    first = _run_shortlist(db, _settings(), [job])
    assert first["counters"]["bewerbung_created"] == 1

    _tier_by_job(monkeypatch, "NOT_SUITABLE")
    second = _run_shortlist(db, _settings(), [job])
    assert second["counters"]["bewerbung_reused"] == 0
    assert second["counters"]["blocked_by_fit"] == 1
    assert second["items"] == []
    db.close()


def test_late_recheck_blocks_reuse_after_the_cv_step(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db)
    _tier_by_job(monkeypatch, "GOOD_MATCH")
    _run_shortlist(db, _settings(), [job])

    calls = {"n": 0}

    def flipping(session, job_id):
        calls["n"] += 1
        return _fixed("GOOD_MATCH" if calls["n"] <= 2 else "NOT_SUITABLE", job_id)

    monkeypatch.setattr(shortlist_gate, "assess_job", flipping)
    result = _run_shortlist(db, _settings(), [job])
    assert result["counters"]["blocked_by_fit"] == 1
    assert result["counters"]["bewerbung_reused"] == 0
    # the late block is audited, with the already committed CV, and is not a failure
    assert [(i["status"], i["phase"], i["bewerbung_draft_id"]) for i in result["items"]] == [
        ("blocked", "fit", None)
    ]
    assert result["items"][0]["cv_draft_id"] is not None
    assert result["counters"]["failed"] == 0
    db.close()


# --- connection isolation -----------------------------------------------------


def test_caller_flushed_write_survives_the_gate(session_factory, monkeypatch):
    db = session_factory()
    job = _seed_job(db)
    job_id = job.id
    job.title = "Edited by the caller"
    db.flush()  # written in the caller's open transaction, not yet committed

    _tier_by_job(monkeypatch, "GOOD_MATCH")
    assert automatic_preparation_block(db, job.id) is None

    def boom(session, job_id):
        raise RuntimeError("x")

    monkeypatch.setattr(shortlist_gate, "assess_job", boom)
    assert automatic_preparation_block(db, job.id) == "ASSESSMENT_UNAVAILABLE"

    db.commit()
    db.close()
    with session_factory() as other:
        assert other.get(JobRecord, job_id).title == "Edited by the caller"


def test_shared_in_memory_connection_fails_closed(monkeypatch):
    """A single shared connection cannot be isolated: the gate holds the job back
    instead of letting its rollback undo the caller's flushed write."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    job = _seed_job(db)
    job.title = "Pending"
    db.flush()
    _tier_by_job(monkeypatch, "GOOD_MATCH")
    assert automatic_preparation_block(db, job.id) == "ASSESSMENT_UNAVAILABLE"
    db.commit()
    assert db.get(JobRecord, job.id).title == "Pending"
    db.close()


def test_bare_connection_bind_fails_closed(session_factory, monkeypatch):
    engine = session_factory.kw["bind"]
    with engine.connect() as connection:
        db = sessionmaker(bind=connection)()
        job = _seed_job(db)
        _tier_by_job(monkeypatch, "GOOD_MATCH")
        assert automatic_preparation_block(db, job.id) == "ASSESSMENT_UNAVAILABLE"
        db.close()


# --- legacy notifier: every attempt is revalidated -----------------------------


def test_notifier_retry_is_revalidated_after_the_backoff(monkeypatch):
    from app.models.job import Job, JobScore
    from app.services import telegram
    from app.services.telegram import TelegramNotifier, TelegramSendOutcome

    sent = []

    async def fake_send(token, chat, text, *, timeout_seconds):
        sent.append(text)
        return TelegramSendOutcome.FAILED

    async def fake_sleep(_seconds):
        verdict["value"] = "FIT_TIER_NOT_SUITABLE"

    verdict = {"value": None}
    monkeypatch.setattr(telegram, "send_telegram_text", fake_send)
    monkeypatch.setattr(telegram.asyncio, "sleep", fake_sleep)
    notifier = TelegramNotifier(bot_token="t", chat_id="c", max_retries=3)
    notifier.guard = lambda: verdict["value"]
    job = Job(source="x", title="IT-Administratoren", company="C", url="https://e.com/1")

    result = asyncio.run(notifier.send_job(job, JobScore(score=90, recommendation="APPLY")))

    assert result is False
    assert len(sent) == 1  # the retry after the backoff was never sent


# --- real SQL isolation ---------------------------------------------------------


def test_isolated_read_uses_its_own_connection_and_keeps_caller_writes(
    session_factory, monkeypatch
):
    db = session_factory()
    job = _seed_job(db)
    job_id = job.id
    job.title = "Edited by the caller"
    db.flush()  # the caller now holds an open write transaction
    caller_connection = db.connection().connection.dbapi_connection
    seen = {}

    def real_read(session, jid):
        seen["connection"] = session.connection().connection.dbapi_connection
        seen["rows"] = session.execute(select(func.count()).select_from(JobRecord)).scalar_one()
        seen["title"] = session.execute(
            select(JobRecord.title).where(JobRecord.id == jid)
        ).scalar_one()
        return _fixed("GOOD_MATCH", jid)

    monkeypatch.setattr(shortlist_gate, "assess_job", real_read)
    assert automatic_preparation_block(db, job_id) is None

    assert seen["connection"] is not caller_connection
    assert seen["rows"] == 1
    # uncommitted caller state is invisible to the isolated reader (committed state only)
    assert seen["title"] != "Edited by the caller"
    db.commit()
    db.close()
    with session_factory() as other:
        assert other.get(JobRecord, job_id).title == "Edited by the caller"
