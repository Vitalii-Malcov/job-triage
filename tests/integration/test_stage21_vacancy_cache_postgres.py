"""Stage 21: real-PostgreSQL proof of the vacancy assessment cache.

- the migration's table and CHECK constraints exist; a one-step downgrade
  drops the table and the upgrade restores it;
- two concurrent writers upserting the same vacancy leave exactly one
  consistent row (`INSERT ... ON CONFLICT DO UPDATE`), and a writer's
  compare-and-swap keeps a row another writer committed meanwhile;
- the writer's REPEATABLE READ batch never stores a key and an assessment
  from different states (A -> B -> A by a second connection), a concurrent
  cache write or vacancy delete becomes a retried conflict;
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
from scripts import stage21_assess_vacancies as writer_cli

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


def test_compare_and_swap_keeps_the_newer_row_of_a_concurrent_writer(engine):
    """Writer A reads the cache row and assesses; writer B stores a row and
    commits; A's compare-and-swap must leave B's row in place (Codex Stage 21
    audit)."""
    (job_id,) = _jobs(engine, 1)
    factory = sessionmaker(engine)
    with factory() as a, factory() as b:
        a.begin()
        observed = fit_cache.cached_keys(a, [job_id]).get(job_id)
        inputs = fit_cache.global_inputs(a)
        assessment = assess_job(a, job_id)
        with b.begin():
            assert fit_cache.upsert_assessment(
                b,
                job_id=job_id,
                key="b" * 64,
                inputs=inputs,
                assessment=assessment,
                expected_key=None,
            )
        written = fit_cache.upsert_assessment(
            a,
            job_id=job_id,
            key="a" * 64,
            inputs=inputs,
            assessment=assessment,
            expected_key=observed,
        )
        a.commit()
    assert observed is None and written is False
    with engine.connect() as conn:
        stored = conn.execute(
            select(JobFitAssessmentRecord.input_key).where(JobFitAssessmentRecord.job_id == job_id)
        ).scalar_one()
    assert stored == "b" * 64


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


# --- consistent reads (Codex audit of 76e8dc1, P2-1) --------------------------


def _set_description(engine, job_id, description) -> None:
    with engine.begin() as conn:
        conn.execute(
            JobRecord.__table__.update()
            .where(JobRecord.id == job_id)
            .values(description=description)
        )


def _stored(engine, job_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                select(JobFitAssessmentRecord.__table__).where(
                    JobFitAssessmentRecord.job_id == job_id
                )
            )
            .mappings()
            .one_or_none()
        )


def _assert_no_false_fresh_row(engine, job_ids) -> None:
    factory = sessionmaker(engine)
    with factory() as session:
        current = fit_cache.current_keys(session, job_ids)
        for job_id in job_ids:
            row = _stored(engine, job_id)
            if row is None or current.get(job_id) != row["input_key"]:
                continue
            live = fit_cache.assessment_values(assess_job(session, job_id))
            assert {k: row[k] for k in live} == live, f"false FRESH row {job_id}"


def test_a_b_a_change_by_a_second_connection_never_leaves_a_false_fresh_row(engine, monkeypatch):
    """The batch transaction reads the key from state A; a second connection
    commits B before `assess_job` reads the vacancy, and later A again."""
    (job_id,) = _jobs(engine, 1)
    state_a = "Python, FastAPI, SQL. Deutsch B1."
    state_b = "Senior Java Architekt mit mindestens 8 Jahren Berufserfahrung. Deutsch C1."
    real = writer_cli.assess_job
    committed: list[bool] = []

    def assess_after_a_concurrent_change(session, target):
        if not committed:
            _set_description(engine, job_id, state_b)  # second connection, committed
            committed.append(True)
        return real(session, target)

    monkeypatch.setattr(writer_cli, "assess_job", assess_after_a_concurrent_change)
    writer = writer_cli._writer_engine(engine)
    factory = sessionmaker(writer)
    with factory() as session, session.begin():
        assert writer_cli.assess_batch(session, [job_id]) == 1
    assert committed == [True]
    _assert_no_false_fresh_row(engine, [job_id])  # state B: the row reads STALE
    with sessionmaker(engine)() as session:
        b_values = fit_cache.assessment_values(real(session, job_id))
    row = _stored(engine, job_id)
    assert {k: row[k] for k in b_values} != b_values  # the A assessment, not B's
    _set_description(engine, job_id, state_a)  # A -> B -> A
    _assert_no_false_fresh_row(engine, [job_id])
    with sessionmaker(engine)() as session:
        assert fit_cache.current_keys(session, [job_id])[job_id] == row["input_key"]  # FRESH


def test_writer_requires_repeatable_read(engine):
    (job_id,) = _jobs(engine, 1)
    with sessionmaker(engine)() as session, session.begin():
        with pytest.raises(fit_cache.InconsistentReadError):
            writer_cli.assess_batch(session, [job_id])


def _hook_once(monkeypatch, action):
    real = writer_cli.assess_job
    calls: list[int] = []

    def assess(session, target):
        if not calls:
            action()
        calls.append(target)
        return real(session, target)

    monkeypatch.setattr(writer_cli, "assess_job", assess)
    return calls


def test_a_cache_row_committed_during_the_batch_is_a_conflict_and_the_retry_wins(
    engine, monkeypatch
):
    """REPEATABLE READ turns the concurrent write into a serialization
    failure; the retry re-reads everything on a fresh snapshot."""
    job_ids = _jobs(engine, 2)

    def other_writer():
        with sessionmaker(engine)() as other, other.begin():
            fit_cache.upsert_assessment(
                other,
                job_id=job_ids[0],
                key="b" * 64,
                inputs=fit_cache.global_inputs(other),
                assessment=assess_job(other, job_ids[0]),
            )

    calls = _hook_once(monkeypatch, other_writer)
    factory = sessionmaker(writer_cli._writer_engine(engine))
    assert writer_cli._write_batch(factory, job_ids) == 2
    assert len(calls) > len(job_ids)  # the first attempt was rolled back and retried
    with sessionmaker(engine)() as session:
        current = fit_cache.current_keys(session, job_ids)
    assert all(_stored(engine, j)["input_key"] == current[j] for j in job_ids)
    _assert_no_false_fresh_row(engine, job_ids)


def test_a_vacancy_deleted_during_the_batch_is_skipped_after_the_retry(engine, monkeypatch):
    job_ids = _jobs(engine, 2)

    def delete_one():
        with engine.begin() as conn:
            conn.execute(delete(JobRecord).where(JobRecord.id == job_ids[1]))

    _hook_once(monkeypatch, delete_one)
    factory = sessionmaker(writer_cli._writer_engine(engine))
    assert writer_cli._write_batch(factory, job_ids) == 1
    assert _stored(engine, job_ids[1]) is None
    _assert_no_false_fresh_row(engine, job_ids)
    # a re-run on the remaining vacancy is a no-op rewrite with the same key
    key = _stored(engine, job_ids[0])["input_key"]
    monkeypatch.setattr(writer_cli, "assess_job", assess_job)
    assert writer_cli._write_batch(factory, [job_ids[0]]) == 1
    assert _stored(engine, job_ids[0])["input_key"] == key


def test_a_conflict_on_every_attempt_is_reported_not_written(engine, monkeypatch):
    (job_id,) = _jobs(engine, 1)
    real = writer_cli.assess_job

    def always_conflict(session, target):
        with sessionmaker(engine)() as other, other.begin():
            fit_cache.upsert_assessment(
                other,
                job_id=job_id,
                key=os.urandom(32).hex(),
                inputs=fit_cache.global_inputs(other),
                assessment=assess_job(other, job_id),
            )
        return real(session, target)

    monkeypatch.setattr(writer_cli, "assess_job", always_conflict)
    factory = sessionmaker(writer_cli._writer_engine(engine))
    assert writer_cli._write_batch(factory, [job_id]) is None
