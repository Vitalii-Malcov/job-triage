"""Stage 17A: explicit, fail-closed database selection."""

import pytest
from fastapi.testclient import TestClient

from app.core import database_target as dt
from app.core.canonical_runtime import canonical_environment
from app.core.config import Settings
from app.core.database_target import (
    PRODUCTION_RUNTIME_DB,
    PROJECT_ROOT,
    DatabaseKind,
    DatabaseTargetError,
    check_database_target,
    classify_database_url,
)


def _url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (_url(PRODUCTION_RUNTIME_DB), DatabaseKind.PRODUCTION_RUNTIME),
        (
            _url(PROJECT_ROOT / "runtime" / "." / "job_triage_runtime.db"),
            DatabaseKind.PRODUCTION_RUNTIME,
        ),
        (
            f"sqlite:///file:{PRODUCTION_RUNTIME_DB.as_posix()}?mode=ro&uri=true",
            DatabaseKind.PRODUCTION_RUNTIME,
        ),
        (_url(PROJECT_ROOT / "stage11_pilot.db"), DatabaseKind.PILOT),
        (_url(PROJECT_ROOT / "stage14b_canonical_v12.db"), DatabaseKind.PILOT),
        (_url(PROJECT_ROOT / "runtime" / "stage16_pilot_scratch.db"), DatabaseKind.PILOT),
        (
            _url(PROJECT_ROOT / "runtime" / "job_triage_runtime.pre-f4b8d2a6c9e1.db"),
            DatabaseKind.BACKUP,
        ),
        (_url(PROJECT_ROOT / "job_search.db"), DatabaseKind.OTHER_SQLITE),
        ("sqlite://", DatabaseKind.MEMORY),
        ("sqlite:///:memory:", DatabaseKind.MEMORY),
        ("postgresql+psycopg://u:secret@db:5432/jobtriage", DatabaseKind.SERVER),
        ("mysql://u:p@h/db", DatabaseKind.INVALID),
        ("not a url", DatabaseKind.INVALID),
    ],
)
def test_classify_database_url(url, kind):
    assert classify_database_url(url)[0] is kind


def test_relative_pilot_url_resolves_against_the_working_directory(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)
    assert classify_database_url("sqlite:///./stage11_pilot.db")[0] is DatabaseKind.PILOT
    assert classify_database_url("sqlite:///runtime/job_triage_runtime.db")[0] is (
        DatabaseKind.PRODUCTION_RUNTIME
    )


def test_pilot_name_outside_the_project_is_an_ordinary_file(tmp_path):
    assert (
        classify_database_url(_url(tmp_path / "stage11_pilot.db"))[0] is DatabaseKind.OTHER_SQLITE
    )


def test_backup_directory_is_a_backup(tmp_path):
    path = tmp_path / "job-triage-backups" / "job_triage_runtime.db"
    assert classify_database_url(_url(path))[0] is DatabaseKind.BACKUP


def _settings(url: str, role: str = "") -> Settings:
    return Settings(_env_file=None, database_url=url, database_role=role, postgres_host="")


@pytest.mark.parametrize(
    ("url", "role"),
    [
        (_url(PRODUCTION_RUNTIME_DB), ""),
        (_url(PRODUCTION_RUNTIME_DB), "production"),
        ("postgresql+psycopg://u:p@db/jobtriage", "production"),
        (_url(PROJECT_ROOT / "stage11_pilot.db"), "pilot"),
        (_url(PROJECT_ROOT / "job_search.db"), ""),
        (_url(PROJECT_ROOT / "job_search.db"), "development"),
        ("sqlite://", "test"),
    ],
)
def test_accepted_combinations(url, role):
    target = check_database_target(_settings(url, role))
    assert "secret" not in target.describe()


@pytest.mark.parametrize(
    ("url", "role", "message"),
    [
        # the incident class: .env pilot without an explicit role
        (_url(PROJECT_ROOT / "stage11_pilot.db"), "", "DATABASE_ROLE=pilot"),
        (_url(PROJECT_ROOT / "stage11_pilot.db"), "production", "PILOT"),
        (_url(PROJECT_ROOT / "stage11_pilot.db"), "test", "PILOT"),
        (_url(PRODUCTION_RUNTIME_DB), "pilot", "PRODUCTION_RUNTIME"),
        (_url(PRODUCTION_RUNTIME_DB), "test", "PRODUCTION_RUNTIME"),
        (_url(PRODUCTION_RUNTIME_DB), "development", "PRODUCTION_RUNTIME"),
        (_url(PROJECT_ROOT / "job_search.db"), "production", "OTHER_SQLITE"),
        (_url(PROJECT_ROOT / "runtime" / "x.pre-migration.db"), "", "BACKUP"),
        (_url(PROJECT_ROOT / "runtime" / "x.pre-migration.db"), "production", "BACKUP"),
        ("mysql://u:p@h/db", "", "supported"),
    ],
)
def test_refused_combinations(url, role, message):
    with pytest.raises(DatabaseTargetError, match=message):
        check_database_target(_settings(url, role))


def test_unknown_role_is_refused():
    with pytest.raises(DatabaseTargetError, match="DATABASE_ROLE must be one of"):
        check_database_target(_settings("sqlite://", "prod"))


def test_refusal_never_echoes_a_password():
    with pytest.raises(DatabaseTargetError) as exc:
        check_database_target(_settings("postgresql+psycopg://u:hunter2@db/x", "pilot"))
    assert "hunter2" not in str(exc.value)


def test_canonical_runtime_declares_the_pilot_role(tmp_path):
    assert canonical_environment(tmp_path / "x.db")["DATABASE_ROLE"] == "pilot"


def test_test_suite_runs_on_an_isolated_database():
    import os

    assert os.environ["DATABASE_ROLE"] == "test"
    kind, _ = classify_database_url(os.environ["DATABASE_URL"])
    assert kind is DatabaseKind.OTHER_SQLITE


def test_web_startup_refuses_an_implicit_pilot_database(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "get_settings", lambda: _settings(_url(PROJECT_ROOT / "stage11_pilot.db"))
    )
    with pytest.raises(DatabaseTargetError):
        with TestClient(main.app):
            pass


def test_scheduler_refuses_a_pilot_database_before_polling(monkeypatch, capsys):
    import app.scheduler as scheduler

    monkeypatch.setattr(
        scheduler, "get_settings", lambda: _settings(_url(PROJECT_ROOT / "stage11_pilot.db"))
    )
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a: None)
    assert scheduler.main() == 1
    assert "DATABASE_ROLE=pilot" in capsys.readouterr().err


def test_module_has_no_import_time_side_effects():
    assert dt.PRODUCTION_RUNTIME_DB.name == "job_triage_runtime.db"


@pytest.mark.parametrize(
    "url",
    [
        # SQLite percent-decodes `file:` URI paths (Codex Stage 17 P1)
        "sqlite:///file:runtime/job_triage_runtime%2Edb?uri=true",
        "sqlite:///file:runtime%2Fjob_triage_runtime.db?mode=ro&uri=true",
        "sqlite:///runtime/job_triage_runtime%2Edb",
    ],
)
def test_percent_encoded_production_spelling_is_still_production(monkeypatch, url):
    monkeypatch.chdir(PROJECT_ROOT)
    assert classify_database_url(url)[0] is DatabaseKind.PRODUCTION_RUNTIME
    with pytest.raises(DatabaseTargetError, match="PRODUCTION_RUNTIME"):
        check_database_target(_settings(url, "test"))


def test_percent_encoded_pilot_spelling_is_refused_without_a_role(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)
    with pytest.raises(DatabaseTargetError, match="DATABASE_ROLE=pilot"):
        check_database_target(_settings("sqlite:///file:stage11_pilot%2Edb?uri=true"))


# --- Codex Stage 17 re-review regressions ----------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("postgresql+psycopg://u:p@db/jobtriage_pilot", DatabaseKind.PILOT),
        ("postgresql+psycopg://u:p@db/stage11", DatabaseKind.PILOT),
        ("postgresql+psycopg://u:p@db/jobtriage_backup_2026", DatabaseKind.BACKUP),
        ("postgresql+psycopg://u:p@db/jobtriage", DatabaseKind.SERVER),
    ],
)
def test_postgres_database_names_are_classified(url, kind):
    assert classify_database_url(url)[0] is kind


def test_postgres_pilot_is_refused_for_production_and_unset_roles():
    url = "postgresql+psycopg://u:p@db/jobtriage_pilot"
    for role in ("", "production"):
        with pytest.raises(DatabaseTargetError):
            check_database_target(_settings(url, role))
    check_database_target(_settings(url, "pilot"))


def test_a_named_memory_uri_is_memory(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)
    url = "sqlite:///file:runtime/job_triage_runtime.db?mode=memory&cache=shared&uri=true"
    assert classify_database_url(url)[0] is DatabaseKind.MEMORY
