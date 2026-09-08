import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.providers.email.base import normalize_account_key


def test_default_settings_are_valid():
    Settings()


def test_negative_company_research_ttl_hours_rejected():
    with pytest.raises(ValidationError):
        Settings(company_research_ttl_hours=-1)


def test_negative_company_research_auto_max_per_run_rejected():
    with pytest.raises(ValidationError):
        Settings(company_research_auto_max_per_run=-1)


def test_zero_is_a_valid_boundary_for_company_research_settings():
    settings = Settings(company_research_ttl_hours=0, company_research_auto_max_per_run=0)
    assert settings.company_research_ttl_hours == 0
    assert settings.company_research_auto_max_per_run == 0


def test_gmail_lookback_days_rejects_zero_and_negative():
    with pytest.raises(ValidationError):
        Settings(gmail_lookback_days=0)
    with pytest.raises(ValidationError):
        Settings(gmail_lookback_days=-1)


def test_gmail_lookback_days_rejects_unreasonably_large_value():
    with pytest.raises(ValidationError):
        Settings(gmail_lookback_days=10_000)


def test_gmail_lookback_days_accepts_boundary_values():
    assert Settings(gmail_lookback_days=1).gmail_lookback_days == 1
    assert Settings(gmail_lookback_days=1095).gmail_lookback_days == 1095


def test_gmail_imap_port_rejects_out_of_range_values():
    with pytest.raises(ValidationError):
        Settings(gmail_imap_port=0)
    with pytest.raises(ValidationError):
        Settings(gmail_imap_port=70_000)


def test_gmail_imap_host_rejects_blank_and_whitespace_only():
    with pytest.raises(ValidationError):
        Settings(gmail_imap_host="")
    with pytest.raises(ValidationError):
        Settings(gmail_imap_host="   ")


def test_gmail_mailbox_rejects_blank_and_whitespace_only():
    with pytest.raises(ValidationError):
        Settings(gmail_mailbox="")
    with pytest.raises(ValidationError):
        Settings(gmail_mailbox="   ")


def test_gmail_username_and_app_password_blank_is_still_allowed():
    """Unlike host/mailbox, a blank username/app_password is a
    deliberate, meaningful "not configured" state (fails closed at the
    collector/provider, not at Settings construction) — see
    app/api/routes.py's _run_gmail_sync.
    """
    settings = Settings(gmail_username="", gmail_app_password="")
    assert settings.gmail_username == ""
    assert settings.gmail_app_password == ""


# ---------------------------------------------------------------------------
# GMAIL-009: length invariants, aligned with DB column widths
# (GmailMessageRecord.account_key/mailbox — String(320)/String(100)) and
# with app.providers.email.base.normalize_account_key.
# ---------------------------------------------------------------------------


def test_gmail_username_accepts_max_length_320():
    settings = Settings(gmail_username="a" * 320)
    assert len(settings.gmail_username) == 320


def test_gmail_username_rejects_length_321():
    with pytest.raises(ValidationError):
        Settings(gmail_username="a" * 321)


def test_gmail_username_is_stripped_but_not_casefolded():
    """Settings strips (matching normalize_account_key) but never
    casefolds gmail_username itself — it is also the literal IMAP LOGIN
    credential, not just an identity key. normalize_account_key applies
    its own casefold on top, so the two never disagree about what the
    "real" (trimmed) value is.
    """
    settings = Settings(gmail_username="  Someone@Example.com  ")
    assert settings.gmail_username == "Someone@Example.com"
    assert normalize_account_key(settings.gmail_username) == "someone@example.com"


def test_gmail_mailbox_accepts_max_length_100():
    settings = Settings(gmail_mailbox="A" * 100)
    assert len(settings.gmail_mailbox) == 100


def test_gmail_mailbox_rejects_length_101():
    with pytest.raises(ValidationError):
        Settings(gmail_mailbox="A" * 101)


def test_gmail_imap_host_accepts_max_length_253():
    settings = Settings(gmail_imap_host="a" * 253)
    assert len(settings.gmail_imap_host) == 253


def test_gmail_imap_host_rejects_length_254():
    with pytest.raises(ValidationError):
        Settings(gmail_imap_host="a" * 254)


def test_gmail_username_and_mailbox_are_stripped_of_surrounding_whitespace():
    settings = Settings(gmail_mailbox="  INBOX  ")
    assert settings.gmail_mailbox == "INBOX"


# ---------------------------------------------------------------------------
# Stage 8B: scheduler configuration -- disabled by default, fail-closed if
# enabled without an account_key, bounded interval/poll settings.
# ---------------------------------------------------------------------------


def test_automation_scheduler_disabled_by_default():
    settings = Settings()
    assert settings.automation_scheduler_enabled is False
    assert settings.automation_scheduler_account_key == ""


def test_automation_scheduler_enabled_with_blank_account_key_fails_closed():
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_enabled=True, automation_scheduler_account_key="")
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_enabled=True, automation_scheduler_account_key="   ")


def test_automation_scheduler_enabled_with_account_key_is_valid():
    settings = Settings(
        automation_scheduler_enabled=True, automation_scheduler_account_key="me@example.com"
    )
    assert settings.automation_scheduler_enabled is True
    assert settings.automation_scheduler_account_key == "me@example.com"


def test_automation_scheduler_disabled_with_blank_account_key_is_still_valid():
    """Disabled is the safe default -- a blank account_key must never be
    rejected just because the scheduler happens to be off."""
    settings = Settings(automation_scheduler_enabled=False, automation_scheduler_account_key="")
    assert settings.automation_scheduler_enabled is False


def test_automation_scheduler_interval_seconds_default_and_bounds():
    assert Settings().automation_scheduler_interval_seconds == 3600
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_interval_seconds=59)
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_interval_seconds=604_801)


def test_automation_scheduler_interval_seconds_accepts_boundary_values():
    lower = Settings(automation_scheduler_interval_seconds=60)
    assert lower.automation_scheduler_interval_seconds == 60
    upper = Settings(automation_scheduler_interval_seconds=604_800)
    assert upper.automation_scheduler_interval_seconds == 604_800


def test_automation_scheduler_poll_seconds_default_and_bounds():
    assert Settings().automation_scheduler_poll_seconds == 15
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_poll_seconds=0)
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_poll_seconds=3601)
