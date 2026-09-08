"""Stage 8D tests: automated Gmail sync + response-draft preparation
cycle (`app.services.automation_gmail`), plus the Gmail-sync extraction
(`app.services.gmail_sync`) and the shared crash-safe progress cursor
(`app.db.automation_mail_progress_repository`).

Mirrors tests/test_automation_shortlist.py's approach: a real
file-backed SQLite session, no-op collector monkeypatches for
integration-level `run_automation_cycle` proof, plus direct calls into
`app.services.automation_gmail.prepare_gmail_sync`/
`prepare_gmail_response_drafts` for fast, precise unit-level proof.
"""

import asyncio
import json
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.automation_mail_progress_repository import (
    advance_gmail_cursor,
    get_mail_progress,
    get_or_create_mail_progress,
)
from app.db.base import Base
from app.db.gmail_repository import upsert_message
from app.db.models import GmailMessageAnalysisRecord, JobRecord
from app.models.automation import AutomationRun, AutomationRunStepResult
from app.models.gmail import GmailSyncResult
from app.providers.email.base import ParsedGmailMessage
from app.services.automation import run_automation_cycle
from app.services.automation_gmail import prepare_gmail_response_drafts, prepare_gmail_sync
from app.services.gmail_message_analysis import GmailMessageNotFoundError, analyze_gmail_message
from app.services.response_draft import (
    ResponseDraftMessageNotFoundError,
    generate_response_draft_for_message,
)

ACCOUNT = "me@example.com"
OTHER_ACCOUNT = "someone-else@example.com"

_uid_counter = 0


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_automation_gmail.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


async def _noop_collector(db, settings, *, touched_jobs=None):
    return {"fetched": 0, "created": 0, "updated": 0, "skipped_invalid": 0, "failed": 0}


# body_plain that classifies as OFFER -> supported -> PROPOSED response draft.
OFFER_BODY = "We are pleased to offer you the position of Backend Engineer at Globex."
# body_plain that classifies as UNKNOWN -> unsupported -> NO_RESPONSE_RECOMMENDED.
UNKNOWN_BODY = "Please find attached our monthly company newsletter."


def _seed_message(
    db, *, account_key=ACCOUNT, body_plain=OFFER_BODY, direction="INBOUND", subject="Offer"
):
    global _uid_counter
    _uid_counter += 1
    parsed = ParsedGmailMessage(
        account_key=account_key,
        mailbox="INBOX",
        uid=_uid_counter,
        uid_validity=100,
        message_id_header=f"<{_uid_counter}@example.com>",
        in_reply_to=None,
        references=(),
        from_address="hr@acme.example.com",
        from_display_name="Recruiter",
        to_addresses=(account_key,),
        cc_addresses=(),
        subject=subject,
        sent_at=datetime.now(UTC),
        direction=direction,
        body_plain=body_plain,
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    msg, _created = upsert_message(db, parsed)
    db.commit()
    return msg


def _settings(**overrides) -> Settings:
    data = {
        "automation_gmail_cycle_enabled": True,
        "automation_gmail_process_max_per_run": 100,
        "gmail_username": ACCOUNT,
        "gmail_app_password": "app-password",
    }
    data.update(overrides)
    return Settings(**data)


def _run_gmail_drafts(db, settings, account_key=ACCOUNT) -> dict:
    return asyncio.run(
        prepare_gmail_response_drafts(db, account_key=account_key, settings=settings)
    )


def _run_gmail_sync(db, settings, account_key=ACCOUNT) -> dict:
    return asyncio.run(prepare_gmail_sync(db, account_key=account_key, settings=settings))


# --- A. Disabled by default ------------------------------------------------


class TestDisabledByDefault:
    def test_default_settings_never_triggers_gmail_steps(self, session_factory, monkeypatch):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

        db = session_factory()
        try:
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=Settings()))
            assert run.status == "COMPLETED"
            results = json.loads(run.results_json)
            assert set(results.keys()) == {"bundesagentur", "xing"}
            assert "gmail_sync" not in results
            assert "gmail_response_drafts" not in results
        finally:
            db.close()

    def test_gmail_cycle_disabled_by_default(self):
        assert Settings().automation_gmail_cycle_enabled is False


# --- C. Account mismatch -----------------------------------------------------


class TestGmailAccountMismatch:
    def test_mismatched_account_key_fails_closed(self, session_factory):
        db = session_factory()
        try:
            settings = _settings(gmail_username=ACCOUNT)
            result = _run_gmail_sync(db, settings, account_key="different-account@example.com")

            assert result["status"] == "failed"
            assert result["error_type"] == "GmailAccountMismatchError"
            assert db.query(JobRecord).count() == 0  # nothing was ever attempted
        finally:
            db.close()

    def test_matching_account_key_proceeds(self, session_factory, monkeypatch):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            return GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            settings = _settings(gmail_username=ACCOUNT)
            result = _run_gmail_sync(db, settings, account_key=ACCOUNT)
            assert result["status"] == "ok"
        finally:
            db.close()

    def test_not_configured_reports_not_configured(self, session_factory):
        db = session_factory()
        try:
            settings = _settings(gmail_username="", gmail_app_password="")
            result = _run_gmail_sync(db, settings, account_key=ACCOUNT)
            assert result["status"] == "not_configured"
            assert result["error_type"] == "CollectorNotConfiguredError"
        finally:
            db.close()


# --- J. Partial mailbox sync -------------------------------------------------


class TestPartialMailboxSync:
    def test_inbox_success_sent_failure_is_partial_and_inbox_work_remains(
        self, session_factory, monkeypatch
    ):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            if mailbox == settings.gmail_mailbox:
                _seed_message(db, account_key=account_key)
                return GmailSyncResult(fetched=1, created=1, duplicates=0, skipped=0, failed=0)
            raise RuntimeError("sent-mailbox-boom")

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            settings = _settings()
            result = _run_gmail_sync(db, settings)

            assert result["status"] == "partial"
            assert result["counters"]["inbox_created"] == 1
            assert result["counters"]["sent_created"] == 0
            assert result["error_type"] == "RuntimeError"

            # The INBOX-persisted message really is durable.
            assert db.query(JobRecord).count() == 0  # gmail messages aren't JobRecords
            from app.db.models import GmailMessageRecord

            assert db.query(GmailMessageRecord).count() == 1
        finally:
            db.close()

    def test_both_mailboxes_fail_is_failed(self, session_factory, monkeypatch):
        async def _boom(db, settings, account_key, mailbox, *, trusted_outbound):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _boom)

        db = session_factory()
        try:
            result = _run_gmail_sync(db, _settings())
            assert result["status"] == "failed"
        finally:
            db.close()

    def test_offline_processing_still_runs_after_partial_sync_failure(
        self, session_factory, monkeypatch
    ):
        """Integration proof via run_automation_cycle: even though
        gmail_sync itself fails outright, gmail_response_drafts must
        still process whatever Gmail messages are ALREADY persisted.
        """
        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)

            monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
            monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)

            async def _boom_sync(db, *, account_key, settings):
                return {"status": "failed", "counters": None, "error_type": "RuntimeError"}

            monkeypatch.setattr("app.services.automation.prepare_gmail_sync", _boom_sync)

            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))
            results = json.loads(run.results_json)

            assert results["gmail_sync"]["status"] == "failed"
            assert results["gmail_response_drafts"]["counters"]["scanned"] == 1
        finally:
            db.close()


# --- D-I. Gmail message processing cursor ------------------------------------


class TestHistoricalBacklogAndBoundedProcessing:
    def test_existing_messages_processed_oldest_first_bounded(self, session_factory):
        db = session_factory()
        try:
            messages = [_seed_message(db, body_plain=OFFER_BODY) for _ in range(5)]

            settings = _settings(automation_gmail_process_max_per_run=2)
            first = _run_gmail_drafts(db, settings)

            assert first["counters"]["scanned"] == 2
            processed_ids = [item["gmail_message_id"] for item in first["items"]]
            assert processed_ids == [messages[0].id, messages[1].id]

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.gmail_after_message_id == messages[1].id

            second = _run_gmail_drafts(db, settings)
            processed_ids_2 = [item["gmail_message_id"] for item in second["items"]]
            assert processed_ids_2 == [messages[2].id, messages[3].id]
        finally:
            db.close()


class TestGmailSuccessCursor:
    def test_analysis_and_draft_success_advances_cursor(self, session_factory):
        db = session_factory()
        try:
            message = _seed_message(db, body_plain=OFFER_BODY)
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["scanned"] == 1
            assert result["counters"]["failed"] == 0
            assert result["items"][0]["status"] == "ok"
            assert result["items"][0]["response_status"] == "PROPOSED"

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.gmail_after_message_id == message.id
        finally:
            db.close()


class TestGmailFailure:
    def test_analysis_failure_stops_before_advancing_cursor(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            first_message = _seed_message(db, body_plain=OFFER_BODY)
            failing_message = _seed_message(db, body_plain=OFFER_BODY)
            never_reached = _seed_message(db, body_plain=OFFER_BODY)

            original = None
            import app.services.automation_gmail as gmail_module

            original = gmail_module.analyze_gmail_message

            def _boom_for_second(db, account_key, gmail_message_id):
                if gmail_message_id == failing_message.id:
                    raise RuntimeError("secret-analysis-detail")
                return original(db, account_key, gmail_message_id)

            monkeypatch.setattr(
                "app.services.automation_gmail.analyze_gmail_message", _boom_for_second
            )

            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["failed"] == 1
            assert result["status"] == "partial"
            job_ids = [item["gmail_message_id"] for item in result["items"]]
            assert job_ids == [first_message.id, failing_message.id]
            assert never_reached.id not in job_ids

            failure = result["failures"][0]
            assert failure["gmail_message_id"] == failing_message.id
            assert failure["phase"] == "analysis"
            assert failure["error_type"] == "RuntimeError"

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.gmail_after_message_id == first_message.id

            assert "secret-analysis-detail" not in json.dumps(result)
        finally:
            db.close()

    def test_next_cycle_retries_the_failed_message(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)
            failing_message = _seed_message(db, body_plain=OFFER_BODY)

            import app.services.automation_gmail as gmail_module

            original = gmail_module.analyze_gmail_message

            def _boom_once(db, account_key, gmail_message_id):
                if gmail_message_id == failing_message.id:
                    raise RuntimeError("boom")
                return original(db, account_key, gmail_message_id)

            monkeypatch.setattr("app.services.automation_gmail.analyze_gmail_message", _boom_once)
            first_result = _run_gmail_drafts(db, _settings())
            assert first_result["counters"]["failed"] == 1

            monkeypatch.undo()
            second_result = _run_gmail_drafts(db, _settings())
            processed_ids = [item["gmail_message_id"] for item in second_result["items"]]
            assert failing_message.id in processed_ids
            assert second_result["counters"]["failed"] == 0
        finally:
            db.close()


class TestDraftFailureAfterSuccessfulAnalysis:
    def test_cursor_not_advanced_and_next_cycle_reuses_analysis(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            message = _seed_message(db, body_plain=OFFER_BODY)

            def _boom(db, account_key, gmail_message_id):
                raise RuntimeError("secret-draft-detail")

            monkeypatch.setattr(
                "app.services.automation_gmail.generate_response_draft_for_message", _boom
            )

            first = _run_gmail_drafts(db, _settings())
            assert first["counters"]["failed"] == 1
            assert first["items"][0]["analysis_id"] is not None
            assert first["items"][0]["analysis_created"] is True
            assert first["items"][0]["phase"] == "response_draft"
            assert "secret-draft-detail" not in json.dumps(first)

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.gmail_after_message_id is None  # not advanced

            call_count = 0
            import app.services.automation_gmail as gmail_module

            original_analyze = gmail_module.analyze_gmail_message

            def _counting_analyze(db, account_key, gmail_message_id):
                nonlocal call_count
                call_count += 1
                return original_analyze(db, account_key, gmail_message_id)

            monkeypatch.setattr(
                "app.services.automation_gmail.analyze_gmail_message", _counting_analyze
            )
            monkeypatch.undo()  # remove the draft-failure patch first
            monkeypatch.setattr(
                "app.services.automation_gmail.analyze_gmail_message", _counting_analyze
            )

            second = _run_gmail_drafts(db, _settings())
            assert second["counters"]["failed"] == 0
            assert second["items"][0]["analysis_created"] is False  # reused, not recomputed
            assert second["items"][0]["response_draft_id"] is not None

            progress_after = get_mail_progress(db, ACCOUNT)
            assert progress_after.gmail_after_message_id == message.id
        finally:
            db.close()


class TestNoResponseRecommended:
    def test_counts_as_processed_and_advances_cursor(self, session_factory):
        db = session_factory()
        try:
            message = _seed_message(db, body_plain=UNKNOWN_BODY)
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["failed"] == 0
            assert result["counters"]["no_response_recommended"] == 1
            assert result["items"][0]["response_status"] == "NO_RESPONSE_RECOMMENDED"

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.gmail_after_message_id == message.id
        finally:
            db.close()


class TestGmailIdempotency:
    def test_repeated_retry_never_duplicates_analysis_or_draft(self, session_factory):
        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)
            _run_gmail_drafts(db, _settings())
            _run_gmail_drafts(db, _settings())  # cursor already past everything -- 0 scanned

            assert db.query(GmailMessageAnalysisRecord).count() == 1
            from app.db.models import ResponseDraftRecord

            assert db.query(ResponseDraftRecord).count() == 1
        finally:
            db.close()


# --- P. CAS race (Gmail cursor) -----------------------------------------------


class TestGmailCursorCASRace:
    def test_stale_owner_cannot_overwrite_newer_cursor(self, session_factory):
        db = session_factory()
        try:
            get_or_create_mail_progress(db, ACCOUNT)
            ok = advance_gmail_cursor(db, ACCOUNT, expected_cursor=None, new_cursor=5)
            assert ok is True

            # A stale owner still believes the cursor is None (its own
            # earlier observation) -- its CAS must fail closed, never
            # overwrite the newer value.
            stale_write = advance_gmail_cursor(db, ACCOUNT, expected_cursor=None, new_cursor=10)
            assert stale_write is False

            progress = get_mail_progress(db, ACCOUNT)
            assert progress.gmail_after_message_id == 5  # unchanged by the stale write
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
                results[name] = advance_gmail_cursor(
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
            assert sorted(results.values()) == [False, True]  # exactly one winner
        finally:
            db_a.close()
            db_b.close()


# --- Q. Account isolation -----------------------------------------------------


class TestAccountIsolation:
    def test_account_a_messages_cannot_enter_account_b_pipeline(self, session_factory):
        db = session_factory()
        try:
            message_a = _seed_message(db, account_key=ACCOUNT, body_plain=OFFER_BODY)
            message_b = _seed_message(db, account_key=OTHER_ACCOUNT, body_plain=OFFER_BODY)

            result = _run_gmail_drafts(db, _settings(), account_key=ACCOUNT)

            processed_ids = [item["gmail_message_id"] for item in result["items"]]
            assert message_a.id in processed_ids
            assert message_b.id not in processed_ids

            # message_b was never analyzed under account A's run.
            with pytest.raises(GmailMessageNotFoundError):
                analyze_gmail_message(db, ACCOUNT, message_b.id)
        finally:
            db.close()

    def test_cross_account_analysis_call_fails_closed(self, session_factory):
        db = session_factory()
        try:
            message_b = _seed_message(db, account_key=OTHER_ACCOUNT, body_plain=OFFER_BODY)
            with pytest.raises(GmailMessageNotFoundError):
                analyze_gmail_message(db, ACCOUNT, message_b.id)
            with pytest.raises(ResponseDraftMessageNotFoundError):
                generate_response_draft_for_message(db, ACCOUNT, message_b.id)
        finally:
            db.close()


# --- R. No send / no approval -------------------------------------------------


class TestNoSendNoApproval:
    def test_automation_gmail_module_never_imports_send_or_approval_logic(self):
        """Spec section 20: "Trace the real runtime call graph. Do NOT
        rely only on source-string scans." -- `hasattr` proves the
        forbidden name was never bound into this module's own namespace
        via import, which a plain string search (defeated by mentioning
        the name in a docstring, as this module's own does to document
        the boundary) cannot guarantee.
        """
        import app.services.automation_gmail as module

        for forbidden in (
            "send_response_draft",
            "approve_or_reject_response_draft",
            "send_follow_up",
            "approve_or_reject_follow_up",
            "GmailSmtpProvider",
            "update_job_status",
        ):
            assert not hasattr(module, forbidden)

    def test_gmail_sync_module_never_imports_send_or_approval_logic(self):
        import app.services.gmail_sync as module

        for forbidden in (
            "send_response_draft",
            "approve_or_reject_response_draft",
            "send_follow_up",
            "approve_or_reject_follow_up",
            "GmailSmtpProvider",
        ):
            assert not hasattr(module, forbidden)

    def test_runtime_send_and_approval_mocks_are_never_invoked(self, session_factory, monkeypatch):
        send_response_draft_mock = _CallCounter()
        approve_response_draft_mock = _CallCounter()

        monkeypatch.setattr(
            "app.services.response_draft_send.send_response_draft", send_response_draft_mock
        )
        monkeypatch.setattr(
            "app.services.response_draft_send.approve_or_reject_response_draft",
            approve_response_draft_mock,
        )

        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)
            _run_gmail_drafts(db, _settings())

            assert send_response_draft_mock.call_count == 0
            assert approve_response_draft_mock.call_count == 0
        finally:
            db.close()


class _CallCounter:
    def __init__(self):
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        raise AssertionError("this send/approval function must never be called by Stage 8D")


# --- S. Job.status unchanged ---------------------------------------------------


class TestJobStatusUnchanged:
    def test_matched_job_status_is_never_mutated(self, session_factory):
        db = session_factory()
        try:
            job = JobRecord(
                fingerprint="fp-1",
                source="bundesagentur",
                title="Backend Engineer",
                company="Globex",
                location="Berlin",
                url="https://example.com/jobs/1",
                description="",
                score=80,
                recommendation="APPLY",
                status="APPLIED",
            )
            db.add(job)
            db.commit()

            _seed_message(db, body_plain=OFFER_BODY)
            _run_gmail_drafts(db, _settings())

            db.expire_all()
            assert db.get(JobRecord, job.id).status == "APPLIED"
        finally:
            db.close()


# --- T. Backward compatibility ------------------------------------------------


class TestBackwardCompatibility:
    def test_old_rows_without_gmail_steps_still_deserialize(self):
        old_results = {
            "bundesagentur": {"status": "ok", "counters": {"created": 1}, "error_type": None},
            "xing": {"status": "not_configured", "counters": None, "error_type": None},
        }
        parsed = {name: AutomationRunStepResult(**payload) for name, payload in old_results.items()}
        assert parsed["bundesagentur"].items is None
        assert parsed["bundesagentur"].failures is None

        run = AutomationRun(
            id=1,
            account_key=ACCOUNT,
            status="COMPLETED",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:01:00+00:00",
            results=old_results,
            error_summary=None,
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert "gmail_sync" not in run.results

    def test_gmail_shaped_items_and_failures_deserialize(self):
        result = AutomationRunStepResult(
            status="partial",
            counters={"scanned": 2, "failed": 1},
            items=[
                {
                    "gmail_message_id": 1,
                    "analysis_id": 5,
                    "response_draft_id": 9,
                    "response_status": "PROPOSED",
                    "analysis_created": True,
                    "draft_created": True,
                    "status": "ok",
                }
            ],
            failures=[{"gmail_message_id": 2, "phase": "analysis", "error_type": "RuntimeError"}],
        )
        assert result.items[0].gmail_message_id == 1
        assert result.failures[0].gmail_message_id == 2
