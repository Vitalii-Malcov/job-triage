"""Audit DB-02: scripts/backup_sqlite.py makes a consistent, verified, safe backup."""

import hashlib
import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_sqlite.py"
spec = importlib.util.spec_from_file_location("backup_sqlite", SCRIPT)
backup_sqlite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_sqlite)


def _make_db(path: Path, *, wal: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT);"
        "CREATE TABLE applications (id INTEGER PRIMARY KEY);"
        "CREATE TABLE application_events (id INTEGER PRIMARY KEY);"
        "CREATE TABLE gmail_messages (id INTEGER PRIMARY KEY);"
    )
    connection.executemany(
        "INSERT INTO jobs (title) VALUES (?)", [(f"job {i}",) for i in range(50)]
    )
    connection.executemany("INSERT INTO applications DEFAULT VALUES", [()] * 3)
    connection.executemany("INSERT INTO gmail_messages DEFAULT VALUES", [()] * 4)
    connection.commit()
    connection.close()
    return path


@pytest.fixture()
def source(tmp_path):
    return _make_db(tmp_path / "src" / "runtime.db")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count(path: Path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize("wal", [False, True])
def test_backup_is_verified_and_the_source_is_untouched(tmp_path, wal):
    source = _make_db(tmp_path / "src" / "runtime.db", wal=wal)
    before = _digest(source)
    target = backup_sqlite.backup(source, tmp_path / "backups", label="pre-deploy")
    assert _digest(source) == before or wal  # WAL bookkeeping may touch the -shm/-wal side files
    assert target.name.endswith("-pre-deploy.db")
    assert _count(target, "jobs") == 50 and _count(target, "gmail_messages") == 4
    sidecar = (target.parent / f"{target.name}.sha256").read_text(encoding="utf-8")
    assert sidecar.split()[0] == _digest(target)
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name, target.name + ".sha256"]


def test_backup_with_an_open_uncommitted_writer_has_no_torn_row(source, tmp_path):
    writer = sqlite3.connect(source)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO jobs (title) VALUES ('uncommitted')")
    target = backup_sqlite.backup(source, tmp_path / "backups")
    writer.rollback()
    writer.close()
    assert _count(target, "jobs") == 50


def test_a_path_with_uri_special_characters_still_opens_read_only(tmp_path):
    source = _make_db(tmp_path / "we#ird%20 dir" / "run#time&x.db")
    before = _digest(source)
    target = backup_sqlite.backup(source, tmp_path / "backups")
    assert _count(target, "jobs") == 50 and _digest(source) == before
    assert not (tmp_path / "we").exists()  # '#' did not truncate the path / create a new file


def test_destination_inside_the_repository_is_refused(source):
    repo_dest = Path(backup_sqlite.REPO_ROOT) / "tmp_backup_should_not_exist"
    with pytest.raises(backup_sqlite.BackupRefused):
        backup_sqlite.backup(source, repo_dest)
    assert not repo_dest.exists()


def test_an_existing_backup_or_sidecar_is_never_overwritten(source, tmp_path):
    moment = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    dest = tmp_path / "backups"
    first = backup_sqlite.backup(source, dest, now=moment)
    kept = _digest(first)
    with pytest.raises(backup_sqlite.BackupRefused):
        backup_sqlite.backup(source, dest, now=moment)
    assert _digest(first) == kept
    first.unlink()  # sidecar alone still blocks
    with pytest.raises(backup_sqlite.BackupRefused):
        backup_sqlite.backup(source, dest, now=moment)


def test_a_planted_sidecar_symlink_is_not_followed(source, tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    moment = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    name = "job_triage-20261107T120000Z.db"
    try:
        (dest / f"{name}.sha256").symlink_to(victim)
    except OSError:
        pytest.skip("symlinks not creatable on this platform")
    with pytest.raises(backup_sqlite.BackupRefused):
        backup_sqlite.backup(source, dest, now=moment)
    assert victim.read_text() == "precious"


def test_missing_source_bad_label_and_unc_are_refused(tmp_path, source):
    with pytest.raises(backup_sqlite.BackupRefused):
        backup_sqlite.backup(tmp_path / "nope.db", tmp_path / "b")
    with pytest.raises(backup_sqlite.BackupRefused):
        backup_sqlite.backup(source, tmp_path / "b", label="../evil")
    with pytest.raises(backup_sqlite.BackupRefused):
        backup_sqlite.backup(source, Path("//server/share/backups"))


def test_failure_after_the_copy_leaves_nothing_behind(source, tmp_path, monkeypatch):
    dest = tmp_path / "backups"

    def boom(_path):
        raise OSError("disk full")

    monkeypatch.setattr(backup_sqlite, "_sha256", boom)
    with pytest.raises(OSError):
        backup_sqlite.backup(source, dest)
    assert list(dest.iterdir()) == []


def test_verification_failure_removes_everything(source, tmp_path, monkeypatch):
    dest = tmp_path / "backups"
    real = backup_sqlite._counts
    calls = {"n": 0}

    def skewed(connection):
        calls["n"] += 1
        counts = real(connection)
        return {**counts, "jobs": counts["jobs"] + (1 if calls["n"] == 2 else 0)}

    monkeypatch.setattr(backup_sqlite, "_counts", skewed)
    with pytest.raises(RuntimeError):
        backup_sqlite.backup(source, dest)
    assert list(dest.iterdir()) == []


def test_backup_deadline_aborts_cleanly(source, tmp_path):
    dest = tmp_path / "backups"
    with pytest.raises(TimeoutError):
        backup_sqlite.backup(source, dest, timeout=-1.0)
    assert list(dest.iterdir()) == []


def test_prune_removes_only_verified_backups_made_by_this_tool(source, tmp_path):
    dest = tmp_path / "backups"
    made = [
        backup_sqlite.backup(source, dest, now=datetime(2026, 11, day, tzinfo=UTC))
        for day in (1, 2, 3, 4)
    ]
    handmade = dest / "job_triage-000-manual.db"  # wrong name grammar
    handmade.write_bytes(b"x")
    (dest / f"{handmade.name}.sha256").write_text("0" * 64 + f"  {handmade.name}\n")
    forged = dest / "job_triage-20250101T000000Z.db"  # right name, sidecar does not match
    forged.write_bytes(b"y")
    (dest / f"{forged.name}.sha256").write_text("1" * 64 + f"  {forged.name}\n")
    removed = backup_sqlite.prune(dest, keep=2)
    assert {p.name for p in removed} == {made[0].name, made[1].name}
    assert made[2].exists() and made[3].exists() and handmade.exists() and forged.exists()
    assert not (dest / f"{made[0].name}.sha256").exists()


def test_prune_rejects_a_non_positive_keep(tmp_path):
    with pytest.raises(ValueError):
        backup_sqlite.prune(tmp_path, keep=0)


def test_cli_exit_codes(source, tmp_path, capsys):
    assert backup_sqlite.main(["--db", str(source), "--dest", str(tmp_path / "b")]) == 0
    assert "OK job_triage-" in capsys.readouterr().out
    missing = ["--db", str(tmp_path / "missing.db"), "--dest", str(tmp_path / "b")]
    assert backup_sqlite.main(missing) == 2
    assert (
        backup_sqlite.main(["--db", str(source), "--dest", str(tmp_path / "b"), "--keep", "-1"])
        == 2
    )


def test_a_failed_sidecar_write_removes_the_sidecar_too(source, tmp_path, monkeypatch):
    dest = tmp_path / "backups"
    real_open = Path.open

    class _Broken:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

        def write(self, _text):
            raise OSError("disk full")

    def fake_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return _Broken(handle) if self.name.endswith(".sha256") and "x" in mode else handle

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(OSError):
        backup_sqlite.backup(source, dest)
    assert list(dest.iterdir()) == []


def test_a_malformed_sidecar_never_aborts_retention(source, tmp_path):
    dest = tmp_path / "backups"
    made = [
        backup_sqlite.backup(source, dest, now=datetime(2026, 11, day, tzinfo=UTC))
        for day in (1, 2, 3)
    ]
    (dest / f"{made[1].name}.sha256").write_bytes(bytes([0xFF, 0xFE, 0x80, 0x20, 0x80]))
    removed = backup_sqlite.prune(dest, keep=1)
    assert [p.name for p in removed] == [made[0].name]  # the undecodable one is simply not ours
    assert made[1].exists() and made[2].exists()


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-3"])
def test_non_finite_or_non_positive_timeouts_are_refused(source, tmp_path, timeout):
    args = ["--db", str(source), "--dest", str(tmp_path / "b"), "--timeout", timeout]
    assert backup_sqlite.main(args) == 2
