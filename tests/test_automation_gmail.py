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
from app.db.models import (
    GmailMessageAnalysisRecord,
    GmailMessageRecord,
    GmailThreadRecord,
    JobRecord,
    ResponseDraftRecord,
)
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


def _is_processed(db, message_id) -> bool:
    """AUD-004: the durable per-message completion marker this project's
    Stage 8D selection now relies on -- see
    `app.db.models.GmailMessageRecord.automation_processed_at`.
    """
    db.expire_all()
    return db.get(GmailMessageRecord, message_id).automation_processed_at is not None


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

            assert _is_processed(db, messages[0].id)
            assert _is_processed(db, messages[1].id)
            assert not _is_processed(db, messages[2].id)

            second = _run_gmail_drafts(db, settings)
            processed_ids_2 = [item["gmail_message_id"] for item in second["items"]]
            assert processed_ids_2 == [messages[2].id, messages[3].id]
        finally:
            db.close()


class TestGmailSuccessCursor:
    def test_analysis_and_draft_success_marks_message_processed(self, session_factory):
        db = session_factory()
        try:
            message = _seed_message(db, body_plain=OFFER_BODY)
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["scanned"] == 1
            assert result["counters"]["failed"] == 0
            assert result["items"][0]["status"] == "ok"
            assert result["items"][0]["response_status"] == "PROPOSED"

            assert _is_processed(db, message.id)
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

            assert _is_processed(db, first_message.id)
            assert not _is_processed(db, failing_message.id)
            assert not _is_processed(db, never_reached.id)

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
    def test_message_not_marked_processed_and_next_cycle_reuses_analysis(
        self, session_factory, monkeypatch
    ):
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

            assert not _is_processed(db, message.id)  # not marked

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

            assert _is_processed(db, message.id)
        finally:
            db.close()


class TestNoResponseRecommended:
    def test_counts_as_processed_and_marks_message_processed(self, session_factory):
        db = session_factory()
        try:
            message = _seed_message(db, body_plain=UNKNOWN_BODY)
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["failed"] == 0
            assert result["counters"]["no_response_recommended"] == 1
            assert result["items"][0]["response_status"] == "NO_RESPONSE_RECOMMENDED"

            assert _is_processed(db, message.id)
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


# --- S8D-PROGRESS-001: Gmail cursor CAS loss must never be "ok" -------------


class TestGmailCursorCASLossIsNonOk:
    def test_cas_loss_after_successful_pipeline_is_non_ok(self, session_factory, monkeypatch):
        monkeypatch.setattr(
            "app.services.automation_gmail.mark_message_automation_processed",
            lambda *a, **kw: False,
        )

        db = session_factory()
        try:
            message = _seed_message(db, body_plain=OFFER_BODY)
            result = _run_gmail_drafts(db, _settings())

            assert result["status"] != "ok"
            assert result["counters"]["failed"] == 1

            failure = result["failures"][0]
            assert failure["phase"] == "cursor"
            assert failure["error_type"] == "GmailMessageAutomationCASLostError"

            item = result["items"][0]
            assert item["status"] == "failed"
            assert item["phase"] == "cursor"
            # Truthful: the underlying work (analysis + draft) really did
            # commit -- only the per-message marker CAS was lost.
            assert item["analysis_id"] is not None
            assert item["response_draft_id"] is not None

            assert not _is_processed(db, message.id)  # never marked
        finally:
            db.close()

    def test_cas_loss_with_a_prior_success_is_partial(self, session_factory, monkeypatch):
        db = session_factory()
        try:
            first = _seed_message(db, body_plain=OFFER_BODY)
            second_message = _seed_message(db, body_plain=OFFER_BODY)

            import app.services.automation_gmail as gmail_module

            original = gmail_module.mark_message_automation_processed

            def _fail_on_second(db, message_id):
                if message_id == second_message.id:
                    return False
                return original(db, message_id)

            monkeypatch.setattr(
                "app.services.automation_gmail.mark_message_automation_processed",
                _fail_on_second,
            )

            result = _run_gmail_drafts(db, _settings())
            assert result["status"] == "partial"
            assert result["counters"]["failed"] == 1

            assert _is_processed(db, first.id)
            assert not _is_processed(db, second_message.id)
        finally:
            db.close()


# --- S8D-AUDIT-001: truthful analyzed_created/reused on draft failure -------


class TestTruthfulAnalysisCountersOnDraftFailure:
    def test_fresh_analysis_then_draft_failure_reports_analyzed_created(
        self, session_factory, monkeypatch
    ):
        def _boom(db, account_key, gmail_message_id):
            raise RuntimeError("secret-draft-detail")

        monkeypatch.setattr(
            "app.services.automation_gmail.generate_response_draft_for_message", _boom
        )

        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["analyzed_created"] == 1
            assert result["counters"]["analyzed_reused"] == 0
            assert result["counters"]["failed"] == 1
            assert result["items"][0]["analysis_created"] is True
        finally:
            db.close()

    def test_reused_analysis_then_draft_failure_reports_analyzed_reused(
        self, session_factory, monkeypatch
    ):
        db = session_factory()
        try:
            message = _seed_message(db, body_plain=OFFER_BODY)
            # Pre-populate the analysis via the real, unmodified Stage 7B
            # service -- exactly what the automation pipeline itself
            # would do on an earlier, successful cycle.
            analyze_gmail_message(db, ACCOUNT, message.id)

            def _boom(db, account_key, gmail_message_id):
                raise RuntimeError("secret-draft-detail")

            monkeypatch.setattr(
                "app.services.automation_gmail.generate_response_draft_for_message", _boom
            )

            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["analyzed_created"] == 0
            assert result["counters"]["analyzed_reused"] == 1
            assert result["counters"]["failed"] == 1
            assert result["items"][0]["analysis_created"] is False
        finally:
            db.close()


# --- S8D-SYNC-001/002: GmailSyncResult.failed must prevent "ok", and total ---
# --- failure (fetched > 0, failed == fetched) must be "failed", not "partial"


class TestGmailSyncCountsFailuresHonestly:
    def test_per_message_sync_failures_prevent_ok_status(self, session_factory, monkeypatch):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            if mailbox == settings.gmail_mailbox:
                return GmailSyncResult(fetched=2, created=1, duplicates=0, skipped=0, failed=1)
            return GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            result = _run_gmail_sync(db, _settings())
            assert result["status"] == "partial"
            assert result["counters"]["failed"] == 1
            assert result["counters"]["inbox_failed"] == 1
        finally:
            db.close()

    def test_zero_failures_and_no_exceptions_is_ok(self, session_factory, monkeypatch):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            return GmailSyncResult(fetched=1, created=1, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            result = _run_gmail_sync(db, _settings())
            assert result["status"] == "ok"
        finally:
            db.close()

    def test_total_failure_all_fetched_failed_is_failed(self, session_factory, monkeypatch):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            if mailbox == settings.gmail_mailbox:
                return GmailSyncResult(fetched=10, created=0, duplicates=0, skipped=0, failed=10)
            return GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            result = _run_gmail_sync(db, _settings())
            assert result["status"] == "failed"
            assert result["counters"]["fetched"] == 10
            assert result["counters"]["failed"] == 10
        finally:
            db.close()

    def test_mixed_success_and_failure_is_partial(self, session_factory, monkeypatch):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            if mailbox == settings.gmail_mailbox:
                return GmailSyncResult(fetched=10, created=7, duplicates=0, skipped=0, failed=3)
            return GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            result = _run_gmail_sync(db, _settings())
            assert result["status"] == "partial"
            assert result["counters"]["fetched"] == 10
            assert result["counters"]["failed"] == 3
        finally:
            db.close()

    def test_full_fetch_zero_failures_is_ok(self, session_factory, monkeypatch):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            if mailbox == settings.gmail_mailbox:
                return GmailSyncResult(fetched=10, created=10, duplicates=0, skipped=0, failed=0)
            return GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            result = _run_gmail_sync(db, _settings())
            assert result["status"] == "ok"
            assert result["counters"]["fetched"] == 10
            assert result["counters"]["failed"] == 0
        finally:
            db.close()


# --- S8D-PRIVACY-001: no account_key in Stage 8D logs -----------------------


class TestPrivacyNoAccountKeyInGmailLogs:
    def test_success_path_logs_never_contain_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        async def _fake_sync_mailbox(db, settings, account_key, mailbox, *, trusted_outbound):
            return GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)

        monkeypatch.setattr("app.services.automation_gmail.sync_mailbox", _fake_sync_mailbox)

        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)
            with caplog.at_level("DEBUG"):
                _run_gmail_sync(db, _settings())
                _run_gmail_drafts(db, _settings())
            assert ACCOUNT not in caplog.text
        finally:
            db.close()

    def test_failure_path_logs_never_contain_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "app.services.automation_gmail.mark_message_automation_processed",
            lambda *a, **kw: False,
        )
        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)
            with caplog.at_level("DEBUG"):
                _run_gmail_drafts(db, _settings())
                _run_gmail_sync(
                    db, _settings(gmail_username="different@example.com"), account_key=ACCOUNT
                )
            assert ACCOUNT not in caplog.text
        finally:
            db.close()


# --- S8D-OUTBOUND-001: Sent messages get analysis, never a draft -----------


class TestOutboundMessageAnalysisOnly:
    def test_outbound_message_gets_analysis_but_no_response_draft(self, session_factory):
        db = session_factory()
        try:
            message = _seed_message(db, body_plain=OFFER_BODY, direction="OUTBOUND")
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["outbound_analysis_only"] == 1
            assert result["counters"]["draft_created"] == 0
            assert result["counters"]["draft_reused"] == 0
            assert result["counters"]["no_response_recommended"] == 0
            assert result["counters"]["failed"] == 0
            assert result["status"] == "ok"

            item = result["items"][0]
            assert item["status"] == "ok"
            assert item["analysis_id"] is not None
            assert item["response_draft_id"] is None

            assert db.query(GmailMessageAnalysisRecord).count() == 1
            assert db.query(ResponseDraftRecord).count() == 0

            assert _is_processed(db, message.id)
        finally:
            db.close()

    def test_inbound_message_still_generates_a_response_draft(self, session_factory):
        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY, direction="INBOUND")
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["outbound_analysis_only"] == 0
            assert result["items"][0]["response_draft_id"] is not None
            assert db.query(ResponseDraftRecord).count() == 1
        finally:
            db.close()

    def test_mixed_outbound_and_inbound_batch(self, session_factory):
        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY, direction="OUTBOUND")
            _seed_message(db, body_plain=OFFER_BODY, direction="INBOUND")
            result = _run_gmail_drafts(db, _settings())

            assert result["counters"]["scanned"] == 2
            assert result["counters"]["outbound_analysis_only"] == 1
            assert result["counters"]["draft_created"] == 1
            assert db.query(ResponseDraftRecord).count() == 1
            assert db.query(GmailMessageAnalysisRecord).count() == 2
        finally:
            db.close()


# --- CAS failure propagates AutomationRun to PARTIAL ------------------------


class TestCASFailurePropagatesToOverallPartial:
    def test_gmail_cursor_cas_loss_makes_run_partial_when_core_collectors_ok(
        self, session_factory, monkeypatch
    ):
        monkeypatch.setattr("app.services.automation.run_bundesagentur", _noop_collector)
        monkeypatch.setattr("app.services.automation.run_xing", _noop_collector)
        monkeypatch.setattr(
            "app.services.automation_gmail.mark_message_automation_processed",
            lambda *a, **kw: False,
        )

        db = session_factory()
        try:
            _seed_message(db, body_plain=OFFER_BODY)
            run = asyncio.run(run_automation_cycle(db, account_key=ACCOUNT, settings=_settings()))

            assert run.status == "PARTIAL"
            results = json.loads(run.results_json)
            assert results["gmail_response_drafts"]["status"] != "ok"
        finally:
            db.close()


# --- AUD-004 (Astra R3): PostgreSQL sequence-allocation-vs-commit-order ------
# --- watermark race -----------------------------------------------------------


def _make_thread(db, *, account_key=ACCOUNT, thread_key="watermark-race-thread"):
    thread = GmailThreadRecord(thread_key=thread_key, subject="subj", account_key=account_key)
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return thread


def _make_message_with_explicit_id(
    db, *, id, thread_id, account_key=ACCOUNT, uid, subject, body_plain=OFFER_BODY
):
    """AUD-004 test helper: constructs a `GmailMessageRecord` with an
    EXPLICIT `id` (bypassing `upsert_message`'s autoincrement) so a test
    can control id-vs-commit-order directly -- see
    `TestPostgresCommitOrderWatermarkRace`'s class docstring for why this
    is necessary to deterministically reproduce the AUD-004 end state on
    SQLite.
    """
    message = GmailMessageRecord(
        id=id,
        thread_id=thread_id,
        account_key=account_key,
        mailbox="INBOX",
        uid_validity=100,
        uid=uid,
        message_id_header=f"<{uid}@watermark-race.example.com>",
        references_json="[]",
        to_addresses_json="[]",
        cc_addresses_json="[]",
        subject=subject,
        direction="INBOUND",
        body_plain=body_plain,
        body_truncated=False,
        has_html=False,
        attachments_json="[]",
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


class TestPostgresCommitOrderWatermarkRace:
    """AUD-004 (Astra R3) regression: PostgreSQL sequence allocation order
    is NOT commit-visibility order -- a transaction that obtains a LOWER
    `id` can commit strictly AFTER another transaction that obtained a
    HIGHER `id` and already committed. Concretely:

        Transaction A: INSERTs message A, obtains id=100, does NOT commit yet.
        Transaction B: INSERTs message B, obtains id=101, COMMITS first.
        Automation runs -- sees (and, under the old watermark, would mark
        past) only message B.
        Transaction A commits.

    Required result: message A must still be discovered and processed --
    NEVER permanently skipped merely because a HIGHER-id message was
    already scanned before A ever became visible.

    **What this proves (SQLite) vs. what it does not.** SQLite's
    single-writer lock means a second connection's INSERT genuinely
    BLOCKS until the first transaction commits or rolls back -- so SQLite
    itself cannot produce "a lower-id row commits after a higher-id one"
    via two REAL concurrent transactions the way PostgreSQL can. This
    test instead constructs the exact same OBSERVABLE end state directly
    (a lower-id row becomes visible strictly after a higher-id row was
    already scanned/processed) via explicit-id inserts, and proves the
    FIX's selection query -- `automation_processed_at IS NULL`, never an
    `id` comparison -- discovers message A regardless. This is a
    deterministic, repository/service-level simulation of the checkpoint
    logic's independence from commit ordering, exactly as it would behave
    under a genuine PostgreSQL race, but it does not exercise real
    PostgreSQL transaction/MVCC machinery. See
    tests/integration/test_gmail_watermark_postgres_concurrency.py for
    the matching proof against a REAL PostgreSQL server with two
    genuinely concurrent transactions -- skipped locally without
    `TEST_POSTGRES_URL`, always run in CI's `scheduler-postgres` job.
    """

    def test_lower_id_message_committed_after_higher_id_message_is_still_discovered(
        self, session_factory
    ):
        db = session_factory()
        try:
            thread = _make_thread(db)

            # "Transaction B": the higher id, committed FIRST -- the only
            # message automation sees (and fully processes) on its first
            # run.
            message_b = _make_message_with_explicit_id(
                db, id=101, thread_id=thread.id, uid=2, subject="B (higher id, commits first)"
            )
            first = _run_gmail_drafts(db, _settings())
            assert first["counters"]["scanned"] == 1
            assert first["items"][0]["gmail_message_id"] == message_b.id
            assert _is_processed(db, message_b.id)

            # "Transaction A": the LOWER id, but only becomes visible
            # (commits) AFTER automation already scanned/processed B --
            # exactly the observable end state a PostgreSQL
            # sequence-allocation-vs-commit-order race produces.
            message_a = _make_message_with_explicit_id(
                db, id=100, thread_id=thread.id, uid=1, subject="A (lower id, commits later)"
            )
            assert message_a.id < message_b.id

            # Required invariant: A is still discovered and processed on
            # the very next scan -- never permanently skipped.
            second = _run_gmail_drafts(db, _settings())
            assert second["counters"]["scanned"] == 1
            assert second["items"][0]["gmail_message_id"] == message_a.id
            assert second["items"][0]["status"] == "ok"
            assert _is_processed(db, message_a.id)

            # A third scan finds nothing new -- both messages fully
            # processed exactly once, no duplicate side effects.
            third = _run_gmail_drafts(db, _settings())
            assert third["counters"]["scanned"] == 0
            assert db.query(GmailMessageAnalysisRecord).count() == 2
            assert db.query(ResponseDraftRecord).count() == 2
        finally:
            db.close()

    def test_zero_eligible_messages_is_a_clean_ok_noop(self, session_factory):
        db = session_factory()
        try:
            result = _run_gmail_drafts(db, _settings())
            assert result["counters"]["scanned"] == 0
            assert result["counters"]["failed"] == 0
            assert result["status"] == "ok"
            assert result["items"] == []
            assert result["failures"] == []
        finally:
            db.close()

    def test_account_isolation_holds_for_the_new_marker_based_selection(self, session_factory):
        """AUD-004's new selection query filters by `account_key` exactly
        like the old one did -- a message committed out of id-order for
        ONE account must never be discovered by a scan scoped to a
        DIFFERENT account.
        """
        db = session_factory()
        try:
            thread_a = _make_thread(db, account_key=ACCOUNT, thread_key="race-thread-a")
            thread_b = _make_thread(db, account_key=OTHER_ACCOUNT, thread_key="race-thread-b")

            message_b_other_account = _make_message_with_explicit_id(
                db, id=201, thread_id=thread_b.id, account_key=OTHER_ACCOUNT, uid=2, subject="B"
            )
            _run_gmail_drafts(db, _settings(), account_key=OTHER_ACCOUNT)
            assert _is_processed(db, message_b_other_account.id)

            message_a_this_account = _make_message_with_explicit_id(
                db, id=200, thread_id=thread_a.id, account_key=ACCOUNT, uid=1, subject="A"
            )

            result = _run_gmail_drafts(db, _settings(), account_key=ACCOUNT)
            assert result["counters"]["scanned"] == 1
            assert result["items"][0]["gmail_message_id"] == message_a_this_account.id
            assert _is_processed(db, message_a_this_account.id)
        finally:
            db.close()
