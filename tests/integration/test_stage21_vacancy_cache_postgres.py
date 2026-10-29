"""Stage 21: real-PostgreSQL proof of the vacancy assessment cache.

- the migration's table and CHECK constraints exist; a one-step downgrade
  drops the table and the upgrade restores it;
- two concurrent writers upserting the same vacancy leave exactly one
  consistent row (`INSERT ... ON CONFLICT DO UPDATE`);
- the UI's read-only connection (`default_transaction_read_only`) reads the
  cache for the dashboard and the filtered list, and cannot write it.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first.
**CI:** part of the `scheduler-postgres` job's zero-skip gate.
"""

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, insert, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.db.models import JobFitAssessmentRecord, JobRecord
from app.services import fit_cache
from app.services.job_fit_assessment import assess_job
from app.ui import queries
from app.ui.readonly import build_readonly_engine, readonly_sessionmaker

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HEAD = "c7e2a9d4f1b3"
PREVIOUS = "b6c1e9a4d2f7"
PREFIX = "stage21-cache-pg-"
NOW = datetime.now(UTC)


@pytest.fixture()
def engine():
    engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    yield engine
    with engine.begin() as conn:  # rows of job_fit_assessments go with ON DELETE CASCADE
        conn.execute(delete(JobRecord).where(JobRecord.fingerprint.like(f"{PREFIX}%")))
    engine.dispose()


def _jobs(engine, count: int) -> list[int]:
    with engine.begin() as conn:
        return [
            conn.execute(
                insert(JobRecord)
                .values(
                    fingerprint=f"{PREFIX}{n}",
                    source="stage21-cache-pg",
                    title=f"Python Entwickler {n} (m/w/d)",
                    company=f"Beispiel GmbH {n}",
                    location="Frankfurt am Main",
                    url=f"https://jobs.example.invalid/{n}",
                    description="Python, FastAPI, SQL. Deutsch B1.",
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
                .returning(JobRecord.id)
            ).scalar_one()
            for n in range(1, count + 1)
        ]


def _fill(engine, job_ids) -> None:
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        inputs = fit_cache.global_inputs(session)
        keys = fit_cache.current_keys(session, job_ids, inputs)
        for job_id in job_ids:
            fit_cache.upsert_assessment(
                session,
                job_id=job_id,
                key=keys[job_id],
                inputs=inputs,
                assessment=assess_job(session, job_id),
            )


def test_table_and_check_constraints_exist(engine):
    inspector = inspect(engine)
    assert "job_fit_assessments" in inspector.get_table_names()
    checks = {c["name"] for c in inspector.get_check_constraints("job_fit_assessments")}
    assert {"ck_job_fit_assessments_fit_tier", "ck_job_fit_assessments_decision"} <= checks
    (job_id,) = _jobs(engine, 1)
    _fill(engine, [job_id])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("UPDATE job_fit_assessments SET fit_tier = 'BEST' WHERE job_id = :j"),
            {"j": job_id},
        )


def test_concurrent_writers_leave_one_consistent_row(engine):
    (job_id,) = _jobs(engine, 1)
    factory = sessionmaker(engine)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(key_char: str) -> None:
        try:
            with factory() as session:
                assessment = assess_job(session, job_id)
                inputs = fit_cache.global_inputs(session)
                # the reads above began a transaction; end it before the
                # explicit per-write transactions (Codex Stage 21 review P1)
                session.rollback()
                for _ in range(20):
                    barrier.wait(timeout=30)
                    with session.begin():
                        fit_cache.upsert_assessment(
                            session,
                            job_id=job_id,
                            key=key_char * 64,
                            inputs=inputs,
                            assessment=assessment,
                        )
        except BaseException as exc:  # noqa: BLE001 -- reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(c,)) for c in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == []
    with engine.connect() as conn:
        rows = conn.execute(
            select(JobFitAssessmentRecord.input_key).where(JobFitAssessmentRecord.job_id == job_id)
        ).all()
    assert len(rows) == 1 and rows[0][0] in {"a" * 64, "b" * 64}


def test_ui_reads_the_cache_on_a_read_only_connection_and_cannot_write_it(engine):
    job_ids = _jobs(engine, 3)
    _fill(engine, job_ids)
    ro_engine = build_readonly_engine(TEST_POSTGRES_URL)
    session = readonly_sessionmaker(ro_engine)()
    try:
        assert session.execute(text("SHOW default_transaction_read_only")).scalar_one() == "on"
        assert fit_cache.cache_in_use(session)
        summary = queries.dashboard_summary(
            session,
            SimpleNamespace(gmail_username=""),
            queries.operator_zone("Europe/Berlin"),
            now=NOW,
        )
        assert summary.cache is not None and summary.cache.fresh >= 3
        tier = session.scalar(
            select(JobFitAssessmentRecord.fit_tier).where(
                JobFitAssessmentRecord.job_id == job_ids[0]
            )
        )
        page = queries.list_vacancies(session, queries.VacancyFilters(fit_tier=tier))
        assert page.from_cache and job_ids[0] in {r.job.id for r in page.rows}
        with pytest.raises(DBAPIError):
            fit_cache.upsert_assessment(
                session,
                job_id=job_ids[0],
                key="c" * 64,
                inputs=fit_cache.global_inputs(session),
                assessment=assess_job(session, job_ids[0]),
            )
    finally:
        session.rollback()
        session.close()
        ro_engine.dispose()
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).where(JobFitAssessmentRecord.input_key == "c" * 64)
            ).scalar_one()
            == 0
        )


def test_one_step_downgrade_and_upgrade():
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_POSTGRES_URL.replace("%", "%%"))
    engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    try:
        downgrade(cfg, PREVIOUS)
        assert "job_fit_assessments" not in set(inspect(engine).get_table_names())
        upgrade(cfg, "head")
        assert "job_fit_assessments" in set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
            )
    finally:
        upgrade(cfg, "head")
        engine.dispose()
