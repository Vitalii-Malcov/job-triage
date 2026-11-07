"""OPS-02: readiness reflects the Telegram bot only when a bot is configured."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.routers import health as health_module
from app.core.config import Settings
from app.main import app
from app.services import component_health as ch
from app.services.component_health import BotState


@pytest.fixture(autouse=True)
def _reset_state():
    before = ch.telegram_state()
    yield
    ch.set_telegram_state(before)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(health_module, "check_readiness", lambda: True)
    return TestClient(app)


def test_liveness_ignores_bot_state(client):
    ch.set_telegram_state(BotState.FAILED)
    assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize("state", [BotState.DISABLED, BotState.RUNNING])
def test_ready_when_bot_off_or_running(client, state):
    ch.set_telegram_state(state)
    assert client.get("/api/v1/ready").status_code == 200


@pytest.mark.parametrize(
    "state", [BotState.STARTING, BotState.RETRYING, BotState.FAILED, BotState.STOPPED]
)
def test_not_ready_when_required_bot_unavailable(client, state):
    ch.set_telegram_state(state)
    response = client.get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_not_ready_when_database_down_even_if_bot_running(monkeypatch):
    monkeypatch.setattr(health_module, "check_readiness", lambda: False)
    ch.set_telegram_state(BotState.RUNNING)
    assert TestClient(app).get("/api/v1/ready").status_code == 503


def test_supervisor_failure_and_recovery_drive_readiness(monkeypatch):
    from app.services import telegram_bot

    calls = {"n": 0}
    seen: list[BotState] = []

    async def attempt(_settings):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, "NetworkError"
        return SimpleNamespace(), None

    async def fake_stop(_application):
        return None

    async def sleep(_delay):
        seen.append(ch.telegram_state())

    monkeypatch.setattr(telegram_bot, "_start_bot_attempt", attempt)
    monkeypatch.setattr(telegram_bot, "stop_bot", fake_stop)

    async def scenario():
        task = asyncio.create_task(telegram_bot._run_bot_supervised(object(), sleep=sleep))
        for _ in range(20):
            await asyncio.sleep(0)
        assert ch.telegram_state() is BotState.RUNNING
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert seen == [BotState.RETRYING]
    assert ch.telegram_state() is BotState.STOPPED


def test_invalid_token_is_terminal(monkeypatch):
    from app.services import telegram_bot

    async def attempt(_settings):
        return None, "InvalidToken"

    monkeypatch.setattr(telegram_bot, "_start_bot_attempt", attempt)
    asyncio.run(telegram_bot._run_bot_supervised(object()))
    assert ch.telegram_state() is BotState.FAILED
    assert ch.telegram_ready() is False


def test_start_without_token_is_disabled():
    from app.services import telegram_bot

    ch.set_telegram_state(BotState.FAILED)
    settings = SimpleNamespace(telegram_bot_token=None)
    assert asyncio.run(telegram_bot.start_bot_supervised(settings)) is None
    assert ch.telegram_state() is BotState.DISABLED


def _settings(**kw):
    base = dict(
        automation_scheduler_enabled=True,
        telegram_daily_digest_enabled=False,
        automation_scheduler_interval_seconds=3600,
        automation_scheduler_poll_seconds=15,
    )
    return SimpleNamespace(**{**base, **kw})


class _Db:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self, _stmt):
        return SimpleNamespace(all=lambda: self._rows)


def test_scheduler_state_disabled_ok_stale_unknown_and_recovery():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    off = _settings(automation_scheduler_enabled=False)
    assert ch.scheduler_state(_Db([]), off, now) == "disabled"
    assert ch.scheduler_state(_Db([]), _settings(), now) == "unknown"
    fresh = SimpleNamespace(next_run_at=now - timedelta(seconds=30))
    assert ch.scheduler_state(_Db([fresh]), _settings(), now) == "ok"
    dead = SimpleNamespace(next_run_at=now - timedelta(hours=3))
    assert ch.scheduler_state(_Db([dead]), _settings(), now) == "stale"
    recovered = SimpleNamespace(next_run_at=now + timedelta(hours=1))
    assert ch.scheduler_state(_Db([recovered]), _settings(), now) == "ok"


def test_expired_run_leases_counts_only_lapsed_running_rows():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(status="RUNNING", lease_expires_at=now - timedelta(minutes=1)),
        SimpleNamespace(status="RUNNING", lease_expires_at=now + timedelta(minutes=1)),
        SimpleNamespace(status="RUNNING", lease_expires_at=None),
    ]
    assert ch.expired_run_leases(_Db(rows), now) == 1


def test_components_report_has_states_only_and_survives_db_errors():
    class Boom:
        def scalars(self, _stmt):
            raise RuntimeError("secret detail")

    ch.set_telegram_state(BotState.RETRYING)
    report = ch.components_report(Boom(), _settings(), database_ready=True)
    assert report == {
        "database": "ok",
        "telegram": "retrying",
        "scheduler": "unknown",
        "collector_leases": "unknown",
    }
    assert ch.components_report(None, _settings(), database_ready=False)["database"] == "down"


def test_components_endpoint_never_leaks_detail(monkeypatch):
    monkeypatch.setattr(health_module, "check_readiness", lambda: False)
    ch.set_telegram_state(BotState.FAILED)
    fake = Settings(api_key="k" * 24, rate_limit_requests=1000, rate_limit_window_seconds=60)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake)
    client = TestClient(app)
    assert client.get("/api/v1/health/components").status_code == 401
    response = client.get("/api/v1/health/components", headers={"X-API-Key": "k" * 24})
    assert response.status_code == 200
    assert response.json()["telegram"] == "failed"
    assert response.json()["database"] == "down"


def test_a_poller_that_dies_after_start_turns_readiness_red_and_restarts(monkeypatch):
    from app.services import telegram_bot

    applications = []
    states_when_stopped = []

    async def attempt(_settings):
        app_ = SimpleNamespace(updater=SimpleNamespace(running=True))
        applications.append(app_)
        return app_, None

    async def fake_stop(application):
        states_when_stopped.append((application, ch.telegram_state()))

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        if len(applications) == 1:
            applications[0].updater.running = False  # token revoked: poller exits
        await real_sleep(0)

    monkeypatch.setattr(telegram_bot, "_start_bot_attempt", attempt)
    monkeypatch.setattr(telegram_bot, "stop_bot", fake_stop)
    monkeypatch.setattr(telegram_bot.asyncio, "sleep", fast_sleep)

    async def scenario():
        task = asyncio.create_task(telegram_bot._run_bot_supervised(object()))
        for _ in range(50):
            await real_sleep(0)
        assert len(applications) == 2  # restarted
        assert ch.telegram_state() is BotState.RUNNING
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert states_when_stopped[0] == (applications[0], BotState.RETRYING)
