from abc import ABC, abstractmethod
from datetime import datetime

from app.models.job import Job


class CollectorError(Exception):
    """Base exception for job collector failures (auth, rate limit, network, API errors).

    Concrete collectors should subclass this so callers (e.g. API routes) can
    catch a single type regardless of which external source failed.
    """


class CollectorNotConfiguredError(CollectorError):
    """Raised by a shared collector-run helper when required config (API key,
    mailbox credentials, etc.) is missing.

    Kept distinct from other CollectorError subclasses so callers can present
    "not configured" differently from "the upstream call failed" — e.g. the
    HTTP API maps this to 503 vs. 502 (see app/api/routes.py), and the
    Telegram control center (app/services/telegram_bot.py) can show a
    configuration hint instead of a generic failure message.
    """


def is_configured(value: str) -> bool:
    """True if `value` is a real, usable config value rather than empty/whitespace-only.

    Shared across collectors (e.g. Bundesagentur's API key, XING's mailbox
    username/app password) so "is this thing configured" is defined once
    instead of duplicated as slightly different `if not value:` checks per
    source, which could drift out of sync (see the whitespace-only-key bug
    this pattern was introduced to fix).
    """
    return bool(value and value.strip())


class JobCollector(ABC):
    """Interface every job source collector implements.

    A collector's only responsibility is fetching postings from one external
    source and mapping them into `Job`. It must never write to the database
    and must never score jobs — persistence and scoring are the caller's
    responsibility (see app/api/routes.py).
    """

    #: Must equal the `source` value set on every `Job` this collector
    #: returns, so fingerprint-based deduplication in
    #: app.db.repositories.upsert_job stays stable across runs.
    source: str

    @abstractmethod
    async def fetch(self, since: datetime | None = None) -> list[Job]:
        """Fetch postings, optionally only those published on/after `since`."""
        raise NotImplementedError
