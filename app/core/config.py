from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.providers.email.base import (
    MAX_ADDRESS_LENGTH,
    MAX_IMAP_HOST_LENGTH,
    MAX_MAILBOX_NAME_LENGTH,
)


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
    # S7E-001 (Codex remediation, HIGH): the real Gmail "Sent Mail" folder —
    # synced in ADDITION to gmail_mailbox (never instead of it). Messages
    # fetched from THIS mailbox are the only ones ever trusted as
    # OUTBOUND — see app/providers/email/imap.py's `trusted_outbound`
    # parameter and its module docstring for why a message's own `From`
    # header (trivially spoofable by anyone who can send us mail) is no
    # longer used to decide direction. Gmail's default English label is
    # used as the default; a non-English/renamed mailbox must be
    # configured explicitly.
    gmail_sent_mailbox: str = "[Gmail]/Sent Mail"
    # Bounded so a misconfigured value can't turn a sync into an
    # effectively-unbounded full-mailbox-history fetch (upper bound ~3
    # years) or a no-op (must fetch at least 1 day back).
    gmail_lookback_days: int = Field(default=30, ge=1, le=1095)

    # Stage 7D outbound SMTP (POST /response-drafts/{id}/send). Reuses
    # gmail_username/gmail_app_password above — a Gmail App Password is
    # valid for both IMAP and SMTP against the same account, so no new
    # secret is introduced. See app/providers/email/smtp.py's module
    # docstring. No default host override needed beyond Gmail's standard
    # SMTPS endpoint; kept configurable only for tests/future flexibility.
    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = Field(default=465, ge=1, le=65535)

    # Stage 7E follow-up agent. How long to wait, after the latest real
    # OUTBOUND message in a job's matched Gmail thread, before a follow-up
    # becomes eligible — see app/services/follow_up_eligibility.py. Bounded
    # (1-90 days): 0 would mean "always immediately due" (never a
    # meaningful wait), and an unbounded value defeats the point of a
    # configurable delay entirely.
    follow_up_delay_days: int = Field(default=7, ge=1, le=90)

    # Stage 8B scheduler (standalone `python -m app.scheduler` worker, never
    # embedded in FastAPI's own process/lifespan -- see app/scheduler.py's
    # module docstring for why: multiple Uvicorn workers must not each run
    # their own independent timer loop). Disabled by default -- opt-in only.
    # account_key is identity only (which account's automation to run), never
    # a secret credential -- it is whatever app.services.automation's own
    # account_key parameter already accepts (mirrors GMAIL_USERNAME's role
    # elsewhere in this project).
    automation_scheduler_enabled: bool = False
    automation_scheduler_account_key: str = ""
    # Bounded 60s..7 days: below 60s risks hammering the DB/collectors far
    # faster than any real job source refreshes; above 7 days defeats the
    # point of a "periodic" scheduler.
    automation_scheduler_interval_seconds: int = Field(default=3600, ge=60, le=604_800)
    # How often the standalone worker polls persisted schedule state to check
    # whether a slot is due -- deliberately independent of
    # automation_scheduler_interval_seconds (the actual run cadence): a small
    # poll interval just keeps the worker responsive to a due slot without
    # busy-looping. 15s is a sensible production default -- frequent enough
    # that a due slot is claimed promptly, far below the interval floor above
    # so it never itself becomes the bottleneck.
    automation_scheduler_poll_seconds: int = Field(default=15, ge=1, le=3600)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GMAIL-009: length/blank invariants, consistent with the DB columns
    # and identity normalization these values ultimately feed
    # (app.db.models.GmailMessageRecord/GmailThreadRecord, and
    # app.providers.email.base.normalize_account_key). Each validator
    # strips before measuring/returning, so Settings and
    # normalize_account_key (which also strips, then casefolds) can never
    # disagree about what the "real" value is — Settings never applies
    # casefold itself, since gmail_username is also the literal IMAP LOGIN
    # credential, not just an identity key.
    @field_validator("gmail_username")
    @classmethod
    def _validate_gmail_username(cls, value: str) -> str:
        # Unlike host/mailbox below, blank remains a deliberate, meaningful
        # "not configured" state (fails closed at the collector — see
        # app/api/routes.py's _run_gmail_sync — not at Settings
        # construction), so an empty/whitespace-only value is returned as
        # "" rather than rejected.
        stripped = value.strip()
        if not stripped:
            return ""
        if len(stripped) > MAX_ADDRESS_LENGTH:
            raise ValueError(f"must not exceed {MAX_ADDRESS_LENGTH} characters")
        return stripped

    @field_validator("gmail_mailbox", "gmail_sent_mailbox")
    @classmethod
    def _validate_gmail_mailbox(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        if len(stripped) > MAX_MAILBOX_NAME_LENGTH:
            raise ValueError(f"must not exceed {MAX_MAILBOX_NAME_LENGTH} characters")
        return stripped

    @field_validator("gmail_imap_host")
    @classmethod
    def _validate_gmail_imap_host(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        if len(stripped) > MAX_IMAP_HOST_LENGTH:
            raise ValueError(f"must not exceed {MAX_IMAP_HOST_LENGTH} characters")
        return stripped

    # Stage 8B: fail closed at construction time rather than letting a
    # blank account_key reach the standalone worker and either crash
    # opaquely mid-poll-loop or (worse) silently poll nothing. Cross-field,
    # so a model_validator, not a field_validator -- deliberately checked
    # here (not only in app.services.scheduler.validate_scheduler_settings)
    # so ANY Settings() construction with this combination fails the same
    # way, not just the one the standalone worker happens to call.
    @model_validator(mode="after")
    def _validate_scheduler_requires_account_key_when_enabled(self) -> "Settings":
        if self.automation_scheduler_enabled and not self.automation_scheduler_account_key.strip():
            raise ValueError(
                "automation_scheduler_account_key must be set when "
                "automation_scheduler_enabled=True"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
