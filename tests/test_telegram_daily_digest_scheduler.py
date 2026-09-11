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
from app.db.telegram_digest_repository import claim_delivery, get_delivery
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


class TestDSTTransitions:
    """hardening/api-boundaries-r1, Section 8: the prior adversarial pass
    (docs/ADVERSARIAL_HARDENING_REPORT.md, Phase 11 verification-pass
    note) correctly downgraded its DST claim because it was mostly
    static reasoning plus a same-timezone-year-round comparison
    (TestTimezoneRollover above never actually crosses a DST boundary --
    September in Europe/Berlin is CEST/UTC+2 on both ends). These tests
    inject real UTC instants straddling the actual 2026 Europe/Berlin
    transitions (computed directly via zoneinfo, not guessed/hardcoded
    from memory -- spring-forward: local clocks jump 02:00->03:00 CEST
    on 2026-03-29, the 02:00-03:00 hour never occurs; fall-back: local
    02:00-03:00 occurs TWICE on 2026-10-25, first as CEST/UTC+2 then
    again as CET/UTC+1). No real time.sleep -- every instant is passed
    explicitly via `now=`.
    """

    # UTC 2026-03-29T01:00:00 is the first instant of Berlin's new
    # UTC+2 offset -- local time is 03:00 (hour jumped straight from
    # 01:59:59 to 03:00:00, skipping the 02:00 hour entirely).
    SPRING_FORWARD_JUST_BEFORE_UTC = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)  # local 01:30, hour=1
    SPRING_FORWARD_JUST_AFTER_UTC = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # local 03:30, hour=3

    # UTC 2026-10-25T00:30 -> local 02:30 CEST (UTC+2, first occurrence).
    # UTC 2026-10-25T01:30 -> local 02:30 CET (UTC+1, second occurrence,
    # SAME calendar date, SAME local wall-clock time as the first).
    FALL_BACK_FIRST_0230_UTC = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    FALL_BACK_SECOND_0230_UTC = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_spring_forward_gate_still_fires_once_the_skipped_hour_has_passed(
        self, session_factory, monkeypatch
    ):
        """digest_hour=2 -- a hour that literally does not exist as a
        local wall-clock value on 2026-03-29 in Europe/Berlin. The
        "not-before" inequality (`local_now.hour < digest_hour`) must
        still correctly gate: not due at local hour 1 (before the
        skip), due at local hour 3 (after the skip landed past the
        nonexistent hour 2) -- confirms the prior audit's static claim
        ("survives a spring-forward hour-skip") against a REAL
        transition, not just the general principle.
        """
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT])
        try:
            settings = _settings(telegram_daily_digest_hour=2)

            before = await run_due_digest_if_claimed(
                db,
                account_key=ACCOUNT,
                settings=settings,
                now=self.SPRING_FORWARD_JUST_BEFORE_UTC,
            )
            assert before is False
            assert calls["count"] == 0

            after = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=settings, now=self.SPRING_FORWARD_JUST_AFTER_UTC
            )
            assert after is True
            assert calls["count"] == 1
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_fall_back_repeated_local_hour_never_sends_twice(
        self, session_factory, monkeypatch
    ):
        """digest_hour=2 -- local wall-clock 02:30 occurs TWICE on
        2026-10-25 (once as CEST, once as CET, one hour apart in real
        UTC time but identical local time). Both ticks map to the SAME
        calendar date (2026-10-25), so the once-per-`(account_key,
        digest_date)` CAS must permit exactly ONE send, not two --
        confirms the prior audit's static claim ("survives a fall-back
        repeated hour without double-sending") against the actual
        repeated-hour instants, not just the general once-per-date
        principle already covered elsewhere in this file for
        non-DST-adjacent dates.
        """
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT])
        try:
            settings = _settings(telegram_daily_digest_hour=2)

            first_tick = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=settings, now=self.FALL_BACK_FIRST_0230_UTC
            )
            second_tick = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=settings, now=self.FALL_BACK_SECOND_0230_UTC
            )

            assert first_tick is True
            assert second_tick is False  # same digest_date, already SENT -- claim lost
            assert calls["count"] == 1
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_fall_back_day_after_still_advances_to_a_new_claimable_date(
        self, session_factory, monkeypatch
    ):
        """The calendar day AFTER the fall-back transition (2026-10-26,
        by which point Berlin is settled into CET/UTC+1) must still be
        an independently claimable digest_date -- the repeated-hour
        transition day must not leave the CAS keying in a state that
        confuses the following, DST-transition-free day.
        """
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT, TelegramSendOutcome.SENT])
        try:
            settings = _settings(telegram_daily_digest_hour=8)

            transition_day = await run_due_digest_if_claimed(
                db,
                account_key=ACCOUNT,
                settings=settings,
                now=datetime(2026, 10, 25, 7, 0, tzinfo=UTC),  # 08:00 CET local (past hour=8 gate)
            )
            next_day = await run_due_digest_if_claimed(
                db,
                account_key=ACCOUNT,
                settings=settings,
                now=datetime(2026, 10, 26, 7, 0, tzinfo=UTC),  # 08:00 CET local, next calendar day
            )

            assert transition_day is True
            assert next_day is True
            assert calls["count"] == 2
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


class TestStalePendingReconciliationViaScheduler:
    """Codex Stage 8E MEDIUM finding (STALE PENDING), exercised through
    the actual scheduler entrypoint `run_due_digest_if_claimed` (not
    just the repository primitive directly -- see
    tests/test_telegram_digest_repository.py::TestReconcileStalePendingToUncertain
    for that lower-level proof).
    """

    @pytest.mark.asyncio
    async def test_fresh_pending_claim_causes_no_second_send_this_tick(
        self, session_factory, monkeypatch
    ):
        """Simulates a concurrent claimer/attempt genuinely still in
        flight (a PENDING row that was JUST claimed) -- a second tick
        observing it must never send, and must never reconcile it
        either (it isn't stale yet)."""
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT])
        try:
            digest_date = DUE_UTC.astimezone(_berlin()).date()
            # Simulates a live concurrent claim.
            claim_delivery(db, ACCOUNT, digest_date, now=DUE_UTC)

            triggered = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
            )

            assert triggered is False
            assert calls["count"] == 0  # never sent
            assert get_delivery(db, ACCOUNT, digest_date).status == "PENDING"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_stale_pending_claim_is_reconciled_to_uncertain_without_sending(
        self, session_factory, monkeypatch
    ):
        """A PENDING claim old enough to be stale (the process that won
        it crashed before resolving the outcome) must be reconciled to
        UNCERTAIN -- and the SAME tick that reconciles it must NEVER
        also attempt a send."""
        db = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT])
        try:
            digest_date = DUE_UTC.astimezone(_berlin()).date()
            claim_delivery(db, ACCOUNT, digest_date, now=DUE_UTC)  # simulates the crashed claimer

            # A later tick, long enough after the claim for it to be
            # stale (STALE_PENDING_TTL_SECONDS default is 300s).
            later = DUE_UTC + timedelta(seconds=301)
            triggered = await run_due_digest_if_claimed(
                db, account_key=ACCOUNT, settings=_settings(), now=later
            )

            assert triggered is True  # an action (reconciliation) happened
            assert calls["count"] == 0  # never sent in the same tick
            assert get_delivery(db, ACCOUNT, digest_date).status == "UNCERTAIN"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_restart_after_stale_pending_reconciliation_never_sends(
        self, session_factory, monkeypatch
    ):
        """After a stale PENDING claim has been reconciled to
        UNCERTAIN, a FRESH restart-style Session (as a new worker
        process would open) must observe the terminal UNCERTAIN state
        and never attempt a send for the SAME calendar date."""
        db_first = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT, TelegramSendOutcome.SENT])
        try:
            digest_date = DUE_UTC.astimezone(_berlin()).date()
            claim_delivery(db_first, ACCOUNT, digest_date, now=DUE_UTC)

            later = DUE_UTC + timedelta(seconds=301)
            await run_due_digest_if_claimed(
                db_first, account_key=ACCOUNT, settings=_settings(), now=later
            )
        finally:
            db_first.close()

        db_second = session_factory()
        try:
            even_later = DUE_UTC + timedelta(seconds=600)
            triggered_again = await run_due_digest_if_claimed(
                db_second, account_key=ACCOUNT, settings=_settings(), now=even_later
            )

            assert triggered_again is False
            assert calls["count"] == 0  # never sent, before or after reconciliation
            assert get_delivery(db_second, ACCOUNT, digest_date).status == "UNCERTAIN"
        finally:
            db_second.close()

    @pytest.mark.asyncio
    async def test_concurrent_stale_reconciliation_only_one_worker_logs_the_action(
        self, session_factory, monkeypatch
    ):
        """Two independent Sessions (simulating two scheduler worker
        processes) both observing the same stale PENDING row on the
        same tick -- only one may win the reconciliation CAS; the
        loser must report "nothing to do" (False), never attempt a
        send, and never double-reconcile."""
        db_a = session_factory()
        db_b = session_factory()
        calls = _fake_send(monkeypatch, [TelegramSendOutcome.SENT, TelegramSendOutcome.SENT])
        try:
            digest_date = DUE_UTC.astimezone(_berlin()).date()
            claim_delivery(db_a, ACCOUNT, digest_date, now=DUE_UTC)

            later = DUE_UTC + timedelta(seconds=301)
            triggered_a = await run_due_digest_if_claimed(
                db_a, account_key=ACCOUNT, settings=_settings(), now=later
            )
            triggered_b = await run_due_digest_if_claimed(
                db_b, account_key=ACCOUNT, settings=_settings(), now=later
            )

            # Exactly one of the two performed the reconciliation;
            # neither ever sent.
            assert {triggered_a, triggered_b} == {True, False}
            assert calls["count"] == 0
            assert get_delivery(db_a, ACCOUNT, digest_date).status == "UNCERTAIN"
        finally:
            db_a.close()
            db_b.close()


class TestCASTruthfulnessOnMarkOutcome:
    """Codex Stage 8E finding (CAS TRUTHFULNESS): if the CAS that
    persists a send outcome (mark_sent/mark_failed/mark_uncertain)
    LOSES -- e.g. a concurrent worker's stale-PENDING reconciliation
    already flipped the row away from PENDING first -- the scheduler
    must never silently log the outcome as if it had been recorded. A
    distinct, sanitized "not persisted" event must be logged instead,
    and the function must still return True (an attempt genuinely
    happened) without raising.
    """

    @pytest.mark.asyncio
    async def test_lost_mark_sent_cas_is_never_logged_as_a_successful_sent(
        self, session_factory, monkeypatch, caplog
    ):
        async def _send(bot_token, chat_id, text, *, timeout_seconds):
            return TelegramSendOutcome.SENT

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _send)
        monkeypatch.setattr("app.services.scheduler.mark_sent", lambda db, record: False)

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                triggered = await run_due_digest_if_claimed(
                    db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
                )

            assert triggered is True
            assert "telegram_daily_digest_outcome_not_persisted" in caplog.text
            assert "telegram_daily_digest_sent" not in caplog.text
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_lost_mark_failed_cas_is_never_logged_as_a_recorded_failure(
        self, session_factory, monkeypatch, caplog
    ):
        async def _send(bot_token, chat_id, text, *, timeout_seconds):
            return TelegramSendOutcome.FAILED

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _send)
        monkeypatch.setattr(
            "app.services.scheduler.mark_failed", lambda db, record, *, last_error: False
        )

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                triggered = await run_due_digest_if_claimed(
                    db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
                )

            assert triggered is True
            assert "telegram_daily_digest_outcome_not_persisted" in caplog.text
            assert "telegram_daily_digest_failed" not in caplog.text
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_lost_mark_uncertain_cas_is_never_logged_as_a_recorded_uncertain(
        self, session_factory, monkeypatch, caplog
    ):
        async def _send(bot_token, chat_id, text, *, timeout_seconds):
            return TelegramSendOutcome.UNCERTAIN

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _send)
        monkeypatch.setattr(
            "app.services.scheduler.mark_uncertain", lambda db, record, *, last_error: False
        )

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                triggered = await run_due_digest_if_claimed(
                    db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
                )

            assert triggered is True
            assert "telegram_daily_digest_outcome_not_persisted" in caplog.text
            # "telegram_daily_digest_uncertain" (the successfully-persisted
            # event name) must not appear -- only the not-persisted one.
            assert "telegram_daily_digest_uncertain " not in caplog.text
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_lost_mark_uncertain_cas_on_unexpected_exception_path(
        self, session_factory, monkeypatch, caplog
    ):
        """Mirrors the three tests above, but for the OTHER call site --
        the unexpected-exception branch's own mark_uncertain call."""

        async def _boom(bot_token, chat_id, text, *, timeout_seconds):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.services.scheduler.send_telegram_text", _boom)
        monkeypatch.setattr(
            "app.services.scheduler.mark_uncertain", lambda db, record, *, last_error: False
        )

        db = session_factory()
        try:
            with caplog.at_level("DEBUG"):
                triggered = await run_due_digest_if_claimed(
                    db, account_key=ACCOUNT, settings=_settings(), now=DUE_UTC
                )

            assert triggered is True
            assert "telegram_daily_digest_outcome_not_persisted" in caplog.text
        finally:
            db.close()
