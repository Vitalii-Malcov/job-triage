"""Stage 22 P1: the batch matcher run fills only missing analyses, offline."""

import hashlib
import shutil

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.database_target import DatabaseKind
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import Base, CandidateJobMatchRecord
from app.db.repositories import upsert_job
from app.models.job import Job, JobScore
from app.services.readiness import expected_alembic_heads
from scripts import stage22_match_vacancies as cli


def _database(path, *, profile=True):
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    session = sessionmaker(engine)()
    if profile:
        get_or_create_candidate_profile(session)
        session.commit()
    for n in range(3):
        upsert_job(
            session,
            Job(
                source="bundesagentur",
                title=f"Python Developer {n}",
                company="ACME",
                url=f"https://example.com/j/{n}",
                description="Python, SQL, Docker. Deutsch B1.",
            ),
            JobScore(score=50, recommendation="MAYBE"),
        )
    session.close()
    engine.dispose()
    return path


def _matches(path):
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as conn:
        n = conn.execute(select(func.count()).select_from(CandidateJobMatchRecord)).scalar_one()
    engine.dispose()
    return n


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dry_run_apply_and_rerun(tmp_path, capsys):
    path = _database(tmp_path / "m.db")
    before = _sha(path)
    assert cli.run(["--database", str(path)]) == 0
    assert "without_current_analysis=3" in capsys.readouterr().out
    assert _sha(path) == before and _matches(path) == 0
    assert cli.run(["--database", str(path), "--apply"]) == 0
    assert "APPLIED: 3 analyses, 0 failed" in capsys.readouterr().out
    assert _matches(path) == 3
    assert cli.run(["--database", str(path), "--apply"]) == 0
    assert "without_current_analysis=0" in capsys.readouterr().out
    assert _matches(path) == 3


def test_limit_and_failures_are_reported(tmp_path, capsys, monkeypatch):
    path = _database(tmp_path / "m.db")
    calls = []

    def broken(session, job_id, *, force_recompute):
        calls.append(job_id)
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "prepare_candidate_job_match", broken)
    assert cli.run(["--database", str(path), "--apply", "--limit", "2"]) == 1
    out = capsys.readouterr().out
    assert len(calls) == 2 and out.count("FAILED: job") == 2


def test_refusals(tmp_path, monkeypatch, capsys):
    path = _database(tmp_path / "m.db")
    backup = tmp_path / "copy.backup.db"
    shutil.copyfile(path, backup)
    assert cli.run(["--database", str(backup), "--apply"]) == 2
    assert cli.run(["--database", str(path), "--limit", "0"]) == 2
    monkeypatch.setattr(cli, "classify_path", lambda _p: DatabaseKind.PRODUCTION_RUNTIME)
    assert cli.run(["--database", str(path), "--apply"]) == 2
    assert "needs --snapshot" in capsys.readouterr().err
    snapshots = tmp_path / "snap"
    assert cli.run(["--database", str(path), "--apply", "--snapshot", str(snapshots)]) == 0
    (copy,) = snapshots.iterdir()
    assert ".pre-stage22-match-" in copy.name


def test_no_profile_is_refused(tmp_path, capsys):
    path = _database(tmp_path / "m.db", profile=False)
    assert cli.run(["--database", str(path)]) == 2
    assert "no candidate profile" in capsys.readouterr().err
