"""Stage 13A: one non-secret startup log line recording the effective
safety-relevant switches, as operational EVIDENCE (it enforces nothing).

Closed allowlist: only the boolean flags named in `SAFETY_FLAGS`, the
database dialect name (never the URL, host, user or password) and the
number of trusted proxies (never their addresses). The Settings object is
never logged or iterated, so adding a new (possibly secret) setting can
never make it appear here.
"""

import logging

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

SAFETY_FLAGS = (
    "outbound_sending_enabled",
    "telegram_gmail_draft_enabled",
    "telegram_bewerbung_draft_enabled",
    "telegram_bewerbung_approval_enabled",
    "telegram_vacancy_feed_enabled",
    "telegram_daily_digest_enabled",
    "telegram_operator_control_enabled",
    "alembic_auto_upgrade",
    "automation_scheduler_enabled",
    "automation_auto_prepare_enabled",
    "automation_gmail_cycle_enabled",
    "automation_follow_up_cycle_enabled",
    "company_research_auto_enabled",
)


def _database_dialect(database_url: str) -> str:
    try:
        return make_url(database_url).get_backend_name()
    except Exception:  # noqa: BLE001 -- evidence only; never echo the value
        return "unknown"


def startup_safety_fields(settings) -> dict[str, str]:
    fields = {name: str(bool(getattr(settings, name))).lower() for name in SAFETY_FLAGS}
    fields["database_dialect"] = _database_dialect(settings.database_url)
    fields["trusted_proxy_count"] = str(len(settings.trusted_proxy_ip_set))
    return fields


def log_startup_safety_state(settings, *, process: str) -> None:
    fields = startup_safety_fields(settings)
    logger.info(
        "startup_safety_state process=%s %s",
        process,
        " ".join(f"{key}={value}" for key, value in fields.items()),
    )
