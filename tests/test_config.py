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


# ---------------------------------------------------------------------------
# S8B-PRE-002: automation_scheduler_account_key canonicalization (whitespace
# normalization, same as gmail_username) + DB length parity
# (automation_schedules.account_key / automation_runs.account_key are both
# String(320) == MAX_ADDRESS_LENGTH).
# ---------------------------------------------------------------------------


def test_automation_scheduler_account_key_is_stripped_of_surrounding_whitespace():
    """S8B-PRE-002: " me@example.com " must normalize to the exact same
    identity as "me@example.com" -- otherwise the two would silently claim
    two different schedule/AutomationRun account namespaces."""
    settings = Settings(
        automation_scheduler_enabled=True,
        automation_scheduler_account_key="  me@example.com  ",
    )
    assert settings.automation_scheduler_account_key == "me@example.com"


def test_automation_scheduler_account_key_whitespace_only_is_blank_while_disabled():
    settings = Settings(automation_scheduler_enabled=False, automation_scheduler_account_key="   ")
    assert settings.automation_scheduler_account_key == ""


def test_automation_scheduler_account_key_whitespace_only_fails_closed_while_enabled():
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_enabled=True, automation_scheduler_account_key="   ")


def test_automation_scheduler_account_key_accepts_max_length_320():
    settings = Settings(
        automation_scheduler_enabled=True, automation_scheduler_account_key="a" * 320
    )
    assert len(settings.automation_scheduler_account_key) == 320


def test_automation_scheduler_account_key_rejects_length_321():
    with pytest.raises(ValidationError):
        Settings(automation_scheduler_account_key="a" * 321)


def test_automation_scheduler_account_key_overlong_value_never_echoed_in_error():
    """S8B-PRE-002: the rejected value itself must never appear in the
    validator's own error message -- account_key identity strings are not
    exempt from this project's "don't echo untrusted/sensitive input into
    error text" convention just because they aren't literal credentials.
    """
    overlong = "s" * 321
    with pytest.raises(ValidationError) as exc_info:
        Settings(automation_scheduler_account_key=overlong)
    assert overlong not in str(exc_info.value)


# ---------------------------------------------------------------------------
# HARD-006 (adversarial hardening r1): rate_limit_requests/window_seconds
# and xing_lookback_days previously accepted 0/negative values that passed
# Settings() construction with no error and only misbehaved later, at
# request time (see docs/ADVERSARIAL_HARDENING_REPORT.md HARD-006).
# ---------------------------------------------------------------------------


def test_rate_limit_requests_zero_rejected():
    """A caller-visible symptom of the pre-fix bug: rate_limit_requests=0
    made _RateLimiter.check's `len(bucket) >= max_requests` always true,
    so every request -- including the first ever made -- was 429'd."""
    with pytest.raises(ValidationError):
        Settings(rate_limit_requests=0)


def test_rate_limit_requests_negative_rejected():
    with pytest.raises(ValidationError):
        Settings(rate_limit_requests=-1)


def test_rate_limit_window_seconds_zero_rejected():
    with pytest.raises(ValidationError):
        Settings(rate_limit_window_seconds=0)


def test_rate_limit_window_seconds_negative_rejected():
    """A caller-visible symptom of the pre-fix bug: a negative window made
    `cutoff = now - window_seconds` a FUTURE timestamp, so every bucket
    entry always looked expired and the limiter silently never limited
    anything -- a security-relevant silent fail-open, not just a crash."""
    with pytest.raises(ValidationError):
        Settings(rate_limit_window_seconds=-1)


def test_rate_limit_requests_and_window_positive_values_still_accepted():
    settings = Settings(rate_limit_requests=1, rate_limit_window_seconds=1)
    assert settings.rate_limit_requests == 1
    assert settings.rate_limit_window_seconds == 1


def test_xing_lookback_days_zero_rejected():
    with pytest.raises(ValidationError):
        Settings(xing_lookback_days=0)


def test_xing_lookback_days_negative_rejected():
    """A caller-visible symptom of the pre-fix bug: a negative value
    pushed the IMAP `since_date` filter into the future, so the collector
    silently returned zero messages every run with no error anywhere."""
    with pytest.raises(ValidationError):
        Settings(xing_lookback_days=-1)


def test_xing_lookback_days_matches_gmail_lookback_days_bounds():
    """Consistency check: the two sibling per-mailbox lookback-window
    settings must accept/reject the same range now that both are bounded."""
    settings = Settings(xing_lookback_days=1095)
    assert settings.xing_lookback_days == 1095
    with pytest.raises(ValidationError):
        Settings(xing_lookback_days=1096)


# ---------------------------------------------------------------------------
# DEPLOY-001 (Codex master review): `compose.yaml` previously interpolated
# POSTGRES_PASSWORD directly into a `user:${PASSWORD}@host` DATABASE_URL
# string -- any URI-reserved character in the password (`@ / : # %`) then
# corrupted the surrounding URL's own delimiter structure. Settings now
# builds the URL from discrete postgres_* parts via
# `sqlalchemy.engine.URL.create`, which percent-encodes each part
# correctly regardless of content.
# ---------------------------------------------------------------------------


def test_database_url_default_is_unaffected_when_postgres_host_unset():
    # `_env_file=None` deliberately bypasses `Settings.model_config`'s
    # `env_file=".env"` for THIS instantiation only -- this test proves
    # the field's actual Python-level default, which must hold
    # independently of whatever `.env` a developer happens to have
    # sitting in the repo root locally (e.g. a Stage 10 shadow-mode
    # pilot's DATABASE_URL=sqlite:///./stage10_pilot.db). Every other
    # `Settings()` call in this file is unaffected -- this is scoped to
    # the one test that asserts an exact default value that `.env`
    # presence can otherwise silently override.
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./job_search.db"
    assert settings.postgres_host == ""


def test_explicit_database_url_is_unaffected_when_postgres_host_unset():
    """An operator supplying DATABASE_URL directly (no postgres_* parts)
    must see it passed through completely unmodified -- this is purely
    additive, not a replacement for that existing configuration path."""
    settings = Settings(database_url="postgresql+psycopg://user:pass@localhost:5432/db")
    assert settings.database_url == "postgresql+psycopg://user:pass@localhost:5432/db"


@pytest.mark.parametrize(
    "password",
    [
        "p@ss:word/with#hash%percent",
        "@@@",
        "a/b/c",
        "col:on",
        "hash#tag",
        "percent%20literal",
        "back\\slash",
        "space in password",
        "quote'and\"quote",
    ],
)
def test_postgres_password_with_reserved_characters_round_trips(password):
    """The exact adversarial case DEPLOY-001 covers: every URI-reserved
    character SQLAlchemy's URL parser treats specially must still produce
    a DATABASE_URL that parses back to the EXACT original password/host/
    user/db -- never a silently-wrong split.
    """
    settings = Settings(
        postgres_host="db",
        postgres_port=5432,
        postgres_user="jobtriage",
        postgres_password=password,
        postgres_db="jobtriage",
    )
    from sqlalchemy.engine import make_url

    parsed = make_url(settings.database_url)
    assert parsed.password == password
    assert parsed.username == "jobtriage"
    assert parsed.host == "db"
    assert parsed.port == 5432
    assert parsed.database == "jobtriage"
    assert parsed.drivername == "postgresql+psycopg"


def test_postgres_host_set_without_password_fails_closed():
    """Required, testable configuration contract: partially-configured
    postgres_* parts must fail Settings() construction with a clear error
    rather than building a broken URL or silently falling back to SQLite.
    """
    with pytest.raises(ValidationError):
        Settings(postgres_host="db", postgres_user="jobtriage", postgres_db="jobtriage")


def test_postgres_host_set_without_user_fails_closed():
    with pytest.raises(ValidationError):
        Settings(postgres_host="db", postgres_password="secret", postgres_db="jobtriage")


def test_postgres_host_set_without_db_fails_closed():
    with pytest.raises(ValidationError):
        Settings(postgres_host="db", postgres_user="jobtriage", postgres_password="secret")


def test_postgres_password_never_appears_in_default_string_repr():
    """`str(URL)`/the settings object's own repr must never leak the raw
    password even if something incidentally logs/prints it -- only the
    dedicated `database_url` attribute (never logged, see
    app/core/config.py's docstring) carries the real value."""
    settings = Settings(
        postgres_host="db",
        postgres_user="jobtriage",
        postgres_password="p@ssw0rd/secret",
        postgres_db="jobtriage",
    )
    from sqlalchemy.engine import make_url

    assert "p@ssw0rd/secret" not in str(make_url(settings.database_url))


def test_postgres_port_default_and_bounds():
    assert Settings().postgres_port == 5432
    with pytest.raises(ValidationError):
        Settings(postgres_port=0)
    with pytest.raises(ValidationError):
        Settings(postgres_port=70_000)


# ---------------------------------------------------------------------------
# DEPLOY-001-RR1 (Codex targeted re-review): Codex independently reproduced
# the raw postgres_password AND the credential-bearing database_url appearing
# in repr(Settings(...))/str(Settings(...)), and the raw password appearing
# in ValidationError text for a partially-invalid construction. postgres_password
# is now a SecretStr and database_url is Field(repr=False) -- these tests
# assert the leak is actually closed, not just that the round-trip/connection
# behavior above still works.
# ---------------------------------------------------------------------------

_SENTINEL_PASSWORD = "sentinel-p@ss:w/rd#100%x-leak-check"


def _settings_with_sentinel_password(**overrides) -> Settings:
    kwargs = {
        "postgres_host": "db",
        "postgres_port": 5432,
        "postgres_user": "jobtriage",
        "postgres_password": _SENTINEL_PASSWORD,
        "postgres_db": "jobtriage",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_postgres_password_is_a_secret_str():
    settings = _settings_with_sentinel_password()
    assert type(settings.postgres_password).__name__ == "SecretStr"
    assert settings.postgres_password.get_secret_value() == _SENTINEL_PASSWORD


def test_raw_password_absent_from_settings_repr():
    settings = _settings_with_sentinel_password()
    assert _SENTINEL_PASSWORD not in repr(settings)


def test_raw_password_absent_from_settings_str():
    settings = _settings_with_sentinel_password()
    assert _SENTINEL_PASSWORD not in str(settings)


def test_database_url_itself_absent_from_settings_repr_and_str():
    """`database_url` is credential-bearing (built from the real
    password) — it must not appear in repr/str at all, not merely with
    the password masked inside it."""
    settings = _settings_with_sentinel_password()
    assert settings.database_url not in repr(settings)
    assert settings.database_url not in str(settings)
    assert "database_url" not in repr(settings)


def test_raw_password_absent_from_validation_error_text():
    """A partially-invalid Settings() construction (a real
    postgres_password alongside an unrelated invalid field) must not
    echo the supplied password into the ValidationError text —
    `hide_input_in_errors=True` suppresses per-field input echoing
    entirely."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            postgres_host="db",
            postgres_user="jobtriage",
            postgres_password=_SENTINEL_PASSWORD,
            postgres_db="jobtriage",
            gmail_lookback_days=-1,
        )
    assert _SENTINEL_PASSWORD not in str(exc_info.value)


def test_raw_password_absent_from_validation_error_when_postgres_parts_incomplete():
    """Same leak, different trigger: postgres_password supplied but a
    SIBLING required part (postgres_db) missing -- the model_validator's
    own raised ValueError must not have caused the password to be echoed
    by Pydantic's error rendering either."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            postgres_host="db",
            postgres_user="jobtriage",
            postgres_password=_SENTINEL_PASSWORD,
        )
    assert _SENTINEL_PASSWORD not in str(exc_info.value)


def test_database_url_construction_and_round_trip_unaffected_by_secret_str():
    """Runtime connection behavior must remain unchanged: the real
    secret value is still used at the URL-construction boundary (via
    get_secret_value()), so the reserved-character round-trip still
    works exactly as before this field became a SecretStr."""
    settings = _settings_with_sentinel_password(postgres_password="p@ss:w/rd#100%x")
    from sqlalchemy.engine import make_url

    parsed = make_url(settings.database_url)
    assert parsed.password == "p@ss:w/rd#100%x"
    assert parsed.username == "jobtriage"
    assert parsed.host == "db"
    assert parsed.database == "jobtriage"


def test_explicit_database_url_compatibility_unaffected_by_repr_false():
    """DEPLOY-001-RR1's Field(repr=False) on database_url must not change
    its actual value/behavior for the existing explicit-DATABASE_URL
    compatibility path -- only what repr()/str() print."""
    settings = Settings(database_url="postgresql+psycopg://user:pass@localhost:5432/db")
    assert settings.database_url == "postgresql+psycopg://user:pass@localhost:5432/db"


# ---------------------------------------------------------------------------
# ASTRA-01: Gmail/XING app passwords printed verbatim in
# repr(Settings(...))/str(Settings(...)) -- same class of leak as
# DEPLOY-001-RR1's postgres_password/database_url findings above, but for
# the IMAP/SMTP credentials. Fixed with Field(repr=False) (not SecretStr,
# unlike postgres_password): these two fields are passed as plain `str` to
# imaplib/smtplib login calls throughout app/providers/email/ and
# app/collectors/xing_email.py, so wrapping them would require unwrapping
# at every call site for no additional leak-closing benefit -- repr=False
# alone already stops both repr()/str() printing (this section) and
# ValidationError text (hide_input_in_errors, already asserted generically
# above) from ever echoing them.
# ---------------------------------------------------------------------------

_SENTINEL_GMAIL_PASSWORD = "sentinel-gmail-app-pw-leak-check"
_SENTINEL_XING_PASSWORD = "sentinel-xing-app-pw-leak-check"


def _settings_with_sentinel_mail_passwords(**overrides) -> Settings:
    kwargs = {
        "gmail_username": "me@example.com",
        "gmail_app_password": _SENTINEL_GMAIL_PASSWORD,
        "xing_mailbox_username": "me2@example.com",
        "xing_mailbox_app_password": _SENTINEL_XING_PASSWORD,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_gmail_app_password_still_a_plain_str_with_correct_value():
    """repr=False must not change the field's type or value -- only
    what repr()/str() print (mirrors GMAIL/SMTP login call sites, which
    require a plain str, not a wrapper type)."""
    settings = _settings_with_sentinel_mail_passwords()
    assert type(settings.gmail_app_password) is str
    assert settings.gmail_app_password == _SENTINEL_GMAIL_PASSWORD


def test_xing_mailbox_app_password_still_a_plain_str_with_correct_value():
    settings = _settings_with_sentinel_mail_passwords()
    assert type(settings.xing_mailbox_app_password) is str
    assert settings.xing_mailbox_app_password == _SENTINEL_XING_PASSWORD


def test_gmail_app_password_absent_from_settings_repr_and_str():
    settings = _settings_with_sentinel_mail_passwords()
    assert _SENTINEL_GMAIL_PASSWORD not in repr(settings)
    assert _SENTINEL_GMAIL_PASSWORD not in str(settings)


def test_xing_mailbox_app_password_absent_from_settings_repr_and_str():
    settings = _settings_with_sentinel_mail_passwords()
    assert _SENTINEL_XING_PASSWORD not in repr(settings)
    assert _SENTINEL_XING_PASSWORD not in str(settings)


def test_mail_passwords_absent_from_validation_error_text():
    """Same leak, ValidationError-rendering trigger: real gmail/xing app
    passwords supplied alongside an unrelated invalid field must not be
    echoed into the ValidationError text."""
    with pytest.raises(ValidationError) as exc_info:
        _settings_with_sentinel_mail_passwords(gmail_lookback_days=-1)
    assert _SENTINEL_GMAIL_PASSWORD not in str(exc_info.value)
    assert _SENTINEL_XING_PASSWORD not in str(exc_info.value)
