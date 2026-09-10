"""Stage 8C tests: automatic shortlist + CV/Bewerbung draft preparation,
covering the Codex-review remediation round (S8C-AUDIT-001/002,
S8C-STATUS-001, S8C-CACHE-001, S8C-POOL-001, S8C-BOUND-001).

Mirrors tests/test_automation_lease.py's/test_scheduler_service.py's
approach: a real file-backed SQLite session, no-op collector
monkeypatches (no network I/O), and direct calls into
`app.services.automation.run_automation_cycle` for integration-level
proof plus direct calls into
`app.services.automation_shortlist.prepare_shortlist_drafts` for fast,
precise unit-level proof of the attribution/bound/ranking/threshold/
reuse/audit policies.

**Deterministic match-score tiers, no candidate profile setup needed.**
With the default EMPTY candidate profile (no confirmed skills/experience),
`app.agents.candidate_job_matcher.compute_match`'s score formula (see that
module's own docstring) yields a fully deterministic `overall_score` for
a job from ONLY its `must_have_skills_json`/`nice_to_have_skills_json`
shape, independent of any specific skill names:

* both empty (`[]`, `[]`)              -> overall_score 60
* must_have=["python","docker"], nice=[] -> overall_score 20
* must_have=[], nice=["something"]      -> overall_score 40
* must_have=["python"], nice=["docker"] -> overall_score 0

These four tiers give every test below full control over shortlist
threshold/ranking behavior without ever touching the candidate profile.

**Exact attribution.** Candidate eligibility is no longer inferred from
`JobRecord.last_seen_at` -- tests build `TouchedJob` entries explicitly
(the `_touch`/`_run_shortlist` helpers below) exactly like
`app.services.collector_runner.run_bundesagentur`/`run_xing` do after a
real successful persist.
"""

import asyncio
import inspect
import json
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.base import CollectorError, CollectorNotConfiguredError
from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.models import (
    ApplicationPackageReviewRecord,
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    CandidateJobMatchRecord,
    JobRecord,
)
from app.models.automation import AutomationRun, AutomationRunStepResult
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.models.job import Job
from app.services.automation import run_automation_cycle
from app.services.automation_shortlist import prepare_shortlist_drafts
from app.services.candidate_preparation import (
    prepare_candidate_cv_draft_with_outcome,
    prepare_candidate_job_match,
)
from app.services.collector_runner import TouchedJob, run_bundesagentur

ACCOUNT = "me@example.com"

TIER_60 = ("[]", "[]")
TIER_40 = ("[]", '["something"]')
TIER_20 = ('["python", "docker"]', "[]")
TIER_0 = ('["python"]', '["docker"]')

_fp_counter = 0


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_automation_shortlist.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


async def _noop_collector(db, settings, *, touched_jobs=None, is_lease_lost=None):
    return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}


def _failing_collector(exc: Exception):
    async def _fail(db, settings, *, touched_jobs=None, is_lease_lost=None):
        raise exc

    return _fail


def _seed_job(db, *, tier=TIER_60, **overrides) -> JobRecord:
    global _fp_counter
    _fp_counter += 1
    now = datetime.now(UTC)
    must_have, nice_to_have = tier
    data = {
        "fingerprint": f"fp-{_fp_counter}",
        "source": "bundesagentur",
        "title": "Software Engineer",
        "company": "Example GmbH",
        "location": "Berlin",
        "url": f"https://careers.example.com/jobs/{_fp_counter}",
        "description": "General remote software engineering position.",
        "skills_json": "[]",
        "data_confidence": 0.9,
        "must_have_skills_json": must_have,
        "nice_to_have_skills_json": nice_to_have,
        "score": 50,
        "recommendation": "APPLY",
        "status": "NEW",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    data.update(overrides)
    record = JobRecord(**data)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _touch(job: JobRecord) -> TouchedJob:
    return TouchedJob(
        job_id=job.id, score=job.score, status=job.status, recommendation=job.recommendation
    )


def _settings(**overrides) -> Settings:
    data = {
        "automation_auto_prepare_enabled": True,
        "automation_shortlist_min_match_score": 0,
        "automation_shortlist_max_per_run": 10,
        "automation_candidate_match_max_per_run": 100,
    }
    data.update(overrides)
    return Settings(**data)


def _run_shortlist(db, settings, jobs) -> dict:
    touched = [_touch(job) for job in jobs]
    return asyncio.run(prepare_shortlist_drafts(db, touched_jobs=touched, settings=settings))


def _run_shortlist_raw(db, settings, touched_jobs) -> dict:
    return asyncio.run(prepare_shortlist_drafts(db, touched_jobs=touched_jobs, settings=settings))


# --- Disabled by default / unaffected when off -------------------------------


class TestDisabledByDefault:
    def test_default_settings_never_triggers_shortlist_step(self, session_factory, monkeypatch):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        try:
            _seed_job(db, tier=TIER_60)

            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=Settings()))

            assert run.status == "COMPLETED"
            results = json.loads(run.results_json)
            assert set(results.keys()) == {"bundesagentur", "xing"}
            assert "shortlist_drafts" not in results

            assert db.query(CandidateJobMatchRecord).count() == 0
            assert db.query(CandidateCVDraftRecord).count() == 0
            assert db.query(BewerbungDraftRecord).count() == 0
        finally:
            db.close()

    def test_enabled_scheduler_alone_does_not_imply_auto_prepare(self, session_factory):
        assert Settings().automation_auto_prepare_enabled is False


# --- S8C-STATUS-001: overall status aggregation -----------------------------


class TestOverallStatusAggregation:
    def test_both_collectors_failed_stage8c_enabled_no_candidates_is_failed(
        self, session_factory, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.automation.run_bundesagentur",
            _failing_collector(CollectorError("boom")),
        )
        monkeypatch.setattr(
            "app.services.automation.run_xing", _failing_collector(CollectorError("boom"))
        )

        db = session_factory()
        try:
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))
            assert run.status == "FAILED"
            results = json.loads(run.results_json)
            assert results["shortlist_drafts"]["status"] == "ok"
            assert results["shortlist_drafts"]["counters"]["candidate_jobs"] == 0
        finally:
            db.close()

    def test_both_collectors_not_configured_stage8c_enabled_is_failed(
        self, session_factory, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.automation.run_bundesagentur",
            _failing_collector(CollectorNotConfiguredError("not configured")),
        )
        monkeypatch.setattr(
            "app.services.automation.run_xing",
            _failing_collector(CollectorNotConfiguredError("not configured")),
        )

        db = session_factory()
        try:
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))
            assert run.status == "FAILED"
        finally:
            db.close()

    def test_one_success_one_failure_shortlist_ok_is_partial(self, session_factory, monkeypatch):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr(
            "app.services.automation.run_xing", _failing_collector(CollectorError("boom"))
        )

        db = session_factory()
        try:
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))
            assert run.status == "PARTIAL"
        finally:
            db.close()

    def test_both_success_shortlist_ok_is_completed(self, session_factory, monkeypatch):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        try:
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))
            assert run.status == "COMPLETED"
        finally:
            db.close()

    def test_both_success_shortlist_partial_is_partial(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            failing_job = _seed_job(db, tier=TIER_60, score=99)
            healthy_job = _seed_job(db, tier=TIER_60, score=1)

            async def _touching_bundesagentur(
                db, settings, *, touched_jobs=None, is_lease_lost=None
            ):
                if touched_jobs is not None:
                    touched_jobs.append(_touch(failing_job))
                    touched_jobs.append(_touch(healthy_job))
                return await _noop_collector(db, settings)

            monkeypatch.setattr(
                "app.services.automation.run_bundesagentur", _touching_bundesagentur
            )
            monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

            import app.services.automation_shortlist as shortlist_module

            original = shortlist_module.prepare_candidate_cv_draft_with_outcome

            def _boom_for_failing_job(db, job_id, match_id, *, force_recompute):
                if job_id == failing_job.id:
                    raise RuntimeError("boom")
                return original(db, job_id, match_id, force_recompute=force_recompute)

            monkeypatch.setattr(
                "app.services.automation_shortlist.prepare_candidate_cv_draft_with_outcome",
                _boom_for_failing_job,
            )

            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))
            assert run.status == "PARTIAL"
            results = json.loads(run.results_json)
            assert results["shortlist_drafts"]["status"] == "partial"
        finally:
            db.close()


# --- Ranking ------------------------------------------------------------------


class TestRanking:
    def test_ranked_by_match_score_desc_then_job_score_desc_then_id_asc(self, session_factory):
        db = session_factory()
        try:
            low = _seed_job(db, tier=TIER_20, score=10)
            high_a = _seed_job(db, tier=TIER_60, score=50)
            high_b_lower_job_score = _seed_job(db, tier=TIER_60, score=30)
            mid = _seed_job(db, tier=TIER_40, score=99)

            settings = _settings()
            result = _run_shortlist(db, settings, [low, high_a, high_b_lower_job_score, mid])

            job_ids_in_order = [item["job_id"] for item in result["items"]]
            assert job_ids_in_order == [high_a.id, high_b_lower_job_score.id, mid.id, low.id]
        finally:
            db.close()

    def test_tie_break_on_job_id_ascending_when_scores_are_identical(self, session_factory):
        db = session_factory()
        try:
            job_a = _seed_job(db, tier=TIER_60, score=50)
            job_b = _seed_job(db, tier=TIER_60, score=50)
            job_c = _seed_job(db, tier=TIER_60, score=50)

            settings = _settings()
            result = _run_shortlist(db, settings, [job_a, job_b, job_c])

            job_ids_in_order = [item["job_id"] for item in result["items"]]
            assert job_ids_in_order == sorted([job_a.id, job_b.id, job_c.id])
        finally:
            db.close()

    def test_max_per_run_caps_the_shortlist(self, session_factory):
        db = session_factory()
        try:
            jobs = [_seed_job(db, tier=TIER_60) for _ in range(5)]

            settings = _settings(automation_shortlist_max_per_run=2)
            result = _run_shortlist(db, settings, jobs)

            assert result["counters"]["matched"] == 5
            assert result["counters"]["shortlisted"] == 2
            assert len(result["items"]) == 2
        finally:
            db.close()


# --- Threshold ------------------------------------------------------------------


class TestThreshold:
    def test_below_threshold_gets_no_cv_or_bewerbung(self, session_factory):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_20)  # score 20, below 50

            settings = _settings(automation_shortlist_min_match_score=50)
            result = _run_shortlist(db, settings, [job])

            assert result["counters"]["matched"] == 1
            assert result["counters"]["shortlisted"] == 0
            assert result["items"] == []
            assert db.query(CandidateCVDraftRecord).count() == 0
            assert db.query(BewerbungDraftRecord).count() == 0
        finally:
            db.close()

    def test_threshold_boundary_itself_is_included(self, session_factory):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)  # score exactly 60

            settings = _settings(automation_shortlist_min_match_score=60)
            result = _run_shortlist(db, settings, [job])

            assert result["counters"]["shortlisted"] == 1
            assert result["items"][0]["job_id"] == job.id
            assert result["items"][0]["match_score"] == 60
        finally:
            db.close()


# --- Match reuse ------------------------------------------------------------


class TestMatchReuse:
    def test_second_cycle_reuses_the_cached_match_no_forced_recompute(
        self, session_factory, monkeypatch
    ):
        call_count = 0
        import app.services.candidate_preparation as prep_module

        original = prep_module.compute_match

        def _counting_compute_match(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.services.candidate_preparation.compute_match", _counting_compute_match
        )

        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            settings = _settings()
            _run_shortlist(db, settings, [job])
            _run_shortlist(db, settings, [job])

            assert call_count == 1
            assert db.query(CandidateJobMatchRecord).count() == 1
        finally:
            db.close()


# --- CV reuse -----------------------------------------------------------------


class TestCVReuse:
    def test_second_cycle_reuses_the_cached_cv_draft(self, session_factory, monkeypatch):
        call_count = 0
        import app.services.candidate_preparation as prep_module

        original = prep_module.compute_cv_draft

        def _counting_compute_cv_draft(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.services.candidate_preparation.compute_cv_draft", _counting_compute_cv_draft
        )

        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            settings = _settings()
            first = _run_shortlist(db, settings, [job])
            second = _run_shortlist(db, settings, [job])

            assert call_count == 1
            assert db.query(CandidateCVDraftRecord).count() == 1
            assert first["items"][0]["cv_reused"] is False
            assert second["items"][0]["cv_reused"] is True
        finally:
            db.close()


# --- S8C-CACHE-001: race-safe CV created/reused -----------------------------


def _make_barrier_synced_get_cached_draft(original, barrier: threading.Barrier):
    """S8C-TEST-001 (Codex re-review): identical technique to
    tests/test_automation_schedule_repository.py's own
    `_make_barrier_synced_get_schedule` helper (see that module's
    docstring for the full rationale) -- a THIN, test-only wrapper around
    the REAL `get_cached_draft` (never a reimplementation of the CAS/
    UNIQUE-constraint logic itself, never a change to production code).
    `prepare_candidate_cv_draft_with_outcome` calls `get_cached_draft`
    exactly once, as its pre-check before deciding whether to attempt an
    INSERT -- wrapping THAT one call site is enough to force "both racers
    observe the identical empty-cache starting state before either
    writes", without touching a single line of `create_draft`'s actual
    INSERT-or-reload logic under test. Tracked per-thread (like the
    Stage 8B helper) so a later, unrelated call from the same thread
    (there isn't one here, but the pattern is kept identical for
    consistency/safety) can never deadlock waiting for a second
    rendezvous nobody else is coming to.
    """
    waited_thread_ids: set[int] = set()
    lock = threading.Lock()

    def _wrapped(db, *, match_id, cv_adapter_version):
        result = original(db, match_id=match_id, cv_adapter_version=cv_adapter_version)
        ident = threading.get_ident()
        with lock:
            first_call_from_this_thread = ident not in waited_thread_ids
            waited_thread_ids.add(ident)
        if first_call_from_this_thread:
            barrier.wait(timeout=10)
        return result

    return _wrapped


class TestCVRaceSafety:
    def test_lost_unique_race_is_reported_as_reused_not_created(self, tmp_path):
        """S8C-TEST-001 (Codex re-review): genuine two-real-thread,
        two-real-Session race against a real UNIQUE constraint -- NOT a
        forced return value. Both threads call the REAL Stage 8C CV
        service path (`prepare_candidate_cv_draft_with_outcome`, which
        `app.services.automation_shortlist.prepare_shortlist_drafts`
        itself calls) with the SAME (match_id, cv_adapter_version) cache
        identity. The barrier forces both threads' `get_cached_draft`
        pre-check to return "no row yet" before EITHER proceeds to its
        own `create_draft` INSERT, so one thread's commit genuinely wins
        the DB's own `uq_candidate_cv_drafts_cache_identity` UNIQUE
        constraint and the other's INSERT genuinely raises
        `sqlalchemy.exc.IntegrityError`, caught by `create_draft`'s own
        (unmodified) `except IntegrityError: db.rollback(); reload
        winner` branch.
        """
        db_path = tmp_path / "test_cv_draft_race.db"
        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        # Single-writer setup, deliberately OUTSIDE the race: seed the job
        # and its match first, so the race below exercises ONLY the CV
        # draft's own cache-identity race (match caching has its own,
        # separately-proven race handling -- not what S8C-TEST-001 is
        # about).
        setup_db = factory()
        job = _seed_job(setup_db, tier=TIER_60)
        match = prepare_candidate_job_match(setup_db, job.id, force_recompute=False)
        job_id = job.id
        match_id = match.id
        setup_db.close()

        verify_before = factory()
        assert verify_before.query(CandidateCVDraftRecord).count() == 0  # cache genuinely empty
        verify_before.close()

        barrier = threading.Barrier(2)
        results: dict[str, tuple[str, object]] = {}

        def _worker(name, db):
            try:
                outcome = prepare_candidate_cv_draft_with_outcome(
                    db, job_id, match_id, force_recompute=False
                )
                results[name] = ("ok", outcome)
            except Exception as exc:
                db.rollback()
                results[name] = ("error", exc)

        session_a = factory()
        session_b = factory()

        import app.services.candidate_preparation as prep_module

        original_get_cached_draft = prep_module.get_cached_draft
        wrapped = _make_barrier_synced_get_cached_draft(original_get_cached_draft, barrier)
        prep_module.get_cached_draft = wrapped
        try:
            thread_a = threading.Thread(target=_worker, args=("a", session_a))
            thread_b = threading.Thread(target=_worker, args=("b", session_b))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=15)
            thread_b.join(timeout=15)
        finally:
            prep_module.get_cached_draft = original_get_cached_draft

        try:
            # Prove BOTH racers actually finished and BOTH produced a
            # result before evaluating anything below (S8B-TEST-001-R1
            # convention) -- a timed join() alone does not guarantee this.
            assert not thread_a.is_alive(), "thread_a did not terminate within the join timeout"
            assert not thread_b.is_alive(), "thread_b did not terminate within the join timeout"
            assert set(results) == {"a", "b"}, (
                f"both racers must report a result before evaluating outcomes -- "
                f"got {sorted(results)}"
            )
            assert all(status == "ok" for status, _ in results.values()), results

            # Exactly one durable row for this cache identity -- no
            # duplicate CV draft, regardless of which thread "won".
            verify_after = factory()
            try:
                row_count = verify_after.query(CandidateCVDraftRecord).count()
                assert row_count == 1
                canonical = verify_after.query(CandidateCVDraftRecord).one()
            finally:
                verify_after.close()

            (_, (draft_a, created_a)) = results["a"]
            (_, (draft_b, created_b)) = results["b"]

            # Exactly one winner (created=True), exactly one loser
            # (created=False) -- this is create_draft's OWN real
            # INSERT-or-reload outcome, never forced by this test.
            assert sorted([created_a, created_b]) == [False, True]
            assert draft_a.id == canonical.id
            assert draft_b.id == canonical.id  # the loser reloaded the SAME winner row

            loser_draft, loser_created = (
                (draft_a, created_a) if not created_a else (draft_b, created_b)
            )
            assert loser_created is False
        finally:
            session_a.close()
            session_b.close()

        # Stage 8C's own real, unmodified mapping (app.services.
        # automation_shortlist.prepare_shortlist_drafts) must now report
        # this as a REUSE for a fresh automation cycle over the same job
        # -- proven by actually calling it, not by re-deriving the
        # created/reused boolean ourselves. No new race here (the CV
        # cache is already durably populated from above): this exercises
        # the ordinary cache-hit path with the loser's OWN, real, already
        # -durable row id.
        stage8c_db = factory()
        try:
            settings = _settings()
            fresh_job = stage8c_db.get(JobRecord, job_id)
            result = _run_shortlist(stage8c_db, settings, [fresh_job])

            assert result["counters"]["cv_created"] == 0
            assert result["counters"]["cv_reused"] == 1
            assert result["items"][0]["cv_reused"] is True
            assert result["items"][0]["cv_draft_id"] == canonical.id

            # No Bewerbung duplication caused by this test: exactly one
            # BewerbungDraftRecord exists after this single, sequential,
            # non-racing Stage 8C cycle.
            assert stage8c_db.query(BewerbungDraftRecord).count() == 1
        finally:
            stage8c_db.close()


# --- Bewerbung reuse ------------------------------------------------------


class TestBewerbungReuse:
    def test_repeated_cycle_with_unchanged_inputs_creates_no_extra_bewerbung(self, session_factory):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            settings = _settings()
            first = _run_shortlist(db, settings, [job])
            second = _run_shortlist(db, settings, [job])
            third = _run_shortlist(db, settings, [job])

            assert db.query(BewerbungDraftRecord).count() == 1
            assert first["counters"]["bewerbung_created"] == 1
            assert first["counters"]["bewerbung_reused"] == 0
            assert second["counters"]["bewerbung_created"] == 0
            assert second["counters"]["bewerbung_reused"] == 1
            assert third["counters"]["bewerbung_created"] == 0
            assert third["counters"]["bewerbung_reused"] == 1
            first_bewerbung_id = first["items"][0]["bewerbung_draft_id"]
            second_bewerbung_id = second["items"][0]["bewerbung_draft_id"]
            assert first_bewerbung_id == second_bewerbung_id
        finally:
            db.close()

    def test_integration_via_run_automation_cycle_does_not_spam_bewerbung(
        self, session_factory, monkeypatch
    ):
        """End-to-end proof through the real scheduler-triggered path
        (run_automation_cycle), not just the unit-level
        prepare_shortlist_drafts call above -- a job the (fake) collector
        touches every cycle must still only ever get ONE
        BewerbungDraftRecord across multiple automation cycles.
        """
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)
            job_id = job.id

            async def _touch_job_collector(db, settings, *, touched_jobs=None, is_lease_lost=None):
                record = db.get(JobRecord, job_id)
                if touched_jobs is not None:
                    touched_jobs.append(_touch(record))
                return await _noop_collector(db, settings)

            monkeypatch.setattr("app.services.automation.run_bundesagentur", _touch_job_collector)
            monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

            settings = _settings()

            run1 = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=settings))
            run2 = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=settings))

            assert run1.status == "COMPLETED"
            assert run2.status == "COMPLETED"
            assert db.query(BewerbungDraftRecord).count() == 1
            results1 = json.loads(run1.results_json)
            results2 = json.loads(run2.results_json)
            assert results1["shortlist_drafts"]["counters"]["bewerbung_created"] == 1
            assert results2["shortlist_drafts"]["counters"]["bewerbung_reused"] == 1
        finally:
            db.close()


# --- Changed inputs -----------------------------------------------------


class TestChangedInputsForceRegeneration:
    def test_candidate_profile_version_change_forces_new_match_cv_and_bewerbung(
        self, session_factory
    ):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            settings = _settings()
            first = _run_shortlist(db, settings, [job])

            apply_candidate_profile_patch(
                db, CandidateProfilePatchRequest(expected_profile_version=1, first_name="Anna")
            )

            second = _run_shortlist(db, settings, [job])

            assert db.query(CandidateJobMatchRecord).count() == 2
            assert db.query(CandidateCVDraftRecord).count() == 2
            assert db.query(BewerbungDraftRecord).count() == 2
            assert first["items"][0]["match_id"] != second["items"][0]["match_id"]
            assert first["items"][0]["cv_draft_id"] != second["items"][0]["cv_draft_id"]
            assert (
                first["items"][0]["bewerbung_draft_id"] != second["items"][0]["bewerbung_draft_id"]
            )
            assert second["counters"]["bewerbung_created"] == 1
            assert second["counters"]["bewerbung_reused"] == 0
        finally:
            db.close()

    def test_job_snapshot_change_forces_new_match_cv_and_bewerbung(self, session_factory):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            settings = _settings()
            first = _run_shortlist(db, settings, [job])

            job.must_have_skills_json = '["python"]'
            db.add(job)
            db.commit()

            second = _run_shortlist(db, settings, [job])

            assert db.query(CandidateJobMatchRecord).count() == 2
            assert db.query(CandidateCVDraftRecord).count() == 2
            assert db.query(BewerbungDraftRecord).count() == 2
            assert first["items"][0]["match_id"] != second["items"][0]["match_id"]
            assert second["counters"]["bewerbung_created"] == 1
            assert second["counters"]["bewerbung_reused"] == 0
        finally:
            db.close()


# --- S8C-AUDIT-001: truthful CV audit on Bewerbung failure ------------------


class TestTruthfulCVAudit:
    def test_cv_committed_then_bewerbung_raises_is_reported_truthfully(
        self, session_factory, monkeypatch, caplog
    ):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            def _boom(db, job_id, cv_draft_id):
                raise RuntimeError("secret-provider-detail")

            monkeypatch.setattr("app.services.automation_shortlist.prepare_bewerbung_draft", _boom)

            settings = _settings()
            with caplog.at_level("DEBUG"):
                result = _run_shortlist(db, settings, [job])

            # The CV draft really is durable.
            assert db.query(CandidateCVDraftRecord).count() == 1
            real_cv_draft_id = db.query(CandidateCVDraftRecord).one().id

            item = result["items"][0]
            assert item["cv_draft_id"] == real_cv_draft_id
            assert item["cv_reused"] is False  # truthfully CREATED, not reused
            assert result["counters"]["cv_created"] == 1
            assert result["counters"]["cv_reused"] == 0

            assert db.query(BewerbungDraftRecord).count() == 0
            assert item["bewerbung_draft_id"] is None
            assert item["status"] == "failed"
            assert item["phase"] == "bewerbung"
            assert item["error_type"] == "RuntimeError"

            assert result["counters"]["failed"] == 1
            # Only one candidate job existed and it failed -- per the
            # step-status rule ("all bounded candidates fail: step =
            # failed"), this is correctly "failed", not "partial" (the
            # already-committed CV draft above proves the audit is
            # truthful regardless of this step-level label).
            assert result["status"] == "failed"

            failure = result["failures"][0]
            assert failure["job_id"] == job.id
            assert failure["phase"] == "bewerbung"
            assert failure["error_type"] == "RuntimeError"

            assert "secret-provider-detail" not in caplog.text
            assert "secret-provider-detail" not in json.dumps(result)
        finally:
            db.close()


# --- S8C-AUDIT-002: persisted match failure traceability --------------------


class TestMatchFailureTraceability:
    def test_match_failure_is_persisted_with_job_id_phase_and_error_type(
        self, session_factory, monkeypatch
    ):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            def _boom(db, job_id, *, force_recompute):
                raise RuntimeError("another-secret-detail")

            monkeypatch.setattr(
                "app.services.automation_shortlist.prepare_candidate_job_match", _boom
            )

            settings = _settings()
            result = _run_shortlist(db, settings, [job])

            assert result["items"] == []  # never became a shortlist candidate
            assert result["counters"]["failed"] == 1
            assert result["counters"]["matched"] == 0

            failure = result["failures"][0]
            assert failure["job_id"] == job.id
            assert failure["phase"] == "match"
            assert failure["error_type"] == "RuntimeError"
            assert "another-secret-detail" not in json.dumps(result)
        finally:
            db.close()


# --- S8C-POOL-001: exact run attribution -------------------------------------


class TestExactRunAttribution:
    def test_job_not_touched_by_this_run_is_excluded_even_if_eligible(self, session_factory):
        db = session_factory()
        try:
            untouched = _seed_job(db, tier=TIER_60)  # eligible in DB, but never "touched"
            touched = _seed_job(db, tier=TIER_60)

            settings = _settings()
            result = _run_shortlist(db, settings, [touched])  # only `touched` is in the trace

            job_ids = {item["job_id"] for item in result["items"]}
            assert touched.id in job_ids
            assert untouched.id not in job_ids
            assert result["counters"]["candidate_jobs"] == 1
        finally:
            db.close()

    def test_job_actually_touched_by_this_run_is_eligible(self, session_factory):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            settings = _settings()
            result = _run_shortlist(db, settings, [job])

            assert result["counters"]["candidate_jobs"] == 1
            assert result["items"][0]["job_id"] == job.id
        finally:
            db.close()

    def test_db_revalidation_excludes_a_job_whose_status_changed_after_touch(self, session_factory):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)
            touched = [_touch(job)]  # captured while still NEW

            # A human (or another process) moves it to APPLIED before
            # Stage 8C actually runs its DB revalidation.
            job.status = "APPLIED"
            db.add(job)
            db.commit()

            settings = _settings()
            result = _run_shortlist_raw(db, settings, touched)

            assert result["counters"]["candidate_jobs"] == 0
            assert result["items"] == []
        finally:
            db.close()


# --- S8C-POOL-001: collector trace correctness -------------------------------


def _ba_job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "Example GmbH",
        "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1184867112-S",
        "description": "General remote software engineering position.",
        "source_reference": "10000-1184867112-S",
        "skills": ["python"],
    }
    data.update(overrides)
    return Job(**data)


class _FakeBundesagenturCollector:
    def __init__(self, jobs):
        self._jobs = jobs
        self.skipped_invalid_count = 0

    async def fetch(self, since=None):
        return self._jobs

    async def fetch_detail(self, source_reference):
        return None


class TestCollectorTrace:
    def test_persisted_job_is_recorded_and_failed_job_is_not(self, session_factory, monkeypatch):
        good_job = _ba_job(title="Good Job", url="https://example.com/good")
        bad_job = _ba_job(title="Bad Job", url="https://example.com/bad")

        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: _FakeBundesagenturCollector([good_job, bad_job]),
        )
        monkeypatch.setattr("app.services.collector_runner.is_api_key_configured", lambda key: True)

        import app.services.collector_runner as runner_module

        original_score_and_persist = runner_module.score_and_persist

        def _fail_for_bad_job(db, profile, job):
            if job.title == "Bad Job":
                raise RuntimeError("boom")
            return original_score_and_persist(db, profile, job)

        monkeypatch.setattr("app.services.collector_runner.score_and_persist", _fail_for_bad_job)

        db = session_factory()
        try:
            settings = Settings(bundesagentur_api_key="key")
            touched: list[TouchedJob] = []
            counters = asyncio.run(run_bundesagentur(db, settings, touched_jobs=touched))

            assert counters["created"] == 1
            assert counters["failed"] == 1
            assert len(touched) == 1

            persisted_job = db.query(JobRecord).filter(JobRecord.title == "Good Job").one()
            assert touched[0].job_id == persisted_job.id
        finally:
            db.close()

    def test_notifier_and_research_failures_do_not_erase_an_already_recorded_touch(
        self, session_factory, monkeypatch
    ):
        job = _ba_job(title="High Score Job", url="https://example.com/high")

        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: _FakeBundesagenturCollector([job]),
        )
        monkeypatch.setattr("app.services.collector_runner.is_api_key_configured", lambda key: True)

        async def _boom_notify(self, job, score):
            raise RuntimeError("telegram-boom")

        monkeypatch.setattr("app.services.collector_runner.TelegramNotifier.send_job", _boom_notify)

        class _BoomCompanyResearchService:
            async def get_or_run(self, db, record, settings):
                raise RuntimeError("research-boom")

        monkeypatch.setattr(
            "app.services.collector_runner.CompanyResearchService", _BoomCompanyResearchService
        )

        db = session_factory()
        try:
            settings = Settings(
                bundesagentur_api_key="key",
                min_job_score_to_notify=0,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                company_research_auto_enabled=True,
            )
            touched: list[TouchedJob] = []
            counters = asyncio.run(run_bundesagentur(db, settings, touched_jobs=touched))

            assert counters["created"] == 1
            assert len(touched) == 1
        finally:
            db.close()

    def test_existing_callers_omitting_touched_jobs_are_unaffected(
        self, session_factory, monkeypatch
    ):
        job = _ba_job(title="Untouched Sink Job", url="https://example.com/untouched")
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: _FakeBundesagenturCollector([job]),
        )
        monkeypatch.setattr("app.services.collector_runner.is_api_key_configured", lambda key: True)

        db = session_factory()
        try:
            settings = Settings(bundesagentur_api_key="key")
            counters = asyncio.run(run_bundesagentur(db, settings))  # no touched_jobs kwarg
            assert counters["created"] == 1
        finally:
            db.close()


# --- S8C-BOUND-001: bound matching work --------------------------------------


class TestCandidateMatchBound:
    def test_match_calls_never_exceed_the_configured_bound(self, session_factory, monkeypatch):
        call_count = 0
        import app.services.automation_shortlist as shortlist_module

        original = shortlist_module.prepare_candidate_job_match

        def _counting(db, job_id, *, force_recompute):
            nonlocal call_count
            call_count += 1
            return original(db, job_id, force_recompute=force_recompute)

        monkeypatch.setattr(
            "app.services.automation_shortlist.prepare_candidate_job_match", _counting
        )

        db = session_factory()
        try:
            jobs = [_seed_job(db, tier=TIER_60, score=i) for i in range(10)]

            settings = _settings(
                automation_candidate_match_max_per_run=3, automation_shortlist_max_per_run=3
            )
            result = _run_shortlist(db, settings, jobs)

            assert call_count <= 3
            assert result["counters"]["candidate_jobs"] <= 3

            # Deterministic preselection: the top-3 by JobRecord.score
            # DESC (id ASC tie-break) must be exactly what got matched --
            # jobs were seeded with score=0..9, so the top 3 are the last
            # three seeded (score 9, 8, 7).
            expected_ids = sorted([jobs[9].id, jobs[8].id, jobs[7].id])
            matched_job_ids = sorted(item["job_id"] for item in result["items"])
            assert matched_job_ids == expected_ids
        finally:
            db.close()

    def test_bound_is_deterministic_across_repeated_calls(self, session_factory):
        db = session_factory()
        try:
            jobs = [_seed_job(db, tier=TIER_60, score=i) for i in range(6)]

            settings = _settings(
                automation_candidate_match_max_per_run=2, automation_shortlist_max_per_run=2
            )
            first = _run_shortlist(db, settings, jobs)
            db.rollback()
            second = _run_shortlist(db, settings, jobs)

            first_ids = sorted(item["job_id"] for item in first["items"])
            second_ids = sorted(item["job_id"] for item in second["items"])
            assert first_ids == second_ids
        finally:
            db.close()


# --- Failure isolation ----------------------------------------------------


class TestFailureIsolation:
    def test_one_shortlisted_job_raising_does_not_block_the_rest(
        self, session_factory, monkeypatch, caplog
    ):
        db = session_factory()
        try:
            failing_job = _seed_job(db, tier=TIER_60, score=99)
            healthy_job = _seed_job(db, tier=TIER_60, score=1)

            import app.services.automation_shortlist as shortlist_module

            original = shortlist_module.prepare_candidate_cv_draft_with_outcome

            def _boom_for_failing_job(db, job_id, match_id, *, force_recompute):
                if job_id == failing_job.id:
                    raise RuntimeError("secret-upstream-detail")
                return original(db, job_id, match_id, force_recompute=force_recompute)

            monkeypatch.setattr(
                "app.services.automation_shortlist.prepare_candidate_cv_draft_with_outcome",
                _boom_for_failing_job,
            )

            settings = _settings()
            with caplog.at_level("DEBUG"):
                result = _run_shortlist(db, settings, [failing_job, healthy_job])

            assert result["status"] == "partial"
            assert result["counters"]["failed"] == 1
            assert result["counters"]["shortlisted"] == 2

            by_job_id = {item["job_id"]: item for item in result["items"]}
            assert by_job_id[failing_job.id]["status"] == "failed"
            assert by_job_id[failing_job.id]["phase"] == "cv"
            assert by_job_id[failing_job.id]["error_type"] == "RuntimeError"
            assert by_job_id[failing_job.id]["cv_draft_id"] is None
            assert by_job_id[healthy_job.id]["status"] == "ok"
            assert by_job_id[healthy_job.id]["cv_draft_id"] is not None

            assert db.query(CandidateCVDraftRecord).count() == 1
            assert db.query(BewerbungDraftRecord).count() == 1

            assert "secret-upstream-detail" not in caplog.text
        finally:
            db.close()

    def test_step_result_never_contains_raw_exception_text(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            def _boom(db, job_id, match_id, *, force_recompute):
                raise RuntimeError("another-secret-detail-xyz")

            monkeypatch.setattr(
                "app.services.automation_shortlist.prepare_candidate_cv_draft_with_outcome", _boom
            )

            settings = _settings()
            result = _run_shortlist(db, settings, [job])

            serialized = json.dumps(result)
            assert "another-secret-detail-xyz" not in serialized
            assert result["items"][0]["error_type"] == "RuntimeError"
            assert result["items"][0]["job_id"] == job.id
        finally:
            db.close()


# --- Job status safety ---------------------------------------------------


class TestJobStatusSafety:
    def test_new_and_saved_status_are_never_mutated_by_shortlisting(self, session_factory):
        db = session_factory()
        try:
            new_job = _seed_job(db, tier=TIER_60, status="NEW")
            saved_job = _seed_job(db, tier=TIER_60, status="SAVED")

            settings = _settings()
            result = _run_shortlist(db, settings, [new_job, saved_job])

            assert result["counters"]["shortlisted"] == 2

            db.expire_all()
            assert db.get(JobRecord, new_job.id).status == "NEW"
            assert db.get(JobRecord, saved_job.id).status == "SAVED"
        finally:
            db.close()


# --- Human approval safety -------------------------------------------------


class TestSafetyNoSendOrApprovalImports:
    def test_automation_shortlist_module_never_imports_send_or_approval_logic(self):
        import app.services.automation_shortlist as module

        source = inspect.getsource(module)
        for forbidden in (
            "smtp",
            "SMTP",
            "send_follow_up",
            "send_response",
            "approve_or_reject",
            "bewerbung_send",
            "TelegramNotifier(",
            "update_job_status",
            "ReviewPackageService",
        ):
            assert forbidden not in source

    def test_candidate_preparation_module_never_imports_send_or_approval_logic(self):
        import app.services.candidate_preparation as module

        source = inspect.getsource(module)
        for forbidden in (
            "smtp",
            "SMTP",
            "send_follow_up",
            "send_response",
            "approve_or_reject",
            "bewerbung_send",
            "TelegramNotifier(",
            "update_job_status",
            "ReviewPackageService",
        ):
            assert forbidden not in source

    def test_shortlist_run_creates_zero_review_or_send_side_effects(self, session_factory):
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60)

            settings = _settings()
            result = _run_shortlist(db, settings, [job])

            assert result["counters"]["shortlisted"] == 1
            assert db.query(ApplicationPackageReviewRecord).count() == 0
        finally:
            db.close()


# --- Backward compatibility --------------------------------------------------


class TestBackwardCompatibility:
    def test_old_collector_only_results_without_items_or_failures_still_deserialize(self):
        old_style_results = {
            "bundesagentur": {
                "status": "ok",
                "counters": {
                    "fetched": 1,
                    "created": 1,
                    "updated": 0,
                    "skipped_invalid": 0,
                    "failed": 0,
                },
                "error_type": None,
            },
            "xing": {
                "status": "not_configured",
                "counters": None,
                "error_type": "CollectorNotConfiguredError",
            },
        }
        parsed = {
            name: AutomationRunStepResult(**payload) for name, payload in old_style_results.items()
        }
        assert parsed["bundesagentur"].items is None
        assert parsed["bundesagentur"].failures is None
        assert parsed["xing"].status == "not_configured"

        run = AutomationRun(
            id=1,
            account_key=ACCOUNT,
            status="COMPLETED",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:01:00+00:00",
            results=old_style_results,
            error_summary=None,
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert run.results["bundesagentur"].items is None
        assert run.results["bundesagentur"].failures is None
