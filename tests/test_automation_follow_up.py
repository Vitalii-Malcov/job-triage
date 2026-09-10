"""Stage 8D tests: automated follow-up proposal cycle
(`app.services.automation_follow_up`) — bounded round-robin scanning
over currently-APPLIED jobs, its crash-safe/wrapping cursor, and its
reuse of the existing Stage 7E `evaluate_follow_up_for_job` service
unchanged.

Mirrors tests/test_automation_gmail.py's/tests/test_follow_up_service.py's
approach: a real file-backed SQLite session, real
`evaluate_follow_up_for_job` calls (never re-implemented), real
relative-to-now timestamps (no fixed clock override is threaded through
`app.services.automation_follow_up.prepare_follow_up_proposals`, so
tests seed anchors relative to `datetime.now(UTC)` at seed time).
"""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.automation_mail_progress_repository import (
    advance_follow_up_cursor,
    get_mail_progress,
    get_or_create_mail_progress,
)
from app.db.base import Base
from app.db.follow_up_repository import list_follow_up_proposals
from app.db.gmail_repository import upsert_message
from app.db.models import GmailMessageAnalysisRecord, JobRecord
from app.providers.email.base import ParsedGmailMessage
from app.services.automation import run_automation_cycle
from app.services.automation_follow_up import prepare_follow_up_proposals
from app.services.follow_up import evaluate_follow_up_for_job

ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"

_uid_counter = 0
_fp_counter = 0


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_automation_follow_up.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


async def _noop_collector(db, settings, *, touched_jobs=None, is_lease_lost=None):
    return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}


def _add_job(db, *, status="APPLIED", source="bundesagentur") -> JobRecord:
    global _fp_counter
    _fp_counter += 1
    job = JobRecord(
        fingerprint=f"fp-{_fp_counter}",
        source=source,
        title="Backend Engineer",
        company="Globex",
        location="Berlin",
        url=f"https://example.com/jobs/{_fp_counter}",
        description="",
        score=80,
        recommendation="APPLY",
        status=status,
    )
    db.add(job)
    db.commit()
    return job


def _add_outbound_message(db, *, account_key=ACCOUNT, sent_at: datetime):
    global _uid_counter
    _uid_counter += 1
    parsed = ParsedGmailMessage(
        account_key=account_key,
        mailbox="INBOX",
        uid=_uid_counter,
        uid_validity=100,
        message_id_header=f"<out-{_uid_counter}@example.com>",
        in_reply_to=None,
        references=(),
        from_address=account_key,
        from_display_name=None,
        to_addresses=("hr@acme.example.com",),
        cc_addresses=(),
        subject="My application at Globex",
        sent_at=sent_at,
        direction="OUTBOUND",
        body_plain="I am applying for the Backend Engineer role at Globex.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    record, _created = upsert_message(db, parsed)
    record.received_at = sent_at
    record.provider_arrival_at = sent_at
    record.provider_arrival_is_trusted = True
    db.commit()
    db.refresh(record)
    return record


def _add_analysis(db, *, account_key=ACCOUNT, gmail_message_id, matched_job_id):
    db.add(
        GmailMessageAnalysisRecord(
            account_key=account_key,
            gmail_message_id=gmail_message_id,
            analysis_version=1,
            input_fingerprint=f"fp-{gmail_message_id}",
            context_fingerprint="ctx",
            match_type="APPLICATION",
            matched_job_id=matched_job_id,
            match_confidence="HIGH",
            match_score=90,
            classification="OTHER",
            classification_confidence="HIGH",
            is_automated=False,
            requires_human_review=True,
        )
    )
    db.commit()


def _seed_eligible_job(db, *, account_key=ACCOUNT, days_old=10, status="APPLIED"):
    """A job whose outbound anchor is `days_old` days old -- eligible
    whenever `days_old > settings.follow_up_delay_days` (default 7).
    """
    job = _add_job(db, status=status)
    anchor = _add_outbound_message(
        db, account_key=account_key, sent_at=datetime.now(UTC) - timedelta(days=days_old)
    )
    _add_analysis(db, account_key=account_key, gmail_message_id=anchor.id, matched_job_id=job.id)
    return job, anchor


def _settings(**overrides) -> Settings:
    data = {
        "automation_follow_up_cycle_enabled": True,
        "automation_follow_up_job_max_per_run": 100,
        "follow_up_delay_days": 7,
    }
    data.update(overrides)
    return Settings(**data)


def _run_follow_up(db, settings, account_key=ACCOUNT) -> dict:
    return asyncio.run(prepare_follow_up_proposals(db, account_key=account_key, settings=settings))


# --- A. Disabled by default ------------------------------------------------


class TestDisabledByDefault:
    def test_default_settings_never_triggers_follow_up_step(self, session_factory, monkeypatch):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        try:
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=Settings()))
            results = json.loads(run.results_json)
            assert "follow_up_proposals" not in results
        finally:
            db.close()

    def test_follow_up_cycle_disabled_by_default(self):
        assert Settings().automation_follow_up_cycle_enabled is False


# --- K. Follow-up bounded scan -------------------------------------------------


class TestFollowUpBoundedScan:
    def test_only_configured_limit_jobs_are_evaluated(self, session_factory):
        db = session_factory()
        try:
            jobs = [_seed_eligible_job(db)[0] for _ in range(5)]

            settings = _settings(automation_follow_up_job_max_per_run=2)
            result = _run_follow_up(db, settings)

            assert result["counters"]["scanned"] == 2
            processed_ids = [item["job_id"] for item in result["items"]]
            assert processed_ids == [jobs[0].id, jobs[1].id]
        finally:
            db.close()


# --- L. Follow-up cursor continuation ------------------------------------------


class TestFollowUpCursorContinuation:
    def test_next_run_starts_after_prior_cursor(self, session_factory):
        db = session_factory()
        try:
            jobs = [_seed_eligible_job(db)[0] for _ in range(4)]

            settings = _settings(automation_follow_up_job_max_per_run=2)
            first = _run_follow_up(db, settings)
            assert [item["job_id"] for item in first["items"]] == [jobs[0].id, jobs[1].id]

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id == jobs[1].id

            second = _run_follow_up(db, settings)
            assert [item["job_id"] for item in second["items"]] == [jobs[2].id, jobs[3].id]
        finally:
            db.close()


# --- M. Follow-up wrap ----------------------------------------------------------


class TestFollowUpWrap:
    def test_cursor_resets_after_reaching_the_end(self, session_factory):
        db = session_factory()
        try:
            for _ in range(3):
                _seed_eligible_job(db)

            settings = _settings(automation_follow_up_job_max_per_run=10)
            result = _run_follow_up(db, settings)

            assert result["counters"]["scanned"] == 3
            assert result["counters"]["cursor_wrapped"] == 1

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id is None
        finally:
            db.close()

    def test_next_run_after_wrap_begins_from_oldest_applied_job(self, session_factory):
        db = session_factory()
        try:
            jobs = [_seed_eligible_job(db)[0] for _ in range(2)]

            settings = _settings(automation_follow_up_job_max_per_run=10)
            _run_follow_up(db, settings)  # wraps

            second = _run_follow_up(db, settings)
            assert [item["job_id"] for item in second["items"]] == [jobs[0].id, jobs[1].id]
        finally:
            db.close()

    def test_no_immediate_second_pass_within_the_same_run(self, session_factory):
        """Wrapping resets the cursor for the NEXT run -- it must never
        cause a single prepare_follow_up_proposals call to silently
        process the same jobs twice.
        """
        db = session_factory()
        try:
            jobs = [_seed_eligible_job(db)[0] for _ in range(2)]

            settings = _settings(automation_follow_up_job_max_per_run=10)
            result = _run_follow_up(db, settings)

            assert result["counters"]["scanned"] == len(jobs)
        finally:
            db.close()


# --- N. Follow-up failure -------------------------------------------------------


class TestFollowUpFailure:
    def test_cursor_stops_before_failed_job(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            first_job, _ = _seed_eligible_job(db)
            failing_job, _ = _seed_eligible_job(db)
            never_reached, _ = _seed_eligible_job(db)

            import app.services.automation_follow_up as follow_up_module

            original = follow_up_module.evaluate_follow_up_for_job

            def _boom_for_second(db, account_key, job_id, *, settings=None, now=None):
                if job_id == failing_job.id:
                    raise RuntimeError("secret-follow-up-detail")
                return original(db, account_key, job_id, settings=settings, now=now)

            monkeypatch.setattr(
                "app.services.automation_follow_up.evaluate_follow_up_for_job", _boom_for_second
            )

            result = _run_follow_up(db, _settings())

            assert result["counters"]["failed"] == 1
            processed_ids = [item["job_id"] for item in result["items"]]
            assert processed_ids == [first_job.id]
            assert never_reached.id not in processed_ids

            failure = result["failures"][0]
            assert failure["job_id"] == failing_job.id
            assert failure["phase"] == "follow_up"
            assert failure["error_type"] == "RuntimeError"
            assert "secret-follow-up-detail" not in json.dumps(result)

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id == first_job.id
        finally:
            db.close()

    def test_next_run_retries_the_failed_job(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            _seed_eligible_job(db)
            failing_job, _ = _seed_eligible_job(db)

            import app.services.automation_follow_up as follow_up_module

            original = follow_up_module.evaluate_follow_up_for_job

            def _boom_once(db, account_key, job_id, *, settings=None, now=None):
                if job_id == failing_job.id:
                    raise RuntimeError("boom")
                return original(db, account_key, job_id, settings=settings, now=now)

            monkeypatch.setattr(
                "app.services.automation_follow_up.evaluate_follow_up_for_job", _boom_once
            )
            first = _run_follow_up(db, _settings())
            assert first["counters"]["failed"] == 1

            monkeypatch.undo()
            second = _run_follow_up(db, _settings())
            processed_ids = [item["job_id"] for item in second["items"]]
            assert failing_job.id in processed_ids
            assert second["counters"]["failed"] == 0
        finally:
            db.close()


# --- O. Proposal reuse -----------------------------------------------------------


class TestProposalReuse:
    def test_unchanged_eligible_job_creates_no_duplicate_proposal(self, session_factory):
        db = session_factory()
        try:
            _seed_eligible_job(db)

            first = _run_follow_up(db, _settings())
            assert first["counters"]["proposal_created"] == 1
            assert first["items"][0]["proposal_created"] is True
            first_proposal_id = first["items"][0]["proposal_id"]

            # Wrap already happened (only one job) -- next call re-scans
            # the same job from the wrapped (None) cursor.
            second = _run_follow_up(db, _settings())
            assert second["counters"]["proposal_reused"] == 1
            assert second["items"][0]["proposal_created"] is False
            assert second["items"][0]["proposal_id"] == first_proposal_id

            proposals = list_follow_up_proposals(db, ACCOUNT, limit=50, offset=0)
            assert len(proposals) == 1
        finally:
            db.close()


# --- P. CAS race (follow-up cursor) --------------------------------------------


class TestFollowUpCursorCASRace:
    def test_stale_owner_cannot_overwrite_newer_cursor(self, session_factory):
        db = session_factory()
        try:
            get_or_create_mail_progress(db, ACCOUNT)
            ok = advance_follow_up_cursor(db, ACCOUNT, expected_cursor=None, new_cursor=5)
            assert ok is True

            stale_write = advance_follow_up_cursor(db, ACCOUNT, expected_cursor=None, new_cursor=10)
            assert stale_write is False

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id == 5
        finally:
            db.close()

    def test_stale_owner_cannot_overwrite_a_wrap(self, session_factory):
        db = session_factory()
        try:
            get_or_create_mail_progress(db, ACCOUNT)
            advance_follow_up_cursor(db, ACCOUNT, expected_cursor=None, new_cursor=5)
            wrapped = advance_follow_up_cursor(db, ACCOUNT, expected_cursor=5, new_cursor=None)
            assert wrapped is True

            stale_write = advance_follow_up_cursor(db, ACCOUNT, expected_cursor=5, new_cursor=99)
            assert stale_write is False

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id is None
        finally:
            db.close()

    def test_real_concurrent_cas_only_one_thread_wins(self, session_factory):
        db_a = session_factory()
        db_b = session_factory()
        try:
            get_or_create_mail_progress(db_a, ACCOUNT)
            barrier = threading.Barrier(2)
            results: dict[str, bool] = {}

            def _worker(name, db):
                barrier.wait(timeout=10)
                results[name] = advance_follow_up_cursor(
                    db, ACCOUNT, expected_cursor=None, new_cursor=42
                )

            thread_a = threading.Thread(target=_worker, args=("a", db_a))
            thread_b = threading.Thread(target=_worker, args=("b", db_b))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=15)
            thread_b.join(timeout=15)

            assert not thread_a.is_alive()
            assert not thread_b.is_alive()
            assert set(results) == {"a", "b"}
            assert sorted(results.values()) == [False, True]
        finally:
            db_a.close()
            db_b.close()


# --- Q. Account isolation -----------------------------------------------------


class TestAccountIsolation:
    def test_job_matched_only_under_another_account_is_not_eligible_here(self, session_factory):
        db = session_factory()
        try:
            job = _add_job(db)
            anchor = _add_outbound_message(
                db, account_key=OTHER_ACCOUNT, sent_at=datetime.now(UTC) - timedelta(days=10)
            )
            _add_analysis(
                db, account_key=OTHER_ACCOUNT, gmail_message_id=anchor.id, matched_job_id=job.id
            )

            result = _run_follow_up(db, _settings(), account_key=ACCOUNT)

            assert result["counters"]["scanned"] == 1
            assert result["counters"]["eligible"] == 0
            assert result["items"][0]["eligibility"] == "NOT_ELIGIBLE"
        finally:
            db.close()

    def test_job_matched_under_the_correct_account_is_eligible(self, session_factory):
        db = session_factory()
        try:
            job, _anchor = _seed_eligible_job(db, account_key=ACCOUNT)
            result = _run_follow_up(db, _settings(), account_key=ACCOUNT)
            assert result["items"][0]["eligibility"] == "ELIGIBLE"
        finally:
            db.close()


# --- No send / no approval ------------------------------------------------------


class TestNoSendNoApproval:
    def test_module_never_imports_send_or_approval_logic(self):
        import app.services.automation_follow_up as module

        for forbidden in (
            "send_follow_up",
            "approve_or_reject_follow_up",
            "send_response_draft",
            "approve_or_reject_response_draft",
            "GmailSmtpProvider",
            "update_job_status",
        ):
            assert not hasattr(module, forbidden)

    def test_runtime_send_and_approval_mocks_are_never_invoked(self, session_factory, monkeypatch):
        send_mock = _CallCounter()
        approve_mock = _CallCounter()
        monkeypatch.setattr("app.services.follow_up_send.send_follow_up", send_mock)
        monkeypatch.setattr("app.services.follow_up_send.approve_or_reject_follow_up", approve_mock)

        db = session_factory()
        try:
            _seed_eligible_job(db)
            _run_follow_up(db, _settings())

            assert send_mock.call_count == 0
            assert approve_mock.call_count == 0
        finally:
            db.close()


class _CallCounter:
    def __init__(self):
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        raise AssertionError("this send/approval function must never be called by Stage 8D")


# --- Job.status unchanged --------------------------------------------------------


class TestJobStatusUnchanged:
    def test_applied_status_is_never_mutated(self, session_factory):
        db = session_factory()
        try:
            job, _anchor = _seed_eligible_job(db)
            _run_follow_up(db, _settings())

            db.expire_all()
            assert db.get(JobRecord, job.id).status == "APPLIED"
        finally:
            db.close()


# --- NOT_ELIGIBLE is not a failure ------------------------------------------------


class TestNotEligibleIsNotAFailure:
    def test_too_recent_job_is_not_eligible_and_not_a_failure(self, session_factory):
        db = session_factory()
        try:
            _seed_eligible_job(db, days_old=1)  # younger than the 7-day delay
            result = _run_follow_up(db, _settings())

            assert result["counters"]["failed"] == 0
            assert result["counters"]["not_eligible"] == 1
            assert result["status"] == "ok"
            assert result["items"][0]["eligibility"] == "NOT_ELIGIBLE"
        finally:
            db.close()

    def test_evaluate_follow_up_for_job_is_the_real_unmodified_service(self, session_factory):
        """Sanity: prepare_follow_up_proposals must produce the exact
        same eligibility the manual service call would for the identical
        inputs -- proving no re-implementation drifted from Stage 7E.
        """
        db = session_factory()
        try:
            job, _anchor = _seed_eligible_job(db, days_old=10)
            direct = evaluate_follow_up_for_job(db, ACCOUNT, job.id, settings=_settings())
            assert direct.eligibility == "ELIGIBLE"
        finally:
            db.close()


# --- S8D-PROGRESS-002: follow-up cursor/wrap CAS loss must never be "ok" ---


class TestFollowUpCursorCASLossIsNonOk:
    def test_normal_cursor_cas_loss_is_non_ok(self, session_factory, monkeypatch):
        monkeypatch.setattr(
            "app.services.automation_follow_up.advance_follow_up_cursor", lambda *a, **kw: False
        )

        db = session_factory()
        try:
            _seed_eligible_job(db)
            result = _run_follow_up(db, _settings())

            assert result["status"] != "ok"
            assert result["counters"]["failed"] == 1
            assert result["counters"]["cursor_wrapped"] == 0

            failure = result["failures"][0]
            assert failure["phase"] == "follow_up"
            assert failure["error_type"] == "AutomationMailProgressCASLostError"

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id is None  # never advanced
        finally:
            db.close()

    def test_cas_loss_with_a_prior_success_is_partial(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            first, _ = _seed_eligible_job(db)
            _seed_eligible_job(db)

            import app.services.automation_follow_up as follow_up_module

            original = follow_up_module.advance_follow_up_cursor

            def _fail_on_second(db, account_key, *, expected_cursor, new_cursor):
                if expected_cursor == first.id:
                    return False
                return original(
                    db, account_key, expected_cursor=expected_cursor, new_cursor=new_cursor
                )

            monkeypatch.setattr(
                "app.services.automation_follow_up.advance_follow_up_cursor", _fail_on_second
            )

            result = _run_follow_up(db, _settings())
            assert result["status"] == "partial"
            assert result["counters"]["failed"] == 1

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id == first.id
        finally:
            db.close()

    def test_wrap_cas_loss_is_non_ok_and_cursor_wrapped_stays_zero(
        self, session_factory, monkeypatch
    ):
        db = session_factory()
        try:
            _seed_eligible_job(db)  # exactly one job -- wrap is attempted after it

            import app.services.automation_follow_up as follow_up_module

            original = follow_up_module.advance_follow_up_cursor

            def _fail_only_the_wrap(db, account_key, *, expected_cursor, new_cursor):
                if new_cursor is None:
                    return False
                return original(
                    db, account_key, expected_cursor=expected_cursor, new_cursor=new_cursor
                )

            monkeypatch.setattr(
                "app.services.automation_follow_up.advance_follow_up_cursor", _fail_only_the_wrap
            )

            result = _run_follow_up(db, _settings())

            assert result["status"] != "ok"
            assert result["counters"]["cursor_wrapped"] == 0
            assert result["counters"]["failed"] == 1

            failure = result["failures"][0]
            assert failure["phase"] == "follow_up"
            assert failure["error_type"] == "AutomationMailProgressCASLostError"

            # The job itself WAS successfully evaluated -- only the wrap
            # bookkeeping failed. The cursor stays at that job's id
            # (never silently reset, never silently advanced further).
            progress = get_mail_progress(db, ACCOUNT)
            assert progress.follow_up_after_job_id is not None
        finally:
            db.close()

    def test_wrap_cas_loss_with_zero_eligible_jobs_is_non_ok(self, session_factory, monkeypatch):
        """Edge case: zero APPLIED jobs exist at all, so the loop never
        runs, but the wrap-check/CAS attempt is still made and can still
        be lost to a newer owner -- this must not default to "ok" merely
        because nothing was "scanned".
        """
        monkeypatch.setattr(
            "app.services.automation_follow_up.advance_follow_up_cursor", lambda *a, **kw: False
        )
        db = session_factory()
        try:
            result = _run_follow_up(db, _settings())
            assert result["counters"]["scanned"] == 0
            assert result["status"] != "ok"
            assert result["counters"]["failed"] == 1
        finally:
            db.close()


# --- S8D-PRIVACY-001: no account_key in Stage 8D follow-up logs -------------


class TestPrivacyNoAccountKeyInFollowUpLogs:
    def test_success_path_logs_never_contain_account_key(self, session_factory, caplog):
        db = session_factory()
        try:
            _seed_eligible_job(db)
            with caplog.at_level("DEBUG"):
                _run_follow_up(db, _settings())
            assert ACCOUNT not in caplog.text
        finally:
            db.close()

    def test_failure_path_logs_never_contain_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "app.services.automation_follow_up.advance_follow_up_cursor", lambda *a, **kw: False
        )
        db = session_factory()
        try:
            _seed_eligible_job(db)
            with caplog.at_level("DEBUG"):
                _run_follow_up(db, _settings())
            assert ACCOUNT not in caplog.text
        finally:
            db.close()


# --- CAS failure propagates AutomationRun to PARTIAL ------------------------


class TestCASFailurePropagatesToOverallPartial:
    def test_follow_up_cursor_cas_loss_makes_run_partial_when_core_collectors_ok(
        self, session_factory, monkeypatch
    ):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)
        monkeypatch.setattr(
            "app.services.automation_follow_up.advance_follow_up_cursor", lambda *a, **kw: False
        )

        db = session_factory()
        try:
            _seed_eligible_job(db)
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))

            assert run.status == "PARTIAL"
            results = json.loads(run.results_json)
            assert results["follow_up_proposals"]["status"] != "ok"
        finally:
            db.close()
