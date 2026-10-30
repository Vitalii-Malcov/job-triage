"""Stage 22 P1-2: the vacancy identity dry run is read-only and names its risks."""

import hashlib
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.models import ApplicationRecord, Base, JobRecord
from app.db.repositories import upsert_job
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.job import Job, JobScore
from app.services.application_tracker import ApplicationTracker
from app.services.readiness import expected_alembic_heads
from scripts import vacancy_identity_report as cli

SCORE = JobScore(score=50, recommendation="MAYBE")


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "identity.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    session = sessionmaker(engine, expire_on_commit=False)()

    def job(ref, url, title="Python Developer", company="ACME GmbH"):
        record, _ = upsert_job(
            session,
            Job(
                source="bundesagentur", title=title, company=company, url=url, source_reference=ref
            ),
            SCORE,
        )
        return record

    job("10001-1-S", "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1-S")
    job("10001-1-S", "https://acme.example/jobs/1")  # same posting, employer URL
    applied = job("10001-2-S", "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-2-S")
    job("10001-2-S", "https://acme.example/jobs/2", title="Senior Python Developer")
    job("10001-3-S", "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-3-S")  # alone
    ApplicationTracker().create_application(
        session,
        company_name="ACME GmbH",
        role_title="Python Developer",
        source="bundesagentur",
        source_reference="10001-2-S",
        application_channel=C.EMAIL,
        origin=O.OPERATOR,
        actor="test",
        job_id=applied.id,
    )
    session.commit()
    session.close()
    engine.dispose()
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_groups_risks_and_read_only(db_path, capsys):
    before = _sha(db_path)
    assert cli.run(["--database", str(db_path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert _sha(db_path) == before  # nothing written
    assert (result["groups"], result["rows"], result["blocked_groups"]) == (2, 4, 1)
    by_ref = {g["reference"]: g for g in result["details"]}
    plain = by_ref["10001-1-S"]
    assert plain["automatic_merge_allowed"] and plain["keep"] == min(
        r["job_id"] for r in plain["rows"]
    )
    applied = by_ref["10001-2-S"]
    assert not applied["automatic_merge_allowed"]
    assert {"APPLICATION_OR_APPROVAL", "TITLE_CHANGED"} <= set(applied["risks"])
    counts = {r["job_id"]: r["references"] for r in applied["rows"]}
    assert any(c.get("applications") == 1 for c in counts.values())
    assert all(c.get("job_reference_tokens", 0) >= 0 for c in counts.values())


def test_text_report_says_nothing_is_merged(db_path, capsys):
    assert cli.run(["--database", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "nothing merged" in out and "keep job" in out


def test_no_groups_without_shared_references(tmp_path, capsys):
    path = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    engine.dispose()
    assert cli.run(["--database", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["groups"] == 0


def test_the_application_is_not_touched(db_path):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with sessionmaker(engine)() as session:
        jobs_before = session.query(JobRecord).count()
        apps_before = session.query(ApplicationRecord).count()
    cli.run(["--database", str(db_path)])
    with sessionmaker(engine)() as session:
        assert session.query(JobRecord).count() == jobs_before
        assert session.query(ApplicationRecord).count() == apps_before
    engine.dispose()


def test_composite_unique_collisions_and_reference_linked_applications(db_path, capsys):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                text("SELECT id FROM jobs WHERE source_reference = '10001-1-S' ORDER BY id")
            )
        ]
        for job_id in ids:  # identical cache identity on both rows of the group
            conn.execute(
                text(
                    "INSERT INTO candidate_job_matches (job_id, candidate_profile_version, "
                    "job_snapshot_fingerprint, algorithm_version, overall_score, coverage_score, "
                    "required_skill_score, preferred_skill_score, analysis_json, created_at) "
                    "VALUES (:j, 1, 'same', 'v12', 50, 50, 50, 50, '{}', '2026-10-30')"
                ),
                {"j": job_id},
            )
        # an application with the external identity only (job_id NULL)
        conn.execute(text("UPDATE applications SET job_id = NULL"))
        conn.execute(text("UPDATE applications SET source_reference = '10001-1-S'"))
    engine.dispose()
    assert cli.run(["--database", str(db_path), "--json"]) == 0
    group = {g["reference"]: g for g in json.loads(capsys.readouterr().out)["details"]}["10001-1-S"]
    assert "UNIQUE_CONFLICT:candidate_job_matches" in group["risks"]
    assert "APPLICATION_OR_APPROVAL" in group["risks"]
    assert group["applications_by_reference"] == 1 and not group["automatic_merge_allowed"]
