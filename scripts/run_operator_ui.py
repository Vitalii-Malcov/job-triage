"""Start the read-only operator UI (Stage 20) on 127.0.0.1 with one command.

    .\\.venv\\Scripts\\python.exe scripts\\run_operator_ui.py `
        --database runtime/job_triage_runtime.db

then open http://127.0.0.1:8000/ui/ and log in with API_KEY.

What this launcher forces for the server process it starts (nothing is
written to `.env` or to the database):

- `UI_ENABLED=true`, bound to 127.0.0.1 only (no other host is accepted);
- the Telegram bot off (`TELEGRAM_BOT_TOKEN=""`), Alembic auto-upgrade off,
  every sending / draft / operator-control / scheduler flag off;
- `DATABASE_URL` = the database given on the command line. A missing file
  is refused (SQLite would otherwise create an empty one).

`API_KEY` is read from the environment or `.env` as usual and is never
printed. Before the server starts and after it stops (Ctrl+C) the launcher
prints a read-only fingerprint of the database -- `PRAGMA quick_check`, row
counts per table, SHA-256 of the logical content -- and says whether
anything changed.
`--snapshot DIR` first copies the database with SQLite's backup API.

Exit codes: 0 unchanged, 2 refused, 3 the server failed (e.g. port in use),
5 the database changed while the UI ran (possible when another process --
scheduler, tracker CLI -- wrote to it).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HOST = "127.0.0.1"

# Forced for the server process only. The UI itself is read-only; these keep
# the rest of the web app (lifespan, flags) from acting on the operator's
# behalf while it runs.
FORCED_ENV = {
    "UI_ENABLED": "true",
    # POSTGRES_HOST would rebuild DATABASE_URL as PostgreSQL (Codex final review)
    "POSTGRES_HOST": "",
    "TELEGRAM_BOT_TOKEN": "",
    "ALEMBIC_AUTO_UPGRADE": "false",
    "OUTBOUND_SENDING_ENABLED": "false",
    "TELEGRAM_GMAIL_DRAFT_ENABLED": "false",
    "TELEGRAM_OPERATOR_CONTROL_ENABLED": "false",
    "TELEGRAM_BEWERBUNG_APPROVAL_ENABLED": "false",
    "TELEGRAM_BEWERBUNG_DRAFT_ENABLED": "false",
    "TELEGRAM_VACANCY_FEED_ENABLED": "false",
    "TELEGRAM_DAILY_DIGEST_ENABLED": "false",
    "AUTOMATION_SCHEDULER_ENABLED": "false",
    "COMPANY_RESEARCH_AUTO_ENABLED": "false",
}


def server_environment(base: dict[str, str], database: Path) -> dict[str, str]:
    env = dict(base)
    env.update(FORCED_ENV)
    env["DATABASE_URL"] = f"sqlite:///{database.resolve().as_posix()}"
    return env


def fingerprint(database: Path) -> dict:
    """Read-only (`mode=ro`) integrity check, row counts and a hash of the
    logical content (`iterdump`). The content hash -- not the file bytes --
    also sees committed changes still in the WAL and updates that keep the
    row counts (Codex final review)."""
    uri = database.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        digest = hashlib.sha256()
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8") + bytes([10]))
    finally:
        conn.close()
    return {"quick_check": check, "counts": counts, "sha256": digest.hexdigest()}


def snapshot(database: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{database.stem}.pre-ui-{stamp}{database.suffix}"
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    return target


def _summary(label: str, fp: dict) -> None:
    rows = sum(fp["counts"].values())
    print(
        f"{label}: quick_check={fp['quick_check']} tables={len(fp['counts'])} "
        f"rows={rows} sha256={fp['sha256'][:16]}"
    )


def compare(before: dict, after: dict) -> list[str]:
    changes = []
    if before["quick_check"] != after["quick_check"]:
        changes.append(f"quick_check {before['quick_check']} -> {after['quick_check']}")
    for table in sorted(set(before["counts"]) | set(after["counts"])):
        old, new = before["counts"].get(table), after["counts"].get(table)
        if old != new:
            changes.append(f"{table}: {old} -> {new} rows")
    if before["sha256"] != after["sha256"]:
        changes.append("content changed (sha256 of the logical dump)")
    return changes


def _resolved_settings(env: dict[str, str]):
    """The settings the server process will see (environment + `.env`)."""
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        from app.core.config import Settings

        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def main(argv: list[str] | None = None, *, run=subprocess.call) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database", required=True, type=Path, help="SQLite database file")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--snapshot", type=Path, help="copy the database here first (backup API)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # "before" prints ahead of uvicorn's log

    database: Path = args.database
    if not database.is_file():
        print(f"REFUSED: no database file at {database}", file=sys.stderr)
        return 2
    env = server_environment(dict(os.environ), database)
    settings = _resolved_settings(env)
    if settings.database_url != env["DATABASE_URL"]:
        print("REFUSED: the configuration resolves to a different database", file=sys.stderr)
        return 2
    if not settings.api_key:
        print(
            "REFUSED: API_KEY is not set (environment or .env); the UI login needs it",
            file=sys.stderr,
        )
        return 2

    before = fingerprint(database)
    _summary("before", before)
    if before["quick_check"] != "ok":
        print("REFUSED: the database fails PRAGMA quick_check", file=sys.stderr)
        return 2
    if args.snapshot:
        print(f"snapshot: {snapshot(database, args.snapshot)}")

    print(f"Open http://{HOST}:{args.port}/ui/ and log in with API_KEY. Ctrl+C stops the server.")
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        HOST,
        "--port",
        str(args.port),
    ]
    status = 0
    try:
        status = run(command, env=env)
    except KeyboardInterrupt:  # Ctrl+C is the normal way to stop
        pass

    after = fingerprint(database)
    _summary("after ", after)
    changes = compare(before, after)
    if changes:
        print("DATABASE CHANGED while the UI ran:")
        for change in changes:
            print(f"  {change}")
        return 5
    print("UNCHANGED: same row counts, integrity and content as before")
    if status:
        print(f"SERVER FAILED: uvicorn exited with status {status} (port in use?)")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
