from functools import lru_cache

from pydantic import Field
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

    # Company Research Agent (POST/GET /jobs/{id}/research, Telegram
    # /research <id>). v1 makes zero outbound network requests — its only
    # provider (JobDataCompanyResearchProvider) builds research purely from
    # a job's own already-persisted data, so there is no API key or
    # HTTP-fetch configuration to expose here (a website-fetch sub-feature
    # existed briefly but was removed after a Codex review found its
    # DNS-resolve-then-fetch SSRF check has an unavoidable DNS-rebinding
    # window — see app/providers/job_data_provider.py). auto_enabled gates
    # firing research automatically from a collector run for high-scoring
    # jobs; off by default to control cost until explicitly opted into.
    # auto_max_per_run bounds how many such automatic runs one collector
    # run can trigger, regardless of how many APPLY jobs it produces.
    company_research_ttl_hours: int = Field(default=720, ge=0)
    company_research_auto_enabled: bool = False
    company_research_auto_max_per_run: int = Field(default=20, ge=0)

    # Gmail inbox foundation (Stage 7A — POST /gmail/sync). Deliberately its
    # own, non-XING-prefixed config block: this reads the user's actual
    # response/reply inbox (not a job-digest-only mailbox), and must not be
    # coupled to XING_MAILBOX_* — see app/collectors/xing_email.py and
    # app/providers/email/ for why the two are kept as fully independent
    # credential sets and code paths. No default on username/app_password on
    # purpose: unset means the sync endpoint fails closed (503) instead of
    # attempting an IMAP login with empty credentials.
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = Field(default=993, ge=1, le=65535)
    gmail_username: str = ""
    gmail_app_password: str = ""
    gmail_mailbox: str = "INBOX"
    # Bounded so a misconfigured value can't turn a sync into an
    # effectively-unbounded full-mailbox-history fetch (upper bound ~3
    # years) or a no-op (must fetch at least 1 day back).
    gmail_lookback_days: int = Field(default=30, ge=1, le=1095)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
