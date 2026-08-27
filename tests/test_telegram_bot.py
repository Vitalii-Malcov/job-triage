from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.telegram_bot as bot
from app.collectors.base import CollectorError, CollectorNotConfiguredError
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import upsert_job
from app.domain.status_transitions import InvalidStatusTransitionError
from app.models.application_status import ApplicationStatus
from app.models.job import Job, JobScore

AUTHORIZED_CHAT_ID = 12345


def _sample_job(**overrides) -> Job:
    data = {
        "source": "test",
        "title": "Python Developer",
        "company": "Acme GmbH",
        "url": "https://example.com/jobs/1",
    }
    data.update(overrides)
    return Job(**data)


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_telegram_bot.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def patch_bot_dependencies(monkeypatch, session_factory):
    fake_settings = Settings(
        telegram_bot_token="test-token", telegram_chat_id=str(AUTHORIZED_CHAT_ID)
    )
    monkeypatch.setattr(bot, "SessionLocal", session_factory)
    monkeypatch.setattr(bot, "get_settings", lambda: fake_settings)


def _make_update(chat_id: int, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    context = MagicMock()
    context.args = args or []
    return context


class TestAuthorization:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler", "args"),
        [
            (bot.cmd_start, []),
            (bot.cmd_help, []),
            (bot.cmd_jobs, []),
            (bot.cmd_job, ["1"]),
            (bot.cmd_status, ["1", "APPLIED"]),
            (bot.cmd_run, ["bundesagentur"]),
        ],
    )
    async def test_unauthorized_chat_gets_no_reply_but_is_logged(self, handler, args, monkeypatch):
        # Spying on bot.logger.warning directly (rather than using caplog)
        # avoids depending on the root logger's handler state, which other
        # tests in the same session mutate via configure_logging()
        # (app/core/logging.py's root.handlers.clear()) whenever the FastAPI
        # lifespan runs — that would make a caplog-based assertion here pass
        # or fail depending on test execution order.
        warning = MagicMock()
        monkeypatch.setattr(bot.logger, "warning", warning)

        update = _make_update(chat_id=999, text="poking around")
        context = _make_context(args)

        await handler(update, context)

        update.message.reply_text.assert_not_called()
        warning.assert_called_once_with(
            "telegram_bot_unauthorized_message chat_id=%s text=%s", "999", "poking around"
        )


class TestStartHelp:
    @pytest.mark.asyncio
    async def test_start_replies_with_help_text(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_start(update, _make_context())
        update.message.reply_text.assert_called_once_with(bot.HELP_TEXT)

    @pytest.mark.asyncio
    async def test_help_replies_with_help_text(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_help(update, _make_context())
        update.message.reply_text.assert_called_once_with(bot.HELP_TEXT)


class TestJobsCommand:
    @pytest.mark.asyncio
    async def test_lists_jobs(self, session_factory):
        db = session_factory()
        upsert_job(db, _sample_job(), JobScore(score=90, recommendation="APPLY"))
        db.close()

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_jobs(update, _make_context())

        text = update.message.reply_text.call_args[0][0]
        assert "Python Developer" in text
        assert "Acme GmbH" in text

    @pytest.mark.asyncio
    async def test_empty_list_reports_no_jobs(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_jobs(update, _make_context())
        update.message.reply_text.assert_called_once_with("No jobs found.")

    @pytest.mark.asyncio
    async def test_filters_by_status(self, session_factory):
        db = session_factory()
        upsert_job(db, _sample_job(), JobScore(score=90, recommendation="APPLY"))
        db.close()

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_jobs(update, _make_context(["APPLIED"]))

        update.message.reply_text.assert_called_once_with("No jobs found.")

    @pytest.mark.asyncio
    async def test_unknown_status_reports_error(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_jobs(update, _make_context(["NOT_A_STATUS"]))
        text = update.message.reply_text.call_args[0][0]
        assert "Unknown status 'NOT_A_STATUS'" in text


class TestJobCommand:
    @pytest.mark.asyncio
    async def test_shows_job_details(self, session_factory):
        db = session_factory()
        record, _ = upsert_job(db, _sample_job(), JobScore(score=90, recommendation="APPLY"))
        job_id = record.id
        db.close()

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_job(update, _make_context([str(job_id)]))

        text = update.message.reply_text.call_args[0][0]
        assert "Python Developer" in text
        assert "Acme GmbH" in text

    @pytest.mark.asyncio
    async def test_not_found(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_job(update, _make_context(["999"]))
        update.message.reply_text.assert_called_once_with("Job #999 not found.")

    @pytest.mark.asyncio
    async def test_usage_without_args(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_job(update, _make_context([]))
        update.message.reply_text.assert_called_once_with("Usage: /job <id>")

    @pytest.mark.asyncio
    async def test_usage_with_non_numeric_id(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_job(update, _make_context(["abc"]))
        update.message.reply_text.assert_called_once_with("Usage: /job <id> (id must be a number)")


class TestStatusCommand:
    @pytest.mark.asyncio
    async def test_updates_status(self, session_factory):
        db = session_factory()
        record, _ = upsert_job(db, _sample_job(), JobScore(score=90, recommendation="APPLY"))
        job_id = record.id
        db.close()

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_status(update, _make_context([str(job_id), "APPLIED"]))

        update.message.reply_text.assert_called_once_with(
            f"Job #{job_id} status updated to APPLIED."
        )

    @pytest.mark.asyncio
    async def test_invalid_transition_uses_domain_error_text_verbatim(self, session_factory):
        db = session_factory()
        record, _ = upsert_job(db, _sample_job(), JobScore(score=90, recommendation="APPLY"))
        job_id = record.id
        db.close()

        update = _make_update(AUTHORIZED_CHAT_ID)
        # NEW -> INTERVIEW is not an allowed transition.
        await bot.cmd_status(update, _make_context([str(job_id), "INTERVIEW"]))

        expected = str(
            InvalidStatusTransitionError(ApplicationStatus.NEW, ApplicationStatus.INTERVIEW)
        )
        update.message.reply_text.assert_called_once_with(expected)

    @pytest.mark.asyncio
    async def test_not_found(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_status(update, _make_context(["999", "APPLIED"]))
        update.message.reply_text.assert_called_once_with("Job #999 not found.")

    @pytest.mark.asyncio
    async def test_unknown_status_value(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_status(update, _make_context(["1", "NOT_A_STATUS"]))
        text = update.message.reply_text.call_args[0][0]
        assert "Unknown status 'NOT_A_STATUS'" in text

    @pytest.mark.asyncio
    async def test_usage_with_missing_args(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_status(update, _make_context(["1"]))
        update.message.reply_text.assert_called_once_with("Usage: /status <id> <new_status>")

    @pytest.mark.asyncio
    async def test_usage_with_non_numeric_id(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_status(update, _make_context(["abc", "APPLIED"]))
        update.message.reply_text.assert_called_once_with(
            "Usage: /status <id> <new_status> (id must be a number)"
        )


class TestRunCommand:
    @pytest.mark.asyncio
    async def test_bundesagentur_success(self, monkeypatch):
        stats = {"fetched": 3, "created": 2, "updated": 1, "skipped_invalid": 0, "failed": 0}
        monkeypatch.setattr(bot, "_run_bundesagentur", AsyncMock(return_value=stats))

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_run(update, _make_context(["bundesagentur"]))

        update.message.reply_text.assert_called_once_with(
            "bundesagentur collector run complete: fetched=3 created=2 updated=1 "
            "skipped_invalid=0 failed=0"
        )

    @pytest.mark.asyncio
    async def test_xing_not_configured(self, monkeypatch):
        monkeypatch.setattr(
            bot,
            "_run_xing",
            AsyncMock(side_effect=CollectorNotConfiguredError("XING mailbox not configured")),
        )

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_run(update, _make_context(["xing"]))

        update.message.reply_text.assert_called_once_with("XING mailbox not configured")

    @pytest.mark.asyncio
    async def test_upstream_failure(self, monkeypatch):
        monkeypatch.setattr(
            bot, "_run_bundesagentur", AsyncMock(side_effect=CollectorError("boom"))
        )

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_run(update, _make_context(["bundesagentur"]))

        update.message.reply_text.assert_called_once_with(
            "bundesagentur collector run failed: boom"
        )

    @pytest.mark.asyncio
    async def test_usage_with_unknown_collector_name(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_run(update, _make_context(["not-a-collector"]))
        update.message.reply_text.assert_called_once_with("Usage: /run bundesagentur|xing")

    @pytest.mark.asyncio
    async def test_usage_with_no_args(self):
        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_run(update, _make_context([]))
        update.message.reply_text.assert_called_once_with("Usage: /run bundesagentur|xing")


class TestJobsMessageLimit:
    @pytest.mark.asyncio
    async def test_reply_stays_under_telegram_limit_with_adversarially_long_fields(
        self, session_factory
    ):
        db = session_factory()
        for i in range(20):
            job = _sample_job(
                title="X" * 200,
                company="Y" * 200,
                url=f"https://example.com/jobs/{i}",
            )
            upsert_job(db, job, JobScore(score=90, recommendation="APPLY"))
        db.close()

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_jobs(update, _make_context())

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert len(text) <= 4096
        assert "more" in text or text.count("\n") < 19

    @pytest.mark.asyncio
    async def test_typical_short_fields_are_not_truncated(self, session_factory):
        db = session_factory()
        for i in range(20):
            job = _sample_job(
                title="Junior Python Developer (m/w/d)",
                company="Acme Software Solutions GmbH",
                url=f"https://example.com/jobs/{i}",
            )
            upsert_job(db, job, JobScore(score=87, recommendation="APPLY"))
        db.close()

        update = _make_update(AUTHORIZED_CHAT_ID)
        await bot.cmd_jobs(update, _make_context())

        text = update.message.reply_text.call_args[0][0]
        assert "more" not in text
        assert text.count("\n") == 19


class TestStartBot:
    @pytest.mark.asyncio
    async def test_returns_none_when_token_not_configured(self):
        settings = Settings(telegram_bot_token="")
        assert await bot.start_bot(settings) is None

    @pytest.mark.asyncio
    async def test_returns_none_and_does_not_raise_when_initialize_fails(self, monkeypatch):
        settings = Settings(
            telegram_bot_token="test-token", telegram_chat_id=str(AUTHORIZED_CHAT_ID)
        )

        async def failing_initialize(self):
            raise RuntimeError("The token was rejected by the server.")

        monkeypatch.setattr(bot.Application, "initialize", failing_initialize)

        result = await bot.start_bot(settings)

        assert result is None


class TestStopBot:
    @pytest.mark.asyncio
    async def test_is_noop_when_application_is_none(self):
        await bot.stop_bot(None)


class TestErrorHandler:
    @pytest.mark.asyncio
    async def test_logs_and_notifies_authorized_chat(self, monkeypatch):
        error_log = MagicMock()
        monkeypatch.setattr(bot.logger, "error", error_log)

        context = MagicMock()
        context.error = RuntimeError("boom")
        context.bot.send_message = AsyncMock()

        await bot._handle_error(update=None, context=context)

        error_log.assert_called_once()
        context.bot.send_message.assert_called_once_with(
            chat_id=str(AUTHORIZED_CHAT_ID), text="Internal error — check server logs."
        )

    @pytest.mark.asyncio
    async def test_swallows_failure_to_notify_chat(self, monkeypatch):
        monkeypatch.setattr(bot.logger, "error", MagicMock())
        warning_log = MagicMock()
        monkeypatch.setattr(bot.logger, "warning", warning_log)

        context = MagicMock()
        context.error = RuntimeError("boom")
        context.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram unreachable"))

        await bot._handle_error(update=None, context=context)  # must not raise

        warning_log.assert_called_once()
