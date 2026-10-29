"""Stage 20: the one-command operator UI launcher (scripts/run_operator_ui.py)."""

import sqlite3

import pytest

from app.core.config import Settings
from scripts import run_operator_ui as launcher


@pytest.fixture()
def database(tmp_path):
    path = tmp_path / "ui.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO jobs (title) VALUES ('Python Entwickler')")
    conn.commit()
    conn.close()
    return path


def test_every_forced_flag_is_a_real_setting():
    fields = {name.upper() for name in Settings.model_fields}
    assert set(launcher.FORCED_ENV) <= fields


def test_forced_environment_overrides_dotenv(tmp_path, monkeypatch, database):
    (tmp_path / ".env").write_text(
        "API_KEY=from-dotenv\nTELEGRAM_BOT_TOKEN=123:real\nOUTBOUND_SENDING_ENABLED=true\n"
        "ALEMBIC_AUTO_UPGRADE=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_KEY", raising=False)
    env = launcher.server_environment({}, database)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=".env")  # conftest disables .env globally
    assert settings.api_key == "from-dotenv"  # the key still comes from .env
    assert settings.telegram_bot_token == ""  # an empty override beats .env
    assert settings.ui_enabled and not settings.outbound_sending_enabled
    assert not settings.alembic_auto_upgrade
    assert settings.database_url == f"sqlite:///{database.resolve().as_posix()}"


def test_refuses_a_missing_database_without_creating_it(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert launcher.main(["--database", str(missing)], run=pytest.fail) == 2
    assert not missing.exists()
    assert "REFUSED" in capsys.readouterr().err


def test_refuses_without_api_key(database, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("API_KEY", raising=False)
    assert launcher.main(["--database", str(database)], run=pytest.fail) == 2
    assert "API_KEY" in capsys.readouterr().err


def test_unchanged_database_and_the_server_command(database, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "secret-launcher-key")
    calls = []

    def run(command, env):
        calls.append((command, env))
        raise KeyboardInterrupt  # Ctrl+C still reaches the after-check

    assert launcher.main(["--database", str(database), "--port", "8010"], run=run) == 0
    [(command, env)] = calls
    assert command[-4:] == ["--host", "127.0.0.1", "--port", "8010"]
    assert env["UI_ENABLED"] == "true" and env["TELEGRAM_BOT_TOKEN"] == ""
    out = capsys.readouterr().out
    assert "UNCHANGED" in out and "secret-launcher-key" not in out


def test_a_change_while_running_is_reported(database, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "k")

    def run(command, env):
        conn = sqlite3.connect(database)
        conn.execute("INSERT INTO jobs (title) VALUES ('written by someone else')")
        conn.commit()
        conn.close()

    assert launcher.main(["--database", str(database)], run=run) == 5
    assert "jobs: 1 -> 2 rows" in capsys.readouterr().out


def test_snapshot_is_a_complete_copy(database, tmp_path):
    copy = launcher.snapshot(database, tmp_path / "backups")
    assert copy.name.startswith("ui.pre-ui-") and copy.suffix == ".db"
    assert launcher.fingerprint(copy)["counts"] == launcher.fingerprint(database)["counts"]


def test_postgres_settings_in_dotenv_cannot_redirect_the_ui(database, monkeypatch, tmp_path):
    # Codex final review P2: POSTGRES_HOST rebuilds DATABASE_URL as PostgreSQL.
    (tmp_path / ".env").write_text(
        "API_KEY=k\nPOSTGRES_HOST=db.example.invalid\nPOSTGRES_USER=u\n"
        "POSTGRES_PASSWORD=p\nPOSTGRES_DB=jobs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": ".env"})
    env = launcher.server_environment({}, database)
    assert launcher._resolved_settings(env).database_url == env["DATABASE_URL"]
    assert launcher.main(["--database", str(database)], run=lambda command, env: 0) == 0


def test_refuses_when_the_configuration_resolves_elsewhere(database, monkeypatch, capsys):
    monkeypatch.setenv("API_KEY", "k")
    other = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/db", api_key="k")
    monkeypatch.setattr(launcher, "_resolved_settings", lambda env: other)
    assert launcher.main(["--database", str(database)], run=pytest.fail) == 2
    assert "different database" in capsys.readouterr().err


def test_an_update_kept_in_the_wal_is_reported(database, monkeypatch, tmp_path, capsys):
    # Codex final review P2: same row counts, change not yet checkpointed.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "k")
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")  # before the baseline fingerprint
    writer.commit()
    main_file = database.read_bytes()

    def run(command, env):
        writer.execute("UPDATE jobs SET title = 'changed'")
        writer.commit()  # stays in the WAL: the writer is still open, no checkpoint
        return 0

    try:
        assert launcher.main(["--database", str(database)], run=run) == 5
        # the main file is byte-identical: only the content hash can see this
        assert database.read_bytes() == main_file
    finally:
        writer.close()
    out = capsys.readouterr().out
    assert "content changed" in out and "rows" not in out.split("CHANGED", 1)[1]


def test_a_failed_server_is_not_reported_as_success(database, monkeypatch, tmp_path, capsys):
    # Codex final review P2: e.g. port 8000 already in use.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "k")
    assert launcher.main(["--database", str(database)], run=lambda command, env: 1) == 3
    assert "SERVER FAILED" in capsys.readouterr().out
