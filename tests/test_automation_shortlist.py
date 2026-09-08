"""Stage 8C tests: automatic shortlist + CV/Bewerbung draft preparation.

Mirrors tests/test_automation_lease.py's/test_scheduler_service.py's
approach: a real file-backed SQLite session, no-op collector
monkeypatches (no network I/O), and direct calls into
`app.services.automation.run_automation_cycle` for integration-level
proof plus direct calls into
`app.services.automation_shortlist.prepare_shortlist_drafts`/
`app.db.repositories.get_current_cycle_candidate_jobs` for fast,
precise unit-level proof of the pool/ranking/threshold/reuse policies.

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
"""

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import apply_candidate_profile_patch
from app.db.models import (
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    CandidateJobMatchRecord,
    JobRecord,
)
from app.db.repositories import get_current_cycle_candidate_jobs
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.services.automation import run_automation_cycle
from app.services.automation_shortlist import prepare_shortlist_drafts

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


async def _noop_collector(db, settings):
    return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}


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


def _settings(**overrides) -> Settings:
    data = {
        "automation_auto_prepare_enabled": True,
        "automation_shortlist_min_match_score": 0,
        "automation_shortlist_max_per_run": 10,
    }
    data.update(overrides)
    return Settings(**data)


def _run_shortlist(db, settings, since=None) -> dict:
    if since is None:
        since = datetime.now(UTC) - timedelta(hours=1)
    return asyncio.run(prepare_shortlist_drafts(db, run_started_at=since, settings=settings))


# --- A. Disabled by default -------------------------------------------------


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
        # automation_auto_prepare_enabled defaults to False independently
        # of any scheduler setting -- Settings() with no overrides at all
        # already proves this (see test above); this test additionally
        # pins the field's own default value directly.
        assert Settings().automation_auto_prepare_enabled is False


# --- B. Candidate pool -------------------------------------------------------


class TestCandidatePool:
    def test_pool_filters_by_last_seen_at_status_and_recommendation(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            stale = since - timedelta(minutes=1)

            included_new = _seed_job(db, status="NEW", recommendation="APPLY", last_seen_at=fresh)
            included_saved = _seed_job(
                db, status="SAVED", recommendation="MAYBE", last_seen_at=fresh
            )
            _seed_job(db, status="NEW", recommendation="APPLY", last_seen_at=stale)  # too old
            _seed_job(db, status="APPLIED", recommendation="APPLY", last_seen_at=fresh)
            _seed_job(db, status="INTERVIEW", recommendation="APPLY", last_seen_at=fresh)
            _seed_job(db, status="OFFER", recommendation="APPLY", last_seen_at=fresh)
            _seed_job(db, status="REJECTED", recommendation="APPLY", last_seen_at=fresh)
            _seed_job(db, status="WITHDRAWN", recommendation="APPLY", last_seen_at=fresh)
            _seed_job(db, status="NEW", recommendation="SKIP", last_seen_at=fresh)
            _seed_job(db, status="NEW", recommendation="NEEDS_ENRICHMENT", last_seen_at=fresh)

            pool = get_current_cycle_candidate_jobs(db, since=since)

            assert [job.id for job in pool] == sorted(
                job.id for job in [included_new, included_saved]
            )
        finally:
            db.close()

    def test_pool_ordering_is_deterministic_ascending_by_id(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            jobs = [_seed_job(db, last_seen_at=fresh) for _ in range(4)]

            pool = get_current_cycle_candidate_jobs(db, since=since)

            assert [job.id for job in pool] == sorted(job.id for job in jobs)
        finally:
            db.close()

    def test_boundary_last_seen_at_equal_to_since_is_included(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            job = _seed_job(db, last_seen_at=since)

            pool = get_current_cycle_candidate_jobs(db, since=since)

            assert [j.id for j in pool] == [job.id]
        finally:
            db.close()


# --- C. Ranking --------------------------------------------------------------


class TestRanking:
    def test_ranked_by_match_score_desc_then_job_score_desc_then_id_asc(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)

            low = _seed_job(db, tier=TIER_20, score=10, last_seen_at=fresh)
            high_a = _seed_job(db, tier=TIER_60, score=50, last_seen_at=fresh)
            high_b_lower_job_score = _seed_job(db, tier=TIER_60, score=30, last_seen_at=fresh)
            mid = _seed_job(db, tier=TIER_40, score=99, last_seen_at=fresh)

            settings = _settings(
                automation_shortlist_min_match_score=0, automation_shortlist_max_per_run=10
            )
            result = _run_shortlist(db, settings, since=since)

            job_ids_in_order = [item["job_id"] for item in result["items"]]
            # high tier (60) beats mid (40) beats low (20) regardless of
            # JobRecord.score; WITHIN the same match-score tier, higher
            # JobRecord.score (50 > 30) wins; id is never used as the
            # primary or secondary key here since scores already differ
            # for high_a vs high_b.
            assert job_ids_in_order == [high_a.id, high_b_lower_job_score.id, mid.id, low.id]
        finally:
            db.close()

    def test_tie_break_on_job_id_ascending_when_scores_are_identical(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)

            job_a = _seed_job(db, tier=TIER_60, score=50, last_seen_at=fresh)
            job_b = _seed_job(db, tier=TIER_60, score=50, last_seen_at=fresh)
            job_c = _seed_job(db, tier=TIER_60, score=50, last_seen_at=fresh)

            settings = _settings()
            result = _run_shortlist(db, settings, since=since)

            job_ids_in_order = [item["job_id"] for item in result["items"]]
            assert job_ids_in_order == sorted([job_a.id, job_b.id, job_c.id])
        finally:
            db.close()

    def test_max_per_run_caps_the_shortlist(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            for _ in range(5):
                _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            settings = _settings(automation_shortlist_max_per_run=2)
            result = _run_shortlist(db, settings, since=since)

            assert result["counters"]["matched"] == 5
            assert result["counters"]["shortlisted"] == 2
            assert len(result["items"]) == 2
        finally:
            db.close()


# --- D. Threshold --------------------------------------------------------------


class TestThreshold:
    def test_below_threshold_gets_no_cv_or_bewerbung(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            _seed_job(db, tier=TIER_20, last_seen_at=fresh)  # score 20, below 50

            settings = _settings(automation_shortlist_min_match_score=50)
            result = _run_shortlist(db, settings, since=since)

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
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            job = _seed_job(db, tier=TIER_60, last_seen_at=fresh)  # score exactly 60

            settings = _settings(automation_shortlist_min_match_score=60)
            result = _run_shortlist(db, settings, since=since)

            assert result["counters"]["shortlisted"] == 1
            assert result["items"][0]["job_id"] == job.id
            assert result["items"][0]["match_score"] == 60
        finally:
            db.close()


# --- E. Match reuse ------------------------------------------------------------


class TestMatchReuse:
    def test_second_cycle_reuses_the_cached_match_no_forced_recompute(
        self, session_factory, monkeypatch
    ):
        call_count = 0
        original = None
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
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            settings = _settings()
            _run_shortlist(db, settings, since=since)
            _run_shortlist(db, settings, since=since)

            assert call_count == 1
            assert db.query(CandidateJobMatchRecord).count() == 1
        finally:
            db.close()


# --- F. CV reuse -----------------------------------------------------------


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
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            settings = _settings()
            first = _run_shortlist(db, settings, since=since)
            second = _run_shortlist(db, settings, since=since)

            assert call_count == 1
            assert db.query(CandidateCVDraftRecord).count() == 1
            assert first["items"][0]["cv_reused"] is False
            assert second["items"][0]["cv_reused"] is True
        finally:
            db.close()


# --- G. Bewerbung reuse ------------------------------------------------------


class TestBewerbungReuse:
    def test_repeated_cycle_with_unchanged_inputs_creates_no_extra_bewerbung(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            settings = _settings()
            first = _run_shortlist(db, settings, since=since)
            second = _run_shortlist(db, settings, since=since)
            third = _run_shortlist(db, settings, since=since)

            assert db.query(BewerbungDraftRecord).count() == 1
            assert first["counters"]["bewerbung_created"] == 1
            assert first["counters"]["bewerbung_reused"] == 0
            assert second["counters"]["bewerbung_created"] == 0
            assert second["counters"]["bewerbung_reused"] == 1
            assert third["counters"]["bewerbung_created"] == 0
            assert third["counters"]["bewerbung_reused"] == 1
            assert (
                first["items"][0]["bewerbung_draft_id"] == second["items"][0]["bewerbung_draft_id"]
            )
        finally:
            db.close()

    def test_integration_via_run_automation_cycle_does_not_spam_bewerbung(
        self, session_factory, monkeypatch
    ):
        """End-to-end proof through the real scheduler-triggered path
        (run_automation_cycle), not just the unit-level
        prepare_shortlist_drafts call above -- a job whose collector
        re-touches last_seen_at every cycle (exactly like a real
        collector re-fetching it) must still only ever get ONE
        BewerbungDraftRecord across multiple automation cycles.
        """
        db = session_factory()
        try:
            job = _seed_job(db, tier=TIER_60, last_seen_at=datetime.now(UTC) - timedelta(hours=2))
            job_id = job.id

            async def _touch_job_collector(db, settings):
                record = db.get(JobRecord, job_id)
                record.last_seen_at = datetime.now(UTC)
                db.commit()
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


# --- H. Changed inputs -----------------------------------------------------


class TestChangedInputsForceRegeneration:
    def test_candidate_profile_version_change_forces_new_match_cv_and_bewerbung(
        self, session_factory
    ):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            settings = _settings()
            first = _run_shortlist(db, settings, since=since)

            apply_candidate_profile_patch(
                db, CandidateProfilePatchRequest(expected_profile_version=1, first_name="Anna")
            )

            second = _run_shortlist(db, settings, since=since)

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
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            job = _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            settings = _settings()
            first = _run_shortlist(db, settings, since=since)

            job.must_have_skills_json = '["python"]'
            db.add(job)
            db.commit()

            second = _run_shortlist(db, settings, since=since)

            assert db.query(CandidateJobMatchRecord).count() == 2
            assert db.query(CandidateCVDraftRecord).count() == 2
            assert db.query(BewerbungDraftRecord).count() == 2
            assert first["items"][0]["match_id"] != second["items"][0]["match_id"]
            assert second["counters"]["bewerbung_created"] == 1
            assert second["counters"]["bewerbung_reused"] == 0
        finally:
            db.close()


# --- I. Failure isolation ----------------------------------------------------


class TestFailureIsolation:
    def test_one_shortlisted_job_raising_does_not_block_the_rest(
        self, session_factory, monkeypatch, caplog
    ):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            failing_job = _seed_job(db, tier=TIER_60, score=99, last_seen_at=fresh)
            healthy_job = _seed_job(db, tier=TIER_60, score=1, last_seen_at=fresh)

            import app.services.automation_shortlist as shortlist_module

            original = shortlist_module.prepare_candidate_cv_draft

            def _boom_for_failing_job(db, job_id, match_id, *, force_recompute):
                if job_id == failing_job.id:
                    raise RuntimeError("secret-upstream-detail")
                return original(db, job_id, match_id, force_recompute=force_recompute)

            monkeypatch.setattr(
                "app.services.automation_shortlist.prepare_candidate_cv_draft",
                _boom_for_failing_job,
            )

            settings = _settings()
            with caplog.at_level("DEBUG"):
                result = _run_shortlist(db, settings, since=since)

            assert result["status"] == "partial"
            assert result["counters"]["failed"] == 1
            assert result["counters"]["shortlisted"] == 2

            by_job_id = {item["job_id"]: item for item in result["items"]}
            assert by_job_id[failing_job.id]["status"] == "failed"
            assert by_job_id[failing_job.id]["error_type"] == "RuntimeError"
            assert by_job_id[failing_job.id]["cv_draft_id"] is None
            assert by_job_id[healthy_job.id]["status"] == "ok"
            assert by_job_id[healthy_job.id]["cv_draft_id"] is not None

            # The healthy job's own draft really did get committed despite
            # the other job's rollback.
            assert db.query(CandidateCVDraftRecord).count() == 1
            assert db.query(BewerbungDraftRecord).count() == 1

            assert "secret-upstream-detail" not in caplog.text
        finally:
            db.close()

    def test_step_result_never_contains_raw_exception_text(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            job = _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            def _boom(db, job_id, match_id, *, force_recompute):
                raise RuntimeError("another-secret-detail-xyz")

            monkeypatch.setattr(
                "app.services.automation_shortlist.prepare_candidate_cv_draft", _boom
            )

            settings = _settings()
            result = _run_shortlist(db, settings, since=since)

            serialized = repr(result)
            assert "another-secret-detail-xyz" not in serialized
            assert result["items"][0]["error_type"] == "RuntimeError"
            assert result["items"][0]["job_id"] == job.id
        finally:
            db.close()


# --- J. Job status safety ---------------------------------------------------


class TestJobStatusSafety:
    def test_new_and_saved_status_are_never_mutated_by_shortlisting(self, session_factory):
        db = session_factory()
        try:
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            new_job = _seed_job(db, tier=TIER_60, status="NEW", last_seen_at=fresh)
            saved_job = _seed_job(db, tier=TIER_60, status="SAVED", last_seen_at=fresh)

            settings = _settings()
            result = _run_shortlist(db, settings, since=since)

            assert result["counters"]["shortlisted"] == 2

            db.expire_all()
            assert db.get(JobRecord, new_job.id).status == "NEW"
            assert db.get(JobRecord, saved_job.id).status == "SAVED"
        finally:
            db.close()


# --- K. Human approval safety -------------------------------------------------


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
            since = datetime.now(UTC) - timedelta(hours=1)
            fresh = since + timedelta(minutes=1)
            _seed_job(db, tier=TIER_60, last_seen_at=fresh)

            settings = _settings()
            result = _run_shortlist(db, settings, since=since)

            assert result["counters"]["shortlisted"] == 1
            # Only match/CV/Bewerbung rows exist -- proven exhaustively by
            # the counts above in other tests; here we additionally prove
            # no ApplicationPackageReviewRecord was created as a side
            # effect of drafting.
            from app.db.models import ApplicationPackageReviewRecord

            assert db.query(ApplicationPackageReviewRecord).count() == 0
        finally:
            db.close()
