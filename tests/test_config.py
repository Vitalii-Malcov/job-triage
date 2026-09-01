import pytest
from pydantic import ValidationError

from app.core.config import Settings


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
