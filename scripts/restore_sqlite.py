"""Verified restore of a backup made by scripts/backup_sqlite.py (audit OPS-03).

    python -m scripts.restore_sqlite --backup <dir>/job_triage-<stamp>.db --target <new db path>
    python -m scripts.restore_sqlite --backup ... --target ... --replace   # overwrite a stopped db

* The backup must be one of this tool family's own files: exact name grammar, regular file,
  and a sidecar whose SHA-256 equals the file. A bad or missing sidecar is refused, never "fixed".
* The backup is opened read-only and must pass ``PRAGMA integrity_check``.
* The target is never overwritten silently. Without ``--replace`` an existing target is refused.
  With ``--replace`` the current file is first copied next to it as
  ``<name>.pre-restore-<stamp>`` (created exclusively; nothing is deleted) and a target with a
  live ``-wal``/``-shm`` sidecar (the application is still running) is refused.
* The production runtime database is refused unless ``--allow-production`` is given. Rehearse on
  a scratch path first: every automated test restores to a temporary file only.
* The restored file is written to a temp file in the target directory and moved into place with
  one atomic replace; its integrity and key table counts are re-checked against the backup.

Exit 0 = restored and verified, 1 = failed (target untouched), 2 = refused input.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.database_target import DatabaseKind, classify_path
from scripts.backup_sqlite import _counts, _owned_backup, _readonly_uri


class RestoreRefused(Exception):
    """The request itself is unacceptable (exit 2)."""


def _inspect(path: Path) -> tuple[str, dict[str, int]]:
    connection = sqlite3.connect(_readonly_uri(path), uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0], _counts(connection)
    finally:
        connection.close()


def restore(
    backup: Path,
    target: Path,
    *,
    replace: bool = False,
    allow_production: bool = False,
    now: datetime | None = None,
) -> Path | None:
    """Restore `backup` to `target`. Returns the pre-restore copy's path when one was made."""
    if str(backup).startswith(("\\\\", "//")) or str(target).startswith(("\\\\", "//")):
        raise RestoreRefused("network (UNC) paths are not supported")
    if not _owned_backup(backup):
        raise RestoreRefused("not a verified backup (name, sidecar or digest does not match)")
    if backup.resolve() == target.resolve():
        raise RestoreRefused("the backup cannot be its own target")
    if classify_path(target) is DatabaseKind.PRODUCTION_RUNTIME and not allow_production:
        raise RestoreRefused("target is the production database; pass --allow-production")
    if not target.parent.is_dir():
        raise RestoreRefused("target directory does not exist")
    if target.is_symlink():
        raise RestoreRefused("target is a symbolic link")
    integrity, expected = _inspect(backup)
    if integrity != "ok":
        raise RestoreRefused(f"backup failed its integrity check ({integrity})")

    pre_restore: Path | None = None
    if target.exists():
        if not replace:
            raise RestoreRefused("target exists; pass --replace (a pre-restore copy is kept)")
        for suffix in ("-wal", "-shm", "-journal"):
            if target.with_name(target.name + suffix).exists():
                raise RestoreRefused("target looks in use (-wal/-shm/-journal present); stop it")
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
        pre_restore = target.with_name(f"{target.name}.pre-restore-{stamp}")
        with target.open("rb") as source, pre_restore.open("xb") as copy:
            shutil.copyfileobj(source, copy)

    handle, partial_name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=target.parent)
    os.close(handle)
    partial = Path(partial_name)
    try:
        shutil.copyfile(backup, partial)
        integrity, counts = _inspect(partial)
        if integrity != "ok" or counts != expected:
            raise RuntimeError(f"restored copy failed verification (integrity={integrity})")
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return pre_restore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    args = parser.parse_args(argv)
    try:
        pre_restore = restore(
            args.backup, args.target, replace=args.replace, allow_production=args.allow_production
        )
    except RestoreRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"OK restored {args.backup.name} -> {args.target.name}")
    if pre_restore is not None:
        print(f"previous file kept as {pre_restore.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
