"""Stage 13A-6: the startup safety-state log line is evidence built from a
closed allowlist -- it never carries a configured secret or identifier."""

import logging

from app.core.config import Settings
from app.core.startup_diagnostics import (
    SAFETY_FLAGS,
    log_startup_safety_state,
    startup_safety_fields,
)

SENTINELS = {
    "api_key": "sentinel-api-key-1a2b",
    "telegram_bot_token": "123456:sentinel-telegram-token",
    "telegram_chat_id": "987654321",
    "gmail_username": "sentinel.gmail@example.com",
    "gmail_app_password": "sentinel-gmail-password",
    "xing_mailbox_username": "sentinel.xing@example.com",
    "xing_mailbox_app_password": "sentinel-xing-password",
    "bundesagentur_api_key": "sentinel-ba-key",
    "automation_scheduler_account_key": "sentinel-account@example.com",
}
DATABASE_URL = "postgresql+psycopg://sentinel_user:sentinel_db_password@sentinel-host:5432/jobs"
PROXY = "203.0.113.77"


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        trusted_proxy_ips=PROXY,
        **SENTINELS,
        **overrides,
    )


def test_fields_are_exactly_the_allowlist():
    fields = startup_safety_fields(_settings())
    assert set(fields) == set(SAFETY_FLAGS) | {"database_dialect", "trusted_proxy_count"}
    assert fields["database_dialect"] == "postgresql"
    assert fields["trusted_proxy_count"] == "1"
    assert all(fields[flag] in {"true", "false"} for flag in SAFETY_FLAGS)


def test_defaults_report_every_safety_switch_off():
    fields = startup_safety_fields(Settings(_env_file=None))
    assert {fields[flag] for flag in SAFETY_FLAGS} == {"false"}


def test_values_reflect_the_effective_settings():
    fields = startup_safety_fields(_settings(outbound_sending_enabled=True))
    assert fields["outbound_sending_enabled"] == "true"
    assert fields["telegram_gmail_draft_enabled"] == "false"


def test_log_line_never_contains_secrets_or_identifiers(caplog):
    caplog.set_level(logging.INFO, logger="app.core.startup_diagnostics")
    log_startup_safety_state(_settings(), process="web")
    (record,) = caplog.records
    message = record.getMessage()
    assert message.startswith("startup_safety_state process=web ")
    for leaked in [*SENTINELS.values(), DATABASE_URL, "sentinel_db_password", "sentinel-host"]:
        assert leaked not in message
    assert PROXY not in message
    assert "sentinel" not in message
