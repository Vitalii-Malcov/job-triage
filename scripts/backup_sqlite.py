"""Consistent, verified backup of a SQLite JobTriage database (audit DB-02).

    python scripts/backup_sqlite.py --db runtime/job_triage_runtime.db \
        --dest C:/Users/<you>/JobTriage-Backups [--label pre-deploy] [--keep 30]

* The source is opened read-only (an escaped ``file:`` URI, so a ``#``/``%``/``?`` in the
  path cannot change the open mode) and copied through SQLite's online backup API inside ONE
  read transaction: the row counts that are checked afterwards are taken from the very same
  snapshot as the copy, so a commit by the running app between the two cannot cause a false
  failure or a torn copy. The source file is never written. Like any reader it takes SHARED
  locks for the duration (rollback-journal mode: a writer waits for the copy's pages; WAL
  mode: a checkpoint may be delayed) -- the copy is done in small steps with a deadline
  (``--timeout``, default 120 s) and fails cleanly instead of waiting forever.
* The destination must be OUTSIDE this repository (the database holds personal data and the
  application history; it must never end up in Git). Files are created exclusively
  (temp file in the destination -> hard link to the final name; sidecar with ``x``), so
  nothing existing is overwritten, a pre-planted symlink is never followed and two runs in the
  same second cannot clobber each other.
* The copy is verified before it is published: ``PRAGMA integrity_check`` must be ``ok`` and the
  key table counts must equal the snapshot's. A SHA-256 sidecar (``<name>.sha256``) is written.
  Any failure removes every artifact this run created.
* ``--keep N`` prunes only files that match this tool's exact name grammar, are regular files
  and whose sidecar holds the file's own digest; everything else in the folder is untouched.

Exit 0 = backup verified, 1 = failed (nothing left behind), 2 = refused input.
Restore = stop the app, check the sidecar digest, copy the file over the database path.
"""

import argparse
import contextlib
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY_TABLES = ("jobs", "applications", "application_events", "gmail_messages")
NAME_PREFIX = "job_triage-"
NAME_PATTERN = re.compile(r"job_triage-\d{8}T\d{6}Z(?:-[A-Za-z0-9_-]+)?\.db")
SIDECAR_PATTERN = re.compile(r"([0-9a-f]{64})  (job_triage-[^\s/\\]+\.db)\n")
PAGES_PER_STEP = 256
DEFAULT_TIMEOUT_SECONDS = 120.0


class BackupRefused(Exception):
    """The request itself is unacceptable (exit 2)."""


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    existing = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in KEY_TABLES
        if table in existing
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_snapshot(db: Path, partial: Path, timeout: float) -> dict[str, int]:
    deadline = time.monotonic() + timeout

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError(f"backup did not finish within {timeout:.0f} s")

    source = sqlite3.connect(_readonly_uri(db), uri=True, timeout=min(timeout, 10.0))
    try:
        source.isolation_level = None  # explicit transaction control
        source.execute("BEGIN")  # one read snapshot for the counts AND the page copy
        try:
            snapshot_counts = _counts(source)
            copy = sqlite3.connect(partial)
            try:
                source.backup(copy, pages=PAGES_PER_STEP, progress=progress, sleep=0.01)
                # a WAL source would leave -wal/-shm side files next to the copy; the backup
                # must be one self-contained file
                copy.execute("PRAGMA journal_mode=DELETE")
            finally:
                copy.close()
        finally:
            with contextlib.suppress(sqlite3.Error):
                source.execute("COMMIT")
        return snapshot_counts
    finally:
        source.close()


def backup(
    db: Path,
    dest: Path,
    *,
    label: str = "",
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    if str(db).startswith(("\\\\", "//")) or str(dest).startswith(("\\\\", "//")):
        raise BackupRefused("network (UNC) paths are not supported")
    if not db.is_file():
        raise BackupRefused("source database does not exist")
    if _is_inside(dest, REPO_ROOT):
        raise BackupRefused(
            "destination is inside the repository (personal data must not be in Git)"
        )
    if dest.resolve() == db.resolve().parent:
        raise BackupRefused("destination is the database's own directory")
    if label and not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        raise BackupRefused("label may only contain letters, digits, '-' and '_'")
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    final = dest / f"{NAME_PREFIX}{stamp}{'-' + label if label else ''}.db"
    sidecar = dest / f"{final.name}.sha256"
    if final.exists() or final.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise BackupRefused("destination file already exists; nothing is overwritten")

    handle, partial_name = tempfile.mkstemp(prefix=".partial-", suffix=".db", dir=dest)
    os.close(handle)
    partial = Path(partial_name)
    published = False
    sidecar_created = False
    try:
        snapshot_counts = _copy_snapshot(db, partial, timeout)
        verify = sqlite3.connect(partial.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
            copy_counts = _counts(verify)
        finally:
            verify.close()
        if integrity != "ok" or copy_counts != snapshot_counts:
            raise RuntimeError(f"verification failed (integrity={integrity})")
        digest = _sha256(partial)
        os.link(partial, final)  # fails if `final` appeared meanwhile: never replaces
        published = True
        with sidecar.open("x", encoding="utf-8") as out:
            sidecar_created = True  # opened exclusively by THIS run: ours to remove on failure
            out.write(f"{digest}  {final.name}\n")
    except BaseException:
        if published:
            final.unlink(missing_ok=True)
        if sidecar_created:
            sidecar.unlink(missing_ok=True)
        raise
    finally:
        for leftover in (partial, *(partial.with_name(partial.name + s) for s in ("-wal", "-shm"))):
            leftover.unlink(missing_ok=True)
    return final


def _owned_backup(path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or not NAME_PATTERN.fullmatch(path.name):
        return False
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        return False
    try:
        match = SIDECAR_PATTERN.fullmatch(sidecar.read_text(encoding="utf-8"))
        return bool(match) and match.group(2) == path.name and match.group(1) == _sha256(path)
    except (OSError, UnicodeDecodeError):
        return False  # unreadable / not ours: never a reason to abort retention


def prune(dest: Path, keep: int) -> list[Path]:
    """Delete the oldest backups created by this tool beyond `keep` (and their sidecars)."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    mine = sorted(p for p in dest.glob(f"{NAME_PREFIX}*.db") if _owned_backup(p))
    removed = mine[:-keep] if len(mine) > keep else []
    for path in removed:
        path.unlink()
        path.with_name(path.name + ".sha256").unlink(missing_ok=True)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--label", default="")
    parser.add_argument("--keep", type=int, default=0, help="keep only the newest N backups")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if args.keep < 0 or not (0 < args.timeout < float("inf")):
        print("REFUSED: --keep must be >= 0 and --timeout a finite number > 0", file=sys.stderr)
        return 2
    try:
        target = backup(args.db, args.dest, label=args.label, timeout=args.timeout)
    except BackupRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, TimeoutError, OSError, sqlite3.Error) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"OK {target.name} sha256={_sha256(target)} bytes={target.stat().st_size}")
    if args.keep > 0:
        for removed in prune(args.dest.resolve(), args.keep):
            print(f"pruned {removed.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
