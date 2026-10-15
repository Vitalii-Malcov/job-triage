"""Stage 14A: print profile readiness + READY_FOR_SEND_PACKAGE for one job.

READ-ONLY, ZERO NETWORK. Never commits, never sends, never touches
Telegram/Gmail/SMTP. The database URL is restricted to a LOCAL, READ-ONLY
SQLite URI of exactly this shape, validated before any engine is created:

    sqlite:///file:<local path>?mode=ro&uri=true

Every other form is rejected without connecting: any non-SQLite dialect
(postgresql, mysql, ...), a sqlite driver suffix, a host/credentials, a
plain sqlite path without `mode=ro`, in-memory databases, `file://host`
authorities, UNC (`\\\\server\\share`) paths and extra URI parameters
(`vfs`, `cache`, ...). The connection additionally runs `PRAGMA
query_only = ON`. The session is always rolled back. Example:

    python scripts/stage14a_send_readiness.py \
        --database-url "sqlite:///file:stage11_pilot.db?mode=ro&uri=true" \
        --job-id 27 \
        --destination-json docs/research/stage14a_e362b_destination.json \
        --location-json docs/research/stage14a_e362b_locations.json

`--destination-json` holds one `ApplicationDestination` object and
`--location-json` a list of `LocationEvidence` objects (see
app/models/application_destination.py and app/services/send_readiness.py).
Both are operator research artifacts; omitting them yields UNKNOWN /
"no authoritative evidence", i.e. NOT ready. Both must be LOCAL filesystem
paths, validated before any read: UNC (`\\\\server\\share`), `//server`,
Win32 device namespaces (`\\\\?\\`, `\\\\.\\`) and any URI scheme
(`file:`, `http:`, `smb:`, ...) are refused.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from app.agents.profile_readiness import render_profile_readiness
from app.core.config import Settings
from app.db.models import JobRecord
from app.models.application_destination import ApplicationDestination
from app.services.send_readiness import (
    LocationEvidence,
    evaluate_send_readiness,
    load_send_readiness_inputs,
    render_send_readiness,
)

_PREFIX = "sqlite:///file:"
_REQUIRED_QUERY = {"mode": "ro", "uri": "true"}


# A URI scheme of two or more characters; a single letter is a drive (C:).
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")


class UnsafeDatabaseUrlError(ValueError):
    """The URL is not a local read-only SQLite URI."""


class UnsafeInputPathError(ValueError):
    """The JSON input path is not a plain local filesystem path."""


def _is_network_or_device_path(path: str) -> bool:
    return path[:2] in ("//", "\\\\", "/\\", "\\/") or path.startswith("\\??\\")


def validate_local_json_path(raw: str) -> str:
    """Return `raw` unchanged if it is a plain local filesystem path, else
    raise `UnsafeInputPathError`. Pure string parsing: never touches the
    filesystem or the network."""
    if not raw or not raw.strip():
        raise UnsafeInputPathError("input path is empty")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise UnsafeInputPathError("input path contains control characters")
    if _URI_SCHEME_RE.match(raw):
        raise UnsafeInputPathError("input path must not be a URI (file:, http:, smb:, ...)")
    if _is_network_or_device_path(raw):
        raise UnsafeInputPathError("UNC, network and device-namespace paths are not allowed")
    # A relative path under a UNC working directory is a network path too.
    drive, _ = os.path.splitdrive(os.path.abspath(raw))
    if _is_network_or_device_path(drive or os.path.abspath(raw)):
        raise UnsafeInputPathError("input path resolves to a network location")
    return raw


def validate_readonly_sqlite_url(raw: str) -> str:
    """Return `raw` unchanged if it is a local read-only SQLite URI, else
    raise `UnsafeDatabaseUrlError`. Pure string parsing: never connects."""
    if not raw.startswith(_PREFIX):
        raise UnsafeDatabaseUrlError(f"database URL must start with {_PREFIX!r}")
    if any(ch in raw for ch in ("\r", "\n", "\x00", "#")):
        raise UnsafeDatabaseUrlError("database URL contains forbidden characters")
    try:
        url = make_url(raw)
    except ArgumentError as exc:
        raise UnsafeDatabaseUrlError(f"unparseable database URL: {exc}") from None
    if url.drivername != "sqlite":
        raise UnsafeDatabaseUrlError("only the plain 'sqlite' dialect is allowed")
    if url.host or url.username or url.password or url.port:
        raise UnsafeDatabaseUrlError("database URL must not carry a host or credentials")
    query = {key: value for key, value in url.query.items()}
    if query != _REQUIRED_QUERY:
        raise UnsafeDatabaseUrlError("database URL query must be exactly 'mode=ro&uri=true'")
    database = url.database or ""
    if not database.startswith("file:"):
        raise UnsafeDatabaseUrlError("database must be a 'file:' URI")
    path = database[len("file:") :]
    if not path:
        raise UnsafeDatabaseUrlError("database file path is empty")
    if path[:2] in ("//", "\\\\", "/\\", "\\/"):
        raise UnsafeDatabaseUrlError("UNC paths and file:// authorities are not allowed")
    if ":memory:" in path or "?" in path or "%" in path:
        raise UnsafeDatabaseUrlError("only a plain local database file path is allowed")
    return raw


def _query_only(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA query_only = ON")
    finally:
        cursor.close()


def _load_json(path: str | None):
    if path is None:
        return None
    validate_local_json_path(path)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--destination-json")
    parser.add_argument("--location-json")
    args = parser.parse_args(argv)

    try:
        database_url = validate_readonly_sqlite_url(args.database_url)
    except UnsafeDatabaseUrlError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        for json_path in (args.destination_json, args.location_json):
            if json_path is not None:
                validate_local_json_path(json_path)
    except UnsafeInputPathError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    raw_destination = _load_json(args.destination_json)
    destination = (
        ApplicationDestination.model_validate(raw_destination) if raw_destination else None
    )
    evidence = [LocationEvidence.model_validate(e) for e in _load_json(args.location_json) or []]

    engine = create_engine(database_url)
    event.listen(engine, "connect", _query_only)
    try:
        with Session(engine, autoflush=False) as db:
            try:
                job = db.get(JobRecord, args.job_id)
                if job is None:
                    print(f"job {args.job_id} not found", file=sys.stderr)
                    return 2
                inputs = load_send_readiness_inputs(
                    db,
                    job,
                    settings=Settings(),
                    destination=destination,
                    location_evidence=evidence,
                )
                print(render_profile_readiness(inputs.profile_report))
                print()
                result = evaluate_send_readiness(inputs)
                print(render_send_readiness(result))
                return 0 if result.ready else 1
            finally:
                db.rollback()
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
