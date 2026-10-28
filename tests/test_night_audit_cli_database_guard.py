"""Night audit 2026-10-27: Stage 17A database roles also bind the CLI tools.

Before: web/scheduler startup refused backup and pilot databases, but every
CLI (tracker, Stage 16 operator, Stage 17 config/job-fit) opened any SQLite
file, so `--apply` against a backup copy wrote into it without a word, and a
test-role process could open the production runtime database.
"""

import re
from pathlib import Path

import pytest

import scripts.stage15_application_tracker as tracker
from app.core.database_target import PRODUCTION_RUNTIME_DB


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture()
def backup_db(tmp_path):
    return _file(tmp_path / "job-triage-backups" / "night" / "copy.db")


@pytest.mark.parametrize("command", [["record-submission"], ["transition", "1", "RECEIVED"]])
def test_cli_apply_against_backup_is_refused(backup_db, capsys, command):
    extra = {
        "record-submission": [
            "--company",
            "C",
            "--role",
            "R",
            "--source",
            "s",
            "--reference",
            "ref-1",
            "--actor",
            "a",
            "--evidence",
            "e",
            "--channel",
            "EMAIL",
            "--submitted-at",
            "2026-10-27T10:00:00+00:00",
        ],
        "transition": ["--actor", "a", "--reason", "r"],
    }[command[0]]
    code = tracker.run(["--database", str(backup_db), *command, *extra, "--apply"])
    assert code == 2
    assert "backup" in capsys.readouterr().err


def test_backup_may_still_be_read(backup_db):
    # Read-only: passes the kind guard (then fails later only because the
    # empty test file has no schema).
    tracker._refuse_kind_for_cli(backup_db, writes=False)


def test_pilot_write_needs_pilot_role(monkeypatch):
    pilot = tracker.Path(tracker.__file__).resolve().parents[1] / "stage99_nightaudit_pilot.db"
    monkeypatch.setenv("DATABASE_ROLE", "test")
    with pytest.raises(tracker.TargetRefusedError, match="DATABASE_ROLE=pilot"):
        tracker._refuse_kind_for_cli(pilot, writes=True)
    monkeypatch.setenv("DATABASE_ROLE", "pilot")
    tracker._refuse_kind_for_cli(pilot, writes=True)


@pytest.mark.parametrize("role", ["test", "development"])
@pytest.mark.parametrize("writes", [False, True])
def test_test_role_never_opens_production(monkeypatch, role, writes):
    monkeypatch.setenv("DATABASE_ROLE", role)
    with pytest.raises(tracker.TargetRefusedError, match="production runtime"):
        tracker._refuse_kind_for_cli(PRODUCTION_RUNTIME_DB, writes=writes)


def test_production_role_and_unset_role_may_use_production(monkeypatch):
    for role in ("production", ""):
        monkeypatch.setenv("DATABASE_ROLE", role)
        tracker._refuse_kind_for_cli(PRODUCTION_RUNTIME_DB, writes=True)


def test_invalid_role_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_ROLE", "prod")
    with pytest.raises(tracker.TargetRefusedError, match="DATABASE_ROLE must be one of"):
        tracker._refuse_kind_for_cli(_file(tmp_path / "x.db"), writes=False)


def test_suite_runs_under_test_role():
    import os

    assert os.environ["DATABASE_ROLE"] == "test"


def test_cli_against_production_is_refused_inside_the_test_suite(capsys):
    """The guard proven end to end: from inside pytest the real runtime
    database cannot be opened by the tracker CLI at all."""
    code = tracker.run(["--database", str(PRODUCTION_RUNTIME_DB), "list"])
    assert code == 2
    assert "production runtime" in capsys.readouterr().err


@pytest.mark.parametrize(
    "script",
    [
        "stage16_telegram_operator",
        "stage17_candidate_config",
        "stage17_job_fit",
        "stage15_import_ald_application",
    ],
)
def test_every_cli_passes_its_write_mode(script):
    source = (Path(tracker.__file__).parent / f"{script}.py").read_text(encoding="utf-8")
    assert re.search(r"resolve_engine\(\s*args\.database,\s*args\.database_url,\s*writes=", source)


# --- Codex night-audit review -------------------------------------------


def test_hard_link_to_production_is_production(tmp_path, monkeypatch):
    import os

    import app.core.database_target as target

    production = tmp_path / "runtime" / "job_triage_runtime.db"
    production.parent.mkdir()
    production.write_bytes(b"x")
    link = tmp_path / "scratch.db"
    os.link(production, link)
    monkeypatch.setattr(target, "PRODUCTION_RUNTIME_DB", production)
    assert target.classify_path(link) is target.DatabaseKind.PRODUCTION_RUNTIME
    monkeypatch.setenv("DATABASE_ROLE", "test")
    with pytest.raises(tracker.TargetRefusedError, match="production runtime"):
        tracker._refuse_kind_for_cli(link, writes=False)


@pytest.mark.parametrize(
    ("name", "role", "message"),
    [
        ("jobtriage_backup", "production", "backup"),
        ("jobtriage_pre_profile", "production", "backup"),
        ("stage16_pilot", "test", "DATABASE_ROLE=pilot"),
    ],
)
def test_postgres_backup_and_pilot_writes_are_refused(monkeypatch, capsys, name, role, message):
    monkeypatch.setenv("DATABASE_ROLE", role)
    url = f"postgresql+psycopg://u:p@localhost:5432/{name}"
    with pytest.raises(tracker.TargetRefusedError, match=message):
        tracker.resolve_engine(None, url, writes=True)


def test_postgres_backup_may_be_read(monkeypatch):
    from app.core.database_target import DatabaseKind

    monkeypatch.setenv("DATABASE_ROLE", "production")
    tracker._refuse_kind_for_cli(None, writes=False, kind=DatabaseKind.BACKUP)


@pytest.mark.parametrize(
    ("role", "target", "kind_name"),
    [
        ("production", "other", "OTHER_SQLITE"),
        ("pilot", "server", "SERVER"),
    ],
)
def test_stated_role_binds_writes(monkeypatch, tmp_path, role, target, kind_name):
    """Codex round 4: the same role table as the web/scheduler startup."""
    from app.core.database_target import DatabaseKind

    monkeypatch.setenv("DATABASE_ROLE", role)
    path = _file(tmp_path / "some.db") if target == "other" else None
    kind = None if target == "other" else DatabaseKind.SERVER
    with pytest.raises(tracker.TargetRefusedError, match=kind_name):
        tracker._refuse_kind_for_cli(path, writes=True, kind=kind)
    tracker._refuse_kind_for_cli(path, writes=False, kind=kind)  # reading stays possible
