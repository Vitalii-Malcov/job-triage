"""Stage 20: real-PostgreSQL proof of the UI's read-only session
(app/ui/readonly.py) -- the part SQLite cannot show:

- the libpq startup option makes every transaction on the UI connection
  read-only (`default_transaction_read_only = on`), also after a rollback
  and on a reused pooled connection;
- ORM and Core writes through the UI session fail; nothing is committed;
- the UI queries (LIKE escaping, keyset pagination, fit-tier counts,
  follow-ups) run inside such read-only transactions.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first.
**CI:** part of the `scheduler-postgres` job's zero-skip gate.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.models import JobRecord
from app.ui import queries
from app.ui.readonly import ReadOnlySessionError, build_readonly_engine, readonly_sessionmaker

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

PREFIX = "stage20-ui-pg-"
NOW = datetime.now(UTC)


def _row(n: int, **overrides) -> dict:
    values = dict(
        fingerprint=f"{PREFIX}{n}",
        source="stage20-ui-pg",
        title=f"Python Entwickler {n} (m/w/d)",
        company=f"Beispiel GmbH {n}",
        location="Frankfurt am Main",
        url=f"https://jobs.example.invalid/{n}",
        description="Python, FastAPI, SQL.",
        skills_json="[]",
        must_have_skills_json="[]",
        nice_to_have_skills_json="[]",
        data_confidence=0.0,
        score=60,
        recommendation="MAYBE",
        status="NEW",
        first_seen_at=NOW - timedelta(hours=n),
        last_seen_at=NOW,
    )
    values.update(overrides)
    return values


@pytest.fixture()
def seeded():
    writer = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    with writer.begin() as conn:
        conn.execute(delete(JobRecord).where(JobRecord.fingerprint.like(f"{PREFIX}%")))
        conn.execute(insert(JobRecord), [_row(n) for n in range(1, 31)])
        conn.execute(insert(JobRecord), [_row(99, title="100% Remote_Entwickler")])
    yield writer
    with writer.begin() as conn:
        conn.execute(delete(JobRecord).where(JobRecord.fingerprint.like(f"{PREFIX}%")))
    writer.dispose()


@pytest.fixture()
def reader():
    engine = build_readonly_engine(TEST_POSTGRES_URL)
    session = readonly_sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _count(writer) -> int:
    with writer.connect() as conn:
        return conn.scalar(
            select(func.count(JobRecord.id)).where(JobRecord.fingerprint.like(f"{PREFIX}%"))
        )


def test_every_ui_transaction_is_read_only(seeded, reader):
    assert reader.execute(text("SHOW default_transaction_read_only")).scalar_one() == "on"
    for attempt in range(2):  # the second attempt runs after a rollback
        with pytest.raises(DBAPIError, match="read-only transaction"):
            reader.execute(insert(JobRecord).values(**_row(500 + attempt)))
        reader.rollback()
    assert reader.execute(text("SHOW transaction_read_only")).scalar_one() == "on"

    reader.add(JobRecord(**_row(600)))
    with pytest.raises(ReadOnlySessionError):
        reader.flush()
    reader.rollback()
    assert _count(seeded) == 31


def test_pooled_connection_stays_read_only(seeded):
    engine = build_readonly_engine(TEST_POSTGRES_URL)
    factory = readonly_sessionmaker(engine)
    for _ in range(3):  # same pooled connection checked out again
        session = factory()
        assert session.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        session.rollback()
        session.close()
    engine.dispose()


def test_ui_queries_run_read_only_on_postgres(seeded, reader):
    filters = queries.VacancyFilters(source="stage20-ui-pg")
    first = queries.list_vacancies(reader, filters)
    assert len(first.rows) == queries.PAGE_SIZE
    rest = queries.list_vacancies(
        reader, queries.VacancyFilters(source="stage20-ui-pg", before_id=first.next_before_id)
    )
    ids = [r.job.id for r in first.rows + rest.rows]
    assert len(ids) == len(set(ids)) == 31 and rest.next_before_id is None

    # % and _ are literal in the search text (escaped LIKE on PostgreSQL)
    literal = queries.list_vacancies(
        reader, queries.VacancyFilters(source="stage20-ui-pg", q="100% Remote_")
    )
    assert [r.job.title for r in literal.rows] == ["100% Remote_Entwickler"]
    assert (
        queries.list_vacancies(
            reader, queries.VacancyFilters(source="stage20-ui-pg", q="0%_R")
        ).rows
        == []
    )

    summary = queries.dashboard_summary(
        reader,
        Settings(_env_file=None, gmail_username=""),
        queries.operator_zone("Europe/Berlin"),
    )
    assert summary.jobs_total >= 31
    assert summary.inbox_review.state == "NOT_CONFIGURED"
    queries.follow_ups(reader, queries.operator_zone("Europe/Berlin"))
    assert _count(seeded) == 31
