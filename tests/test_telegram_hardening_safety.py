"""Stage 8E: source-scan regression tests that lock in the Telegram
hardening fixes -- mirrors
tests/test_scheduler_service.py::TestSafetyNoSendOrApprovalImports'
source-scan approach, applied to (1) the "no logger.exception/exc_info
anywhere in a Telegram runtime path" hardening requirement and (2) the
existing "Stage 8E never sends email / approves anything" safety
boundary, extended to the new digest modules.
"""

import inspect
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.services.scheduler import run_due_digest_if_claimed
from app.services.telegram import TelegramSendOutcome

DISTINCTIVE_ACCOUNT = "must-never-leak@example.com"
DUE_UTC = datetime(2026, 9, 8, 7, 0, tzinfo=UTC)  # 09:00 Europe/Berlin -- past hour=8 gate


class TestNoTracebackLoggingInTelegramModules:
    def test_telegram_service_never_uses_logger_exception_or_exc_info(self):
        import app.services.telegram as module

        source = inspect.getsource(module)
        assert "logger.exception" not in source
        assert "exc_info=" not in source

    def test_telegram_bot_never_uses_logger_exception_or_exc_info(self):
        import app.services.telegram_bot as module

        source = inspect.getsource(module)
        assert "logger.exception" not in source
        assert "exc_info=" not in source

    def test_telegram_digest_never_uses_logger_exception_or_exc_info(self):
        import app.services.telegram_digest as module

        source = inspect.getsource(module)
        assert "logger.exception" not in source
        assert "exc_info=" not in source


class TestNoBotTokenOrUrlLogging:
    def test_telegram_service_never_logs_the_request_url_or_bot_token_variable(self):
        """The request URL embeds the bot token
        (f"https://api.telegram.org/bot{bot_token}/..."); this proves no
        logger.* call in the module references the `url` local at all --
        the only two places `url` is used are building the request and
        passing it to httpx, never a log call.
        """
        import app.services.telegram as module

        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("logger."):
                assert "url" not in stripped
                assert "bot_token" not in stripped
                assert "token" not in stripped


class TestNoAccountKeyInDigestSchedulerLogs:
    """Codex Stage 8E HIGH finding (PRIVACY): account_key (a normalized
    email address) must never appear in Stage 8E runtime logs --
    delivery_id/digest_date/status/counts are the only safe identifiers
    for locating a specific digest delivery in logs.
    """

    @pytest.fixture()
    def session_factory(self, tmp_path):
        db_path = tmp_path / "test_no_account_key_in_logs.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _settings(self) -> Settings:
        return Settings(
            automation_scheduler_account_key=DISTINCTIVE_ACCOUNT,
            telegram_daily_digest_enabled=True,
            telegram_daily_digest_hour=8,
            telegram_daily_digest_timezone="Europe/Berlin",
            telegram_bot_token="test-token",
            telegram_chat_id="999",
        )

    @pytest.mark.asyncio
    async def test_sent_outcome_never_logs_account_key(self, session_factory, monkeypatch, caplog):
        async def _send(bot_token, chat_id, text, *, timeout_seconds):
            return TelegramSendOutcome.SENT

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _send)

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                await run_due_digest_if_claimed(
                    db, account_key=DISTINCTIVE_ACCOUNT, settings=self._settings(), now=DUE_UTC
                )
        finally:
            db.close()

        assert DISTINCTIVE_ACCOUNT not in caplog.text

    @pytest.mark.asyncio
    async def test_failed_outcome_never_logs_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        async def _send(bot_token, chat_id, text, *, timeout_seconds):
            return TelegramSendOutcome.FAILED

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _send)

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                await run_due_digest_if_claimed(
                    db, account_key=DISTINCTIVE_ACCOUNT, settings=self._settings(), now=DUE_UTC
                )
        finally:
            db.close()

        assert DISTINCTIVE_ACCOUNT not in caplog.text

    @pytest.mark.asyncio
    async def test_uncertain_outcome_never_logs_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        async def _send(bot_token, chat_id, text, *, timeout_seconds):
            return TelegramSendOutcome.UNCERTAIN

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _send)

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                await run_due_digest_if_claimed(
                    db, account_key=DISTINCTIVE_ACCOUNT, settings=self._settings(), now=DUE_UTC
                )
        finally:
            db.close()

        assert DISTINCTIVE_ACCOUNT not in caplog.text

    @pytest.mark.asyncio
    async def test_unexpected_exception_never_logs_account_key(
        self, session_factory, monkeypatch, caplog
    ):
        async def _boom(bot_token, chat_id, text, *, timeout_seconds):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _boom)

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                await run_due_digest_if_claimed(
                    db, account_key=DISTINCTIVE_ACCOUNT, settings=self._settings(), now=DUE_UTC
                )
        finally:
            db.close()

        assert DISTINCTIVE_ACCOUNT not in caplog.text

    def test_run_due_digest_if_claimed_source_never_logs_account_key_variable(self):
        """Source-scan pin: no multi-line `logger.*(...)` call inside
        app.services.scheduler.run_due_digest_if_claimed ever passes the
        `account_key` local as a format argument -- catches a future
        regression even if a test's specific account_key string
        happened not to trip the runtime checks above."""
        import ast
        import textwrap

        import app.services.scheduler as module

        source = textwrap.dedent(inspect.getsource(module.run_due_digest_if_claimed))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("debug", "info", "warning", "error", "exception")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                for arg in node.args:
                    assert not (isinstance(arg, ast.Name) and arg.id == "account_key")


class TestStage8ENeverSendsOrApproves:
    """Extends the existing app.services.scheduler/app.scheduler safety
    scan (tests/test_scheduler_service.py) to the new Stage 8E digest
    modules -- the digest is read-only reporting, never a send/approval/
    Job.status mutation path."""

    # Call-shaped (trailing "(") rather than bare words -- these modules'
    # own docstrings legitimately use plain English words like "SMTP" in
    # prose (e.g. comparing Telegram's sendMessage to SMTP), which a bare
    # substring check would false-positive on.
    FORBIDDEN = (
        "send_follow_up(",
        "send_response_draft(",
        "approve_or_reject(",
        "bewerbung_send(",
        "update_job_status(",
    )

    def test_telegram_digest_module_never_imports_send_or_approval_logic(self):
        import app.services.telegram_digest as module

        source = inspect.getsource(module)
        for forbidden in self.FORBIDDEN:
            assert forbidden not in source

    def test_telegram_digest_repository_module_never_imports_send_or_approval_logic(self):
        import app.db.telegram_digest_repository as module

        source = inspect.getsource(module)
        for forbidden in self.FORBIDDEN:
            assert forbidden not in source

    def test_telegram_service_module_never_imports_send_or_approval_logic(self):
        import app.services.telegram as module

        source = inspect.getsource(module)
        for forbidden in self.FORBIDDEN:
            assert forbidden not in source

    def test_scheduler_service_module_still_never_imports_send_or_approval_logic(self):
        """Re-asserted here (not just in tests/test_scheduler_service.py)
        because Stage 8E added new code (run_due_digest_if_claimed) to
        this exact module -- this pins the safety property against that
        new code specifically, independent of whether the older test
        file changes."""
        import app.services.scheduler as module

        source = inspect.getsource(module)
        for forbidden in self.FORBIDDEN:
            assert forbidden not in source
        assert "TelegramNotifier(" not in source
