from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./job_search.db"
    # Opt-in only: when true, run `alembic upgrade head` programmatically on
    # startup. Intended for local dev/tests. Production must run migrations
    # explicitly (manually or in CI/CD) before starting the app.
    alembic_auto_upgrade: bool = False
    # No default on purpose: an empty/unset key means require_api_key()
    # rejects every request instead of accepting a predictable value.
    api_key: str = ""
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_timeout_seconds: float = 5.0
    telegram_max_retries: int = 3
    min_job_score_to_notify: int = 80

    # No default on purpose: an unset key means the collector endpoint fails
    # closed (503) instead of calling the upstream API with an empty key.
    bundesagentur_api_key: str = ""
    bundesagentur_search_keywords: str = ""
    bundesagentur_search_location: str = ""
    bundesagentur_search_radius_km: int = 25

    # XING email digest collector (POST /collectors/xing/run). No default on
    # username/app_password on purpose: unset means the endpoint fails closed
    # (503) instead of attempting an IMAP login with empty credentials. See
    # README "Collectors" -> "XING (email digest)" for how to generate an
    # App Password, and app/collectors/xing_email.py for the hard constraint
    # that tracking links inside these emails are never followed by code.
    xing_mailbox_imap_host: str = "imap.gmail.com"
    xing_mailbox_imap_port: int = 993
    xing_mailbox_username: str = ""
    xing_mailbox_app_password: str = ""
    xing_lookback_days: int = 7

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
