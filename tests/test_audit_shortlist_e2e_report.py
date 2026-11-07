"""The end-to-end shortlist report: refuses unsafe targets and never admits admin/support
titles or NOT_SUITABLE vacancies into the automatic shortlist."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database_target import DatabaseKind
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import Base, JobRecord
from scripts import shortlist_e2e_report as report

NOW = datetime(2026, 11, 7, tzinfo=UTC)
TITLES = [
    ("Junior Python Entwickler (m/w/d)", "Python, FastAPI, SQL. Deutsch B1. 100% Remote."),
    ("IT-Administrator (m/w/d)", "Windows Server, Active Directory, Benutzerverwaltung."),
    ("Systemadministratoren (m/w/d)", "Linux, Netzwerk, Monitoring."),
    ("IT-Support Helpdesk 1st Level", "Anwenderbetreuung, Tickets."),
    ("Senior Java Architekt", "Mindestens 10 Jahre Erfahrung. Deutsch C2."),
]


@pytest.fixture()
def copy_db(tmp_path):
    path = tmp_path / "eval_copy.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        get_or_create_candidate_profile(db)
        for n, (title, description) in enumerate(TITLES):
            db.add(
                JobRecord(
                    fingerprint=f"fp-{n}",
                    source="bundesagentur",
                    title=title,
                    company=f"Firma {n}",
                    location="Frankfurt am Main",
                    url=f"https://jobs.example.invalid/{n}",
                    description=description,
                    score=90,
                    recommendation="APPLY",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )
        db.commit()
    engine.dispose()
    return path


def test_admin_support_and_not_suitable_never_reach_the_automatic_shortlist(copy_db):
    engine = create_engine(f"sqlite:///{copy_db.as_posix()}")
    with Session(engine) as db:
        result = report.analyse(db, Settings(_env_file=None))
    engine.dispose()
    summary = result["summary"]
    assert summary["jobs_total"] == len(TITLES)
    assert summary["admin_support_titles_total"] == 3
    assert summary["VIOLATION_admin_support_in_automatic_shortlist"] == []
    assert summary["VIOLATION_not_suitable_in_automatic_shortlist"] == []
    by_title = {r["title"]: r for r in result["rows"]}
    for title in ("IT-Administrator (m/w/d)", "Systemadministratoren (m/w/d)"):
        assert by_title[title]["auto"] is False
        assert by_title[title]["tier"] == "NOT_SUITABLE"


def test_report_refuses_missing_and_production_targets(tmp_path, copy_db, monkeypatch):
    assert report.main(["--db", str(tmp_path / "absent.db")]) == 2
    monkeypatch.setattr(report, "classify_path", lambda _p: DatabaseKind.PRODUCTION_RUNTIME)
    assert report.main(["--db", str(copy_db)]) == 2


def test_json_output_never_targets_a_database_or_an_existing_file(tmp_path, copy_db):
    assert report.main(["--db", str(copy_db), "--json", str(copy_db)]) == 2
    other = tmp_path / "other.db"
    assert report.main(["--db", str(copy_db), "--json", str(other)]) == 2
    existing = tmp_path / "report.json"
    existing.write_text("keep", encoding="utf-8")
    assert report.main(["--db", str(copy_db), "--json", str(existing)]) == 2
    assert existing.read_text(encoding="utf-8") == "keep"
