"""Stage 8E: source-scan regression tests that lock in the Telegram
hardening fixes -- mirrors
tests/test_scheduler_service.py::TestSafetyNoSendOrApprovalImports'
source-scan approach, applied to (1) the "no logger.exception/exc_info
anywhere in a Telegram runtime path" hardening requirement and (2) the
existing "Stage 8E never sends email / approves anything" safety
boundary, extended to the new digest modules.
"""

import inspect


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
