"""Stage 21: persistent vacancy assessment cache (docs/STAGE21_VACANCY_CACHE_PLAN.md)."""

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, select, text, update
from sqlalchemy.orm import sessionmaker

from app.core.database_target import DatabaseKind
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import (
    Base,
    CandidateJobMatchRecord,
    CandidateProfileRecord,
    JobCommuteCheckRecord,
    JobFitAssessmentRecord,
    JobRecord,
)
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services import fit_cache
from app.services.application_tracker import ApplicationTracker
from app.services.job_fit_assessment import assess_job
from app.services.readiness import expected_alembic_heads
from app.ui import queries
from app.ui.readonly import ReadOnlySessionError, build_readonly_engine, readonly_sessionmaker
from scripts import stage21_assess_vacancies as writer_cli

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
NO_MAIL = SimpleNamespace(gmail_username="")  # inbox NOT_CONFIGURED
DESCRIPTIONS = [
    "Junior Python Entwickler, FastAPI, SQL. Deutsch B1. 100% Remote in Deutschland.",
    "Senior Java Architekt mit mindestens 8 Jahren Berufserfahrung. Deutsch C1 zwingend.",
    "Python Backend, hybrides Arbeiten (2 Tage Homeoffice). Gehalt 50.000 EUR.",
    "Werkstudent Data Engineering, Python, Einstieg ab sofort.",
    "Softwareentwickler (m/w/d) vor Ort in Frankfurt, Python und Django.",
    "Trainee IT, keine Vorkenntnisse nötig, Englisch.",
]


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "stage21_cache.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:  # stamp: the CLI requires the Alembic head
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    engine.dispose()
    return path


@pytest.fixture()
def session(db_path):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    db = sessionmaker(engine, expire_on_commit=True)()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture()
def reader(db_path):
    engine = build_readonly_engine(f"sqlite:///{db_path.as_posix()}")
    db = readonly_sessionmaker(engine)()
    yield db
    db.close()
    engine.dispose()


def _catalogue(db) -> list[int]:
    get_or_create_candidate_profile(db)
    ids = []
    for n, description in enumerate(DESCRIPTIONS, start=1):
        job = JobRecord(
            fingerprint=f"fp-{n}",
            source="bundesagentur",
            title=f"Entwickler {n} (m/w/d)",
            company=f"Beispiel GmbH {n}",
            location="Frankfurt am Main",
            url=f"https://jobs.example.invalid/{n}",
            description=description,
            score=60,
            recommendation="MAYBE",
            first_seen_at=NOW - timedelta(days=n * 5),
            last_seen_at=NOW,
        )
        db.add(job)
        db.flush()
        ids.append(job.id)
    _application(db, ref="REF-1", job_id=ids[0])
    db.commit()
    return ids


def _application(db, *, ref, job_id=None):
    return ApplicationTracker().create_application(
        db,
        company_name="Beispiel GmbH 1",
        role_title="Entwickler 1 (m/w/d)",
        source="bundesagentur",
        source_reference=ref,
        application_channel=C.EMPLOYER_PORTAL,
        initial_status=S.SUBMITTED,
        origin=O.IMPORT,
        actor="test",
        occurred_at=NOW - timedelta(days=1),
        job_id=job_id,
        correlation_key=f"submission:{ref}",
    )


def _fill(db_path, *extra) -> int:
    return writer_cli.run(["--database", str(db_path), "--apply", *extra])


def _rows(db) -> dict[int, JobFitAssessmentRecord]:
    db.expire_all()
    return {r.job_id: r for r in db.scalars(select(JobFitAssessmentRecord))}


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- equivalence ------------------------------------------------------------------


def test_cached_rows_equal_the_live_assessment(db_path, session):
    ids = _catalogue(session)
    assert _fill(db_path) == 0
    rows = _rows(session)
    assert set(rows) == set(ids)
    for job_id, row in rows.items():
        live = fit_cache.assessment_values(assess_job(session, job_id))
        assert {k: getattr(row, k) for k in live} == live
        assert row.rules_hash == fit_cache.rules_hash()
    current = fit_cache.current_keys(session)
    assert all(rows[i].input_key == current[i] for i in ids)
    # the catalogue exercises more than one tier and the applied flag
    assert len({r.fit_tier for r in rows.values()}) >= 2
    assert rows[ids[0]].applied and not rows[ids[1]].applied


# --- invalidation -----------------------------------------------------------------


def _changed(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.5
    if isinstance(value, datetime):
        return value + timedelta(seconds=1)
    if value is None:
        return "changed"
    return value + " changed"


@pytest.mark.parametrize("column", [c for c in fit_cache.hashed_job_columns() if c != "id"])
def test_every_included_job_column_makes_the_row_stale(session, column):
    job_id = _catalogue(session)[1]
    before = fit_cache.current_keys(session)[job_id]
    value = session.execute(select(JobRecord.__table__.c[column]).where(JobRecord.id == job_id))
    session.execute(
        update(JobRecord.__table__)
        .where(JobRecord.id == job_id)
        .values({column: _changed(value.scalar())})
    )
    session.commit()
    assert fit_cache.current_keys(session)[job_id] != before


def test_fingerprint_and_source_are_included_and_last_seen_at_is_not():
    included = set(fit_cache.hashed_job_columns())
    assert {"fingerprint", "source", "location", "title", "description"} <= included
    assert "last_seen_at" not in included


def test_last_seen_at_alone_keeps_the_row_fresh(session):
    job_id = _catalogue(session)[1]
    before = fit_cache.current_keys(session)[job_id]
    session.execute(
        update(JobRecord).where(JobRecord.id == job_id).values(last_seen_at=NOW + timedelta(days=1))
    )
    session.commit()
    assert fit_cache.current_keys(session)[job_id] == before


def _invalidate_profile(db, job_id):
    db.execute(
        update(CandidateProfileRecord).values(
            profile_version=CandidateProfileRecord.profile_version + 1
        )
    )


def _invalidate_match(db, job_id):
    db.execute(
        insert(CandidateJobMatchRecord).values(
            job_id=job_id,
            candidate_profile_version=1,
            job_snapshot_fingerprint="x" * 64,
            algorithm_version="v12",
            overall_score=50,
            coverage_score=50,
            required_skill_score=50,
            preferred_skill_score=50,
            analysis_json="{}",
            created_at=NOW,
        )
    )


def _invalidate_commute(db, job_id):
    db.execute(
        insert(JobCommuteCheckRecord).values(
            job_id=job_id,
            origin_label="Heusenstamm",
            destination_label="Frankfurt am Main",
            car_route_km=20.0,
            method="MANUAL",
            source_note="test",
            checked_at=NOW,
        )
    )


def _invalidate_application(db, job_id):
    _application(db, ref="REF-NEW")


def _invalidate_application_edit(db, job_id):
    # a field edit without an event (updated_at moves) also invalidates
    db.execute(
        text("UPDATE applications SET destination_url = 'https://x.invalid', updated_at = :t"),
        {"t": NOW + timedelta(days=1)},
    )


def _invalidate_older_application_edit(db, job_id):
    # Codex Stage 21 review P2: an edit of an OLDER application that leaves
    # count, max id and max updated_at unchanged must still invalidate
    db.execute(text("UPDATE applications SET destination_url = 'https://y.invalid' WHERE id = 1"))


def _invalidate_event(db, job_id):
    app_id = db.scalar(text("SELECT id FROM applications ORDER BY id LIMIT 1"))
    ApplicationTracker().record_receipt(
        db, app_id, origin=O.GMAIL, actor="test", correlation_key="gmail:r", occurred_at=NOW
    )


@pytest.mark.parametrize(
    "change",
    [
        _invalidate_profile,
        _invalidate_match,
        _invalidate_commute,
        _invalidate_application,
        _invalidate_application_edit,
        _invalidate_older_application_edit,
        _invalidate_event,
    ],
)
def test_every_other_input_makes_the_row_stale(session, change):
    job_id = _catalogue(session)[1]
    _application(session, ref="REF-2")  # two applications: id 1 is the older one
    session.commit()
    before = fit_cache.current_keys(session)[job_id]
    change(session, job_id)
    session.commit()
    assert fit_cache.current_keys(session)[job_id] != before


def test_commute_key_follows_latest_commute_check_ordering(session):
    job_id = _catalogue(session)[1]
    _invalidate_commute(session, job_id)
    session.commit()
    first = fit_cache.latest_commute_keys(session, [job_id])[job_id]
    # an older measurement inserted later must not become the "latest" one
    session.execute(
        insert(JobCommuteCheckRecord).values(
            job_id=job_id,
            origin_label="Heusenstamm",
            destination_label="Frankfurt am Main",
            car_route_km=99.0,
            method="MANUAL",
            source_note="older",
            checked_at=NOW - timedelta(days=30),
        )
    )
    session.commit()
    assert fit_cache.latest_commute_keys(session, [job_id])[job_id] == first


# --- rules hash -------------------------------------------------------------------


def test_rules_hash_covers_the_import_closure(tmp_path):
    closure = fit_cache.rule_closure()
    assert {
        "app.services.job_fit_assessment",
        "app.services.fit_cache",
        "app.agents.job_fit_signals",
        "app.services.application_guard",
        "app.models.candidate_search",
    } <= set(closure)
    for path in closure.values():
        target = tmp_path / path.relative_to(fit_cache.PROJECT_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    assert fit_cache.compute_rules_hash(tmp_path) == fit_cache.compute_rules_hash()
    # a dependency-only change (not job_fit_assessment.py itself) changes the hash
    signals = tmp_path / "app" / "agents" / "job_fit_signals.py"
    signals.write_text(signals.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert fit_cache.compute_rules_hash(tmp_path) != fit_cache.compute_rules_hash()


def test_excluded_job_columns_are_never_read_by_the_rules():
    readers = {
        name
        for name, path in fit_cache.rule_closure().items()
        for column in fit_cache.JOB_COLUMNS_EXCLUDED
        if column in path.read_text(encoding="utf-8")
    }
    # the model defines the column and fit_cache lists the exclusion; no rule reads it
    assert readers == {"app.db.models", "app.services.fit_cache"}


# --- never stale-as-current ------------------------------------------------------


def test_stale_row_is_never_listed_or_counted_under_its_old_tier(db_path, session, reader):
    ids = _catalogue(session)
    assert _fill(db_path) == 0
    stale_id = ids[2]
    session.execute(
        update(JobFitAssessmentRecord)
        .where(JobFitAssessmentRecord.job_id == stale_id)
        .values(fit_tier="GOOD_MATCH")
    )
    session.execute(update(JobRecord).where(JobRecord.id == stale_id).values(title="Neu (m/w/d)"))
    session.commit()

    page = queries.list_vacancies(reader, queries.VacancyFilters(fit_tier="GOOD_MATCH"))
    assert page.from_cache
    assert stale_id not in {r.job.id for r in page.rows}

    summary = queries.dashboard_summary(
        reader, NO_MAIL, queries.operator_zone("Europe/Berlin"), now=NOW
    )
    assert summary.cache == queries.CacheOverview(fresh=len(ids) - 1, stale=1, not_assessed=0)
    fresh_tiers = [r.fit_tier for i, r in _rows(session).items() if i != stale_id]
    assert summary.tiers_all["GOOD_MATCH"].value == fresh_tiers.count("GOOD_MATCH")
    assert sum(m.value for m in summary.tiers_all.values()) == len(ids) - 1
    assert queries.vacancy_detail(reader, stale_id).cache_state == "STALE"


def test_fresh_rows_are_listed_by_their_cached_tier(db_path, session, reader):
    ids = _catalogue(session)
    assert _fill(db_path) == 0
    rows = _rows(session)
    tier = rows[ids[1]].fit_tier
    page = queries.list_vacancies(reader, queries.VacancyFilters(fit_tier=tier))
    expected = sorted((i for i, r in rows.items() if r.fit_tier == tier), reverse=True)
    assert [r.job.id for r in page.rows] == expected
    assert page.next_before_id is None and not page.budget_exhausted
    applied = queries.list_vacancies(reader, queries.VacancyFilters(applied="yes"))
    assert [r.job.id for r in applied.rows] == [ids[0]]
    assert queries.vacancy_detail(reader, ids[1]).cache_state == "FRESH"


def test_new_vacancy_is_not_assessed_and_window_counts_say_so(db_path, session, reader):
    ids = _catalogue(session)
    assert _fill(db_path) == 0
    session.add(
        JobRecord(
            fingerprint="fp-new",
            source="bundesagentur",
            title="Neu (m/w/d)",
            company="Neu GmbH",
            location="Offenbach",
            url="https://jobs.example.invalid/new",
            description="Python",
            score=60,
            recommendation="MAYBE",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    session.commit()
    summary = queries.dashboard_summary(
        reader, NO_MAIL, queries.operator_zone("Europe/Berlin"), now=NOW
    )
    assert summary.cache.not_assessed == 1 and summary.cache.fresh == len(ids)
    assert "1 not assessed" in summary.tiers["GOOD_MATCH"].reason


def test_empty_cache_keeps_the_stage20_behaviour(session, reader):
    _catalogue(session)
    assert not fit_cache.cache_in_use(reader)
    page = queries.list_vacancies(reader, queries.VacancyFilters(fit_tier="GOOD_MATCH"))
    assert not page.from_cache
    summary = queries.dashboard_summary(
        reader, NO_MAIL, queries.operator_zone("Europe/Berlin"), now=NOW
    )
    assert summary.cache is None and summary.tiers_all is None


def test_ui_session_cannot_write_the_cache(db_path, session, reader):
    ids = _catalogue(session)
    inputs = fit_cache.global_inputs(reader)
    with pytest.raises(Exception) as caught:
        fit_cache.upsert_assessment(
            reader,
            job_id=ids[0],
            key="k" * 64,
            inputs=inputs,
            assessment=assess_job(reader, ids[0]),
        )
        reader.flush()
    assert isinstance(caught.value, ReadOnlySessionError) or "readonly" in str(caught.value)
    reader.rollback()
    assert _rows(session) == {}


# --- writer CLI -------------------------------------------------------------------


def test_dry_run_writes_nothing(db_path, session, capsys):
    _catalogue(session)
    session.close()
    before = _sha(db_path)
    assert writer_cli.run(["--database", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "not_assessed=6 to_assess=6" in out and "DRY RUN" in out
    assert _sha(db_path) == before


def test_apply_is_idempotent_and_limit_and_only_stale_work(db_path, session, capsys):
    ids = _catalogue(session)
    assert _fill(db_path, "--limit", "2") == 0
    assert set(_rows(session)) == set(sorted(ids, reverse=True)[:2])
    assert _fill(db_path) == 0
    assert len(_rows(session)) == len(ids)
    capsys.readouterr()
    assert _fill(db_path) == 0
    assert "APPLIED: 0 rows written" in capsys.readouterr().out

    session.execute(update(JobRecord).where(JobRecord.id == ids[3]).values(title="Neu (m/w/d)"))
    session.add(
        JobRecord(
            fingerprint="fp-new",
            source="bundesagentur",
            title="Neu",
            company="Neu GmbH",
            url="https://jobs.example.invalid/new",
            score=1,
            recommendation="SKIP",
        )
    )
    session.commit()
    assert _fill(db_path, "--only-stale") == 0
    out = capsys.readouterr().out
    assert "stale=1 not_assessed=1 to_assess=1" in out and "APPLIED: 1 rows written" in out


def test_interrupted_backfill_keeps_finished_batches_and_resumes(db_path, session, monkeypatch):
    ids = _catalogue(session)
    monkeypatch.setattr(writer_cli, "BATCH_SIZE", 2)
    real = writer_cli.assess_batch
    calls = []

    def interrupt_second(db, batch):
        calls.append(batch)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(db, batch)

    monkeypatch.setattr(writer_cli, "assess_batch", interrupt_second)
    assert _fill(db_path) == 130
    assert len(_rows(session)) == 2
    monkeypatch.setattr(writer_cli, "assess_batch", real)
    assert _fill(db_path) == 0
    assert set(_rows(session)) == set(ids)


def test_writer_refuses_backups_and_needs_a_snapshot_for_production(
    db_path, session, tmp_path, monkeypatch, capsys
):
    _catalogue(session)
    session.close()
    backup = tmp_path / "copy.backup.db"
    shutil.copyfile(db_path, backup)
    assert writer_cli.run(["--database", str(backup), "--apply"]) == 2

    monkeypatch.setattr(writer_cli, "classify_path", lambda _p: DatabaseKind.PRODUCTION_RUNTIME)
    before = _sha(db_path)
    assert writer_cli.run(["--database", str(db_path), "--apply"]) == 2
    assert "needs --snapshot" in capsys.readouterr().err
    assert _sha(db_path) == before
    snapshots = tmp_path / "snapshots"
    assert (
        writer_cli.run(["--database", str(db_path), "--apply", "--snapshot", str(snapshots)]) == 0
    )
    (copy,) = snapshots.iterdir()
    assert ".pre-stage21-" in copy.name


def test_writer_refuses_a_database_without_the_cache_table(db_path, capsys):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE job_fit_assessments"))
    engine.dispose()
    assert writer_cli.run(["--database", str(db_path)]) == 2
    assert "job_fit_assessments is missing" in capsys.readouterr().err


# --- pages render from the cache ------------------------------------------------

from tests.test_stage20_ui import _login, client_factory, db_url  # noqa: E402,F401


def test_pages_render_from_the_cache(db_url, client_factory):  # noqa: F811
    engine = create_engine(db_url)
    db = sessionmaker(engine)()
    ids = _catalogue(db)
    inputs = fit_cache.global_inputs(db)
    keys = fit_cache.current_keys(db, ids, inputs)
    for job_id in ids:
        fit_cache.upsert_assessment(
            db, job_id=job_id, key=keys[job_id], inputs=inputs, assessment=assess_job(db, job_id)
        )
    db.commit()
    tier = db.scalar(
        select(JobFitAssessmentRecord.fit_tier).where(JobFitAssessmentRecord.job_id == ids[1])
    )
    db.close()
    engine.dispose()

    client = client_factory()
    _login(client)
    dashboard = client.get("/ui/").text
    assert "all stored vacancies" in dashboard and f"{len(ids)} fresh" in dashboard
    listing = client.get(f"/ui/vacancies?fit_tier={tier}").text
    assert "use the assessment cache" in listing and f"/ui/vacancies/{ids[1]}" in listing
    assert "Cached assessment" in client.get(f"/ui/vacancies/{ids[1]}").text
