"""Audit OPS-03: restore rehearsal. Every restore here targets a temporary scratch database."""

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import backup_sqlite, restore_sqlite
from scripts.restore_sqlite import RestoreRefused, restore

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _make_db(path: Path, jobs: int = 50) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT);"
        "CREATE TABLE applications (id INTEGER PRIMARY KEY);"
        "CREATE TABLE application_events (id INTEGER PRIMARY KEY);"
        "CREATE TABLE gmail_messages (id INTEGER PRIMARY KEY);"
    )
    connection.executemany(
        "INSERT INTO jobs (title) VALUES (?)", [(f"job {i}",) for i in range(jobs)]
    )
    connection.executemany("INSERT INTO applications DEFAULT VALUES", [()] * 3)
    connection.commit()
    connection.close()
    return path


def _count(path: Path, table: str = "jobs") -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


@pytest.fixture()
def backup_file(tmp_path):
    source = _make_db(tmp_path / "src" / "work.db")
    return backup_sqlite.backup(source, tmp_path / "backups", now=NOW)


def test_restore_to_a_new_path_reproduces_the_backup(tmp_path, backup_file):
    target = tmp_path / "scratch" / "restored.db"
    target.parent.mkdir()
    assert restore(backup_file, target) is None
    assert _count(target) == 50 and _count(target, "applications") == 3
    assert (
        hashlib.sha256(target.read_bytes()).hexdigest()
        == hashlib.sha256(backup_file.read_bytes()).hexdigest()
    )
    assert backup_file.exists()  # the backup itself is never consumed


def test_existing_target_is_refused_without_replace(tmp_path, backup_file):
    target = _make_db(tmp_path / "scratch" / "live.db", jobs=7)
    with pytest.raises(RestoreRefused, match="exists"):
        restore(backup_file, target)
    assert _count(target) == 7


def test_replace_keeps_the_previous_file(tmp_path, backup_file):
    target = _make_db(tmp_path / "scratch" / "live.db", jobs=7)
    kept = restore(backup_file, target, replace=True, now=NOW)
    assert kept is not None and kept.name == "live.db.pre-restore-20261107T120000Z"
    assert _count(kept) == 7  # nothing was lost
    assert _count(target) == 50


def test_replace_refuses_a_database_that_looks_in_use(tmp_path, backup_file):
    target = _make_db(tmp_path / "scratch" / "live.db", jobs=7)
    target.with_name("live.db-wal").write_bytes(b"")
    with pytest.raises(RestoreRefused, match="in use"):
        restore(backup_file, target, replace=True)
    assert _count(target) == 7


def test_tampered_backup_is_refused(tmp_path, backup_file):
    with backup_file.open("ab") as handle:
        handle.write(b"tamper")
    target = tmp_path / "scratch" / "restored.db"
    target.parent.mkdir()
    with pytest.raises(RestoreRefused, match="not a verified backup"):
        restore(backup_file, target)
    assert not target.exists()


def test_backup_without_sidecar_is_refused(tmp_path, backup_file):
    backup_file.with_name(backup_file.name + ".sha256").unlink()
    target = tmp_path / "scratch" / "restored.db"
    target.parent.mkdir()
    with pytest.raises(RestoreRefused):
        restore(backup_file, target)


def test_production_database_needs_an_explicit_flag(tmp_path, backup_file, monkeypatch):
    target = tmp_path / "scratch" / "prod.db"
    target.parent.mkdir()
    monkeypatch.setattr(
        restore_sqlite, "classify_path", lambda _p: restore_sqlite.DatabaseKind.PRODUCTION_RUNTIME
    )
    with pytest.raises(RestoreRefused, match="production"):
        restore(backup_file, target)
    assert not target.exists()
    restore(backup_file, target, allow_production=True)
    assert _count(target) == 50


def test_missing_target_directory_is_refused(tmp_path, backup_file):
    with pytest.raises(RestoreRefused, match="directory"):
        restore(backup_file, tmp_path / "nowhere" / "restored.db")


def test_cli_exit_codes_and_no_partial_file_left(tmp_path, backup_file, capsys):
    target = tmp_path / "scratch" / "restored.db"
    target.parent.mkdir()
    args = ["--backup", str(backup_file), "--target", str(target)]
    assert restore_sqlite.main(args) == 0
    assert restore_sqlite.main(args) == 2  # target now exists
    assert "REFUSED" in capsys.readouterr().err
    assert sorted(p.name for p in target.parent.iterdir()) == ["restored.db"]
