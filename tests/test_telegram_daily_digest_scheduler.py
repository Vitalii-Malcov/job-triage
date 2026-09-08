"""Stage 8E tests for the optional DAILY Telegram digest's scheduling
layer -- `app.services.scheduler.run_due_digest_if_claimed` (hour gate,
once-per-local-date idempotency, FAILED-retry vs UNCERTAIN-never-retry,
timezone rollover) and `validate_scheduler_settings`'s extended
digest-specific checks. Never hits real Telegram -- `send_telegram_text`
is monkeypatched to a deterministic fake outcome per test.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.telegram_digest_repository import get_delivery
from app.services.scheduler import (
    SchedulerConfigurationError,
    run_due_digest_if_claimed,
    validate_scheduler_settings,
)
from app.services.telegram import TelegramSendOutcome

ACCOUNT = "me@example.com"


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "test_telegram_daily_digest_scheduler.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _settings(**overrides) -> Settings:
    defaults = {
        "automation_scheduler_account_key": ACCOUNT,
        "telegram_daily_digest_enabled": True,
        "telegram_daily_digest_hour": 8,
        "telegram_daily_digest_timezone": "Europe/Berlin",
        "telegram_bot_token": "test-token",
        "telegram_chat_id": "999",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _fake_send(monkeypatch, outcomes):
    """Monkeypatches app.services.scheduler.send_telegram_text to return
    each of `outcomes` in order (one per call) and records call count."""
    calls = {"count": 0}
    iterator = iter(outcomes)

    async def _send(bot_token, chat_id, text, *, timeout_seconds):
        calls["count"] += 1
        return next(iterator)

    monkeypatch.setattr("app.services.scheduler.send_telegram_text", _send)
    return calls


# 08:00 Europe/Berlin in September (CEST, UTC+2) is 06:00 UTC.
DUE_UTC = datetime(2026, 9, 8, 7, 0, tzinfo=UTC)  # 09:00 Berlin -- past the hour=8 gate
NOT_DUE_UTC = datetime(2026, 9, 8, 4, 0, tzinfo=UTC)  # 06:00 Berlin -- before the hour=8 gate


class TestHourGate:
    @pytest.mark.asyncio
    async def test_not_due_before_configured_local_hour_creates_no_row(self, session_factory):
        db = session_factory()
        try:
            triggered = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=NOT_DUE_UTC
            )
            assert triggered is False
            assert get_delivery(db, ACCOUNT, NOT_DUE_UTC.date()) is None
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_due_at_or_after_configured_local_hour_sends(self, session_factory, monkeypatch):
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT])
        try:
            triggered = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
            )
            assert triggered is True
            assert calls["count"] == 1
            delivery = get_delivery(db, ACCOUNT, DUE_UTC.astimezone(_berlin()).date())
            assert delivery is not None
            assert delivery.status == "SENT"
        finally:
            db.close()


class TestOncePerDate:
    @pytest.mark.asyncio
    async def test_second_tick_same_date_after_sent_does_not_resend(
        self, session_factory, monkeypatch
    ):
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT, TelegramSendOutcome.SENT])
        try:
            first = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
            )
            second = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC + timedelta(hours=1)
            )

            assert first is True
            assert second is False
            assert calls["count"] == 1  # send_telegram_text was never called a second time
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_restart_style_second_worker_sees_already_sent_and_skips(
        self, session_factory, monkeypatch
    ):
        """Simulates a restart / second concurrent scheduler worker: a
        FRESH db Session (as a new process would open) observes the
        already-SENT row for today and never calls send_telegram_text
        again."""
        db_first = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT])
        try:
            await run_due_digest_if_claimed(
                db_first, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
            )
        finally:
            db_first.close()

        db_second = session_factory()
        try:
            triggered_again = await run_due_digest_if_claimed(
                db_second,
                account_key=ACCOUNT,
                settings=_settings(),
                now=DUE_UTC + timedelta(minutes=5),
            )
            assert triggered_again is False
            assert calls["count"] == 1
        finally:
            db_second.close()


class TestFailedIsRetryableSameDate:
    @pytest.mark.asyncio
    async def test_failed_outcome_can_be_retried_and_eventually_sent(
        self, session_factory, monkeypatch
    ):
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.FAILED, TelegramSendOutcome.SENT])
        try:
            first = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
            )
            second = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC + timedelta(hours=1)
            )

            assert first is True
            assert second is True
            assert calls["count"] == 2
            assert get_delivery(db, ACCOUNT, DUE_UTC.astimezone(_berlin()).date()).status == "SENT"
        finally:
            db.close()


class TestUncertainIsNeverRetried:
    @pytest.mark.asyncio
    async def test_uncertain_outcome_is_never_retried_same_date(self, session_factory, monkeypatch):
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.UNCERTAIN, TelegramSendOutcome.SENT])
        try:
            first = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
            )
            second = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC + timedelta(hours=1)
            )

            assert first is True
            assert second is False  # never retried -- claim_delivery sees UNCERTAIN, skips
            assert calls["count"] == 1
            assert get_delivery(db, ACCOUNT, DUE_UTC.astimezone(_berlin()).date()).status == (
                "UNCERTAIN"
            )
        finally:
            db.close()


class TestTimezoneRollover:
    @pytest.mark.asyncio
    async def test_next_local_date_is_claimable_independently(self, session_factory, monkeypatch):
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT, TelegramSendOutcome.SENT])
        try:
            day_one = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
            )
            day_two = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC + timedelta(days=1)
            )

            assert day_one is True
            assert day_two is True
            assert calls["count"] == 2
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_a_utc_instant_just_before_local_midnight_is_still_the_earlier_date(
        self, session_factory, monkeypatch
    ):
        """23:30 Berlin local time is still the SAME local date as
        09:00 Berlin earlier that day -- both must map to one claim,
        never two, even though the UTC clock date may differ from the
        local one near midnight."""
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT])
        try:
            morning = DUE_UTC  # 09:00 Berlin
            late_night_utc = DUE_UTC.replace(hour=21, minute=30)  # 23:30 Berlin (CEST, UTC+2)

            first = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=morning
            )
            second = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=late_night_utc
            )

            assert first is True
            assert second is False
            assert calls["count"] == 1
        finally:
            db.close()


def _berlin():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Europe/Berlin")


class TestValidateSchedulerSettingsDigestChecks:
    def test_digest_disabled_never_raises(self):
        validate_scheduler_settings(Settings(telegram_daily_digest_enabled=False))

    def test_digest_enabled_with_everything_configured_does_not_raise(self):
        validate_scheduler_settings(_settings())

    def test_digest_enabled_with_blank_account_key_raises(self):
        settings = Settings.model_construct(
            automation_scheduler_enabled=False,
            automation_scheduler_account_key="   ",
            telegram_daily_digest_enabled=True,
            telegram_daily_digest_hour=8,
            telegram_daily_digest_timezone="Europe/Berlin",
            telegram_bot_token="tok",
            telegram_chat_id="1",
        )
        with pytest.raises(SchedulerConfigurationError):
            validate_scheduler_settings(settings)

    def test_digest_enabled_with_blank_bot_token_raises(self):
        settings = Settings.model_construct(
            automation_scheduler_enabled=False,
            automation_scheduler_account_key=ACCOUNT,
            telegram_daily_digest_enabled=True,
            telegram_daily_digest_hour=8,
            telegram_daily_digest_timezone="Europe/Berlin",
            telegram_bot_token="   ",
            telegram_chat_id="1",
        )
        with pytest.raises(SchedulerConfigurationError):
            validate_scheduler_settings(settings)

    def test_digest_enabled_with_invalid_timezone_raises(self):
        settings = Settings.model_construct(
            automation_scheduler_enabled=False,
            automation_scheduler_account_key=ACCOUNT,
            telegram_daily_digest_enabled=True,
            telegram_daily_digest_hour=8,
            telegram_daily_digest_timezone="Not/ARealZone",
            telegram_bot_token="tok",
            telegram_chat_id="1",
        )
        with pytest.raises(SchedulerConfigurationError):
            validate_scheduler_settings(settings)

    def test_automation_disabled_alone_does_not_block_digest_validation(self):
        """automation_scheduler_enabled=False and
        telegram_daily_digest_enabled=True together must validate
        cleanly -- the two features are independent (see
        app.services.scheduler's module docstring)."""
        validate_scheduler_settings(
            Settings(
                automation_scheduler_enabled=False,
                automation_scheduler_account_key=ACCOUNT,
                telegram_daily_digest_enabled=True,
                telegram_daily_digest_hour=8,
                telegram_daily_digest_timezone="Europe/Berlin",
                telegram_bot_token="tok",
                telegram_chat_id="1",
            )
        )


class TestConfigLevelValidation:
    def test_settings_rejects_digest_enabled_with_blank_account_key(self):
        with pytest.raises(ValueError, match="telegram_daily_digest_enabled"):
            Settings(
                telegram_daily_digest_enabled=True,
                automation_scheduler_account_key="",
            )
