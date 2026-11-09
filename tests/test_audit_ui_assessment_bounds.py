"""UI-01: viewing pages never recomputes the whole catalogue, never writes and never runs
assessment work on the event loop."""

import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, JobRecord
from app.services import fit_cache
from app.services.job_fit_assessment import assess_job
from app.services.readiness import expected_alembic_heads
from app.ui import queries, routes
from app.ui.readonly import build_readonly_engine, readonly_sessionmaker
from scripts import stage21_assess_vacancies as writer_cli

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
JOBS = 30
PAGE = 5


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "ui_bounds.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    with sessionmaker(engine)() as db:
        for n in range(JOBS):
            db.add(
                JobRecord(
                    fingerprint=f"fp-{n}",
                    source="bundesagentur",
                    title=f"Python Entwickler {n} (m/w/d)",
                    company=f"Firma {n}",
                    location="Frankfurt am Main",
                    url=f"https://jobs.example.invalid/{n}",
                    description="Junior Python Backend, FastAPI, SQL. Deutsch B1.",
                    score=60,
                    recommendation="MAYBE",
                    first_seen_at=NOW - timedelta(days=1),
                    last_seen_at=NOW,
                )
            )
        db.commit()
    engine.dispose()
    return path


@pytest.fixture()
def reader(db_path):
    engine = build_readonly_engine(f"sqlite:///{db_path.as_posix()}")
    db = readonly_sessionmaker(engine)()
    yield db
    db.close()
    engine.dispose()


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Counter:
    def __init__(self):
        self.calls = 0

    def __call__(self, db, job_id):
        self.calls += 1
        return assess_job(db, job_id)


SETTINGS = SimpleNamespace(gmail_username="")
ZONE = ZoneInfo("Europe/Berlin")


def test_plain_listing_assesses_at_most_one_page(reader, db_path):
    before = _sha(db_path)
    counter = _Counter()
    page = queries.list_vacancies(reader, queries.VacancyFilters(), assess=counter, page_size=PAGE)
    assert len(page.rows) == PAGE
    assert counter.calls == PAGE
    assert _sha(db_path) == before


def test_uncached_computed_filter_is_bounded_by_the_scan_budget(reader, db_path):
    before = _sha(db_path)
    counter = _Counter()
    queries.list_vacancies(
        reader,
        queries.VacancyFilters(fit_tier="NOT_SUITABLE"),
        assess=counter,
        page_size=PAGE,
        scan_budget=8,
    )
    assert counter.calls <= 8
    assert _sha(db_path) == before


def test_uncached_dashboard_over_budget_assesses_nothing(reader, db_path):
    before = _sha(db_path)
    counter = _Counter()
    dashboard = queries.dashboard_summary(
        reader, SETTINGS, ZONE, now=NOW, assess=counter, scan_budget=3
    )
    assert counter.calls == 0
    assert dashboard.tiers["GOOD_MATCH"].state == "UNKNOWN"
    assert _sha(db_path) == before


def test_cached_dashboard_reads_the_cache_without_assessing(reader, db_path):
    assert writer_cli.run(["--database", str(db_path), "--apply"]) == 0
    reader.expire_all()
    before = _sha(db_path)
    counter = _Counter()
    dashboard = queries.dashboard_summary(reader, SETTINGS, ZONE, now=NOW, assess=counter)
    assert counter.calls == 0
    assert dashboard.cache is not None and dashboard.cache.fresh == JOBS
    assert _sha(db_path) == before


def test_cached_listing_assesses_only_the_returned_page(reader, db_path):
    assert writer_cli.run(["--database", str(db_path), "--apply"]) == 0
    assert fit_cache.cache_in_use(reader)
    before = _sha(db_path)
    counter = _Counter()
    tier = assess_job(reader, 1).fit_tier
    page = queries.list_vacancies(
        reader, queries.VacancyFilters(fit_tier=tier), assess=counter, page_size=PAGE
    )
    assert len(page.rows) == PAGE
    assert counter.calls == PAGE
    assert _sha(db_path) == before


def test_ui_handlers_that_read_the_database_are_not_coroutines():
    """A coroutine handler would run assess_job's database work on the event loop; plain `def`
    handlers run in the worker pool. Only the login handler (async body reading) may be async."""
    endpoints = [
        getattr(route, "endpoint", None) for route in (*routes.router.routes, *routes.pages.routes)
    ]
    endpoints = [e for e in endpoints if e is not None]
    assert any(e.__name__ == "dashboard" for e in endpoints)
    offenders = [
        e.__name__
        for e in endpoints
        if inspect.iscoroutinefunction(e) and "queries" in inspect.getsource(e)
    ]
    assert offenders == []
