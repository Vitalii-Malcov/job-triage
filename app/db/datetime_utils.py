"""Shared datetime-normalization helper for the persistence layer.

Consolidates a fix that had been independently duplicated (with each copy's
docstring explicitly cross-referencing the others as "mirrors X exactly")
across `app.db.automation_repository`, `app.db.automation_schedule_repository`,
`app.db.follow_up_repository`, and (inline) `app.services.company_research
.CompanyResearchService._is_fresh` -- one shared implementation instead of
four independently-maintained copies of the same reasoning.
"""

from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
    """SQLite (unlike Postgres) doesn't preserve tzinfo through a
    `DateTime(timezone=True)` round-trip -- a value stored as UTC comes
    back naive, which would otherwise raise `TypeError: can't compare
    offset-naive and offset-aware datetimes` when compared against a
    tz-aware `now` in pure Python (as opposed to inside a SQL WHERE
    clause, which compares as text and is unaffected). Every timestamp
    column this project writes is UTC, so a naive read is always safe to
    reattach as UTC rather than a sign of a genuinely ambiguous value.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
