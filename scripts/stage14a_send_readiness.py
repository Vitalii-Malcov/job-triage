"""Stage 14A: print profile readiness + READY_FOR_SEND_PACKAGE for one job.

READ-ONLY, ZERO NETWORK. Never commits, never sends, never touches
Telegram/Gmail/SMTP. The database URL is restricted to a LOCAL, READ-ONLY
SQLite URI of exactly this shape, validated before any engine is created:

    sqlite:///file:<local path>?mode=ro&uri=true

Every other form is rejected without connecting: any non-SQLite dialect
(postgresql, mysql, ...), a sqlite driver suffix, a host/credentials, a
plain sqlite path without `mode=ro`, in-memory databases, `file://host`
authorities, UNC (`\\\\server\\share`) paths and extra URI parameters
(`vfs`, `cache`, ...). On Windows the database file's drive (resolved
against the working directory) must be a LOCAL volume per `GetDriveTypeW`:
a mapped network drive (e.g. `Z:` -> SMB) or an unclassifiable drive is
refused. The connection additionally runs `PRAGMA
query_only = ON`. The session is always rolled back. Example:

    python scripts/stage14a_send_readiness.py \
        --database-url "sqlite:///file:stage11_pilot.db?mode=ro&uri=true" \
        --job-id 27 \
        --destination-json docs/research/stage14a_e362b_destination.json \
        --location-json docs/research/stage14a_e362b_locations.json

`--identity-json` (Stage 14D) holds the operator-confirmed
`ApplicationIdentity` (contact data); omitting it yields
CONTACT_DATA_MISSING, i.e. NOT ready. Expected failures (an artifact naming
a different vacancy, a missing profile, an invalid input file) print a
BLOCKED or refused line with a reason code and exit nonzero, never a
traceback.

`--destination-json` holds one `ApplicationDestination` object and
`--location-json` a list of `LocationEvidence` objects (see
app/models/application_destination.py and app/services/send_readiness.py).
Both are operator research artifacts; omitting them yields UNKNOWN /
"no authoritative evidence", i.e. NOT ready. Both must be LOCAL filesystem
paths, validated before any read: UNC (`\\\\server\\share`), `//server`,
Win32 device namespaces (`\\\\?\\`, `\\\\.\\`) and any URI scheme
(`file:`, `http:`, `smb:`, ...) are refused, and on Windows the same
local-drive classification applies.
"""

import argparse
import json
import ntpath
import os
import re
import sys
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from app.agents.profile_readiness import render_profile_readiness
from app.core.config import Settings
from app.db.models import JobRecord
from app.models.application_destination import ApplicationDestination
from app.models.application_identity import (
    ApplicationIdentityInvalidError,
    load_application_identity,
)
from app.services.send_readiness import (
    LocationEvidence,
    NoCandidateProfileError,
    VacancyEvidenceMismatchError,
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


class InputInvalidError(ValueError):
    """Stage 14D: an operator input file is unreadable or invalid. The
    message names files/fields only, never values."""


def _is_network_or_device_path(path: str) -> bool:
    return path[:2] in ("//", "\\\\", "/\\", "\\/") or path.startswith("\\??\\")


# GetDriveTypeW results that are a local volume. DRIVE_REMOTE (4),
# DRIVE_UNKNOWN (0) and DRIVE_NO_ROOT_DIR (1) are refused (fail closed).
_DRIVE_REMOVABLE, _DRIVE_FIXED, _DRIVE_REMOTE, _DRIVE_CDROM, _DRIVE_RAMDISK = 2, 3, 4, 5, 6
_LOCAL_DRIVE_TYPES = frozenset({_DRIVE_REMOVABLE, _DRIVE_FIXED, _DRIVE_CDROM, _DRIVE_RAMDISK})
_IS_WINDOWS = os.name == "nt"
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:$")


def _cwd() -> str:
    return os.getcwd()


def _drive_type(root: str) -> int:
    """Win32 `GetDriveTypeW` for a drive root such as `Z:\\`. Classifies the
    volume only; never opens the target file."""
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))


def _is_local_volume(path: str) -> bool:
    """True only if `path` (resolved against the working directory) lies on
    a drive letter that Windows classifies as a local volume. A mapped
    network drive (`Z:` -> SMB), a UNC working directory or any drive whose
    type cannot be determined is NOT local. Off Windows, only the string
    checks apply (there are no drive letters to map)."""
    if not _IS_WINDOWS:
        return True
    try:
        absolute = ntpath.normpath(ntpath.join(_cwd(), path))
        drive = ntpath.splitdrive(absolute)[0]
        if not _DRIVE_LETTER_RE.match(drive):
            return False  # UNC share or device namespace
        return _drive_type(drive + "\\") in _LOCAL_DRIVE_TYPES
    except Exception:  # noqa: BLE001 -- classification failure fails closed
        return False


def validate_local_json_path(raw: str) -> str:
    """Return `raw` unchanged if it is a plain local filesystem path, else
    raise `UnsafeInputPathError`. Never reads the file and never touches
    the network: string checks plus (on Windows) a drive-type
    classification of the resolved drive root."""
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
    if not _is_local_volume(raw):
        raise UnsafeInputPathError("input path is not on a local drive (mapped network drive?)")
    return raw


def validate_readonly_sqlite_url(raw: str) -> str:
    """Return `raw` unchanged if it is a local read-only SQLite URI, else
    raise `UnsafeDatabaseUrlError`. Pure string parsing: never connects."""
    if not raw.startswith(_PREFIX):
        raise UnsafeDatabaseUrlError(f"database URL must start with {_PREFIX!r}")
    if any(ch in raw for ch in ("\r", "\n", "\x00", "#")):
        raise UnsafeDatabaseUrlError("database URL contains forbidden characters")
    # Checked on the RAW string: SQLAlchemy 2.1+ percent-decodes the database
    # part in `make_url` (2.0 keeps it verbatim), so a check on the parsed
    # path alone lets `%2E`/`%2F`-style escapes through on newer versions.
    if "%" in raw:
        raise UnsafeDatabaseUrlError("percent-encoded database URLs are not allowed")
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
    # SQLite reads `file:/C:/x.db` as `C:/x.db`; classify that drive.
    fs_path = path[1:] if re.match(r"^/[A-Za-z]:", path) else path
    if not _is_local_volume(fs_path):
        raise UnsafeDatabaseUrlError(
            "database file is not on a local drive (mapped network drive?)"
        )
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


def _validation_fields(exc: ValidationError) -> str:
    return ", ".join(
        sorted({".".join(str(p) for p in error["loc"]) or "<root>" for error in exc.errors()})
    )


def _load_inputs(args) -> tuple:
    """Parse the operator input files; `InputInvalidError` for any
    unreadable/invalid file (Stage 14D: never a traceback)."""
    try:
        raw_destination = _load_json(args.destination_json)
    except (OSError, ValueError):
        raise InputInvalidError("destination file is unreadable or not JSON") from None
    try:
        destination = (
            ApplicationDestination.model_validate(raw_destination) if raw_destination else None
        )
    except ValidationError as exc:
        raise InputInvalidError(f"destination is invalid: {_validation_fields(exc)}") from None
    try:
        raw_locations = _load_json(args.location_json) or []
    except (OSError, ValueError):
        raise InputInvalidError("location file is unreadable or not JSON") from None
    if not isinstance(raw_locations, list):
        raise InputInvalidError("location file must hold a JSON list")
    try:
        evidence = [LocationEvidence.model_validate(e) for e in raw_locations]
    except ValidationError as exc:
        raise InputInvalidError(
            f"location evidence is invalid: {_validation_fields(exc)}"
        ) from None
    try:
        identity = load_application_identity(
            Path(args.identity_json) if args.identity_json is not None else None
        )
    except ApplicationIdentityInvalidError as exc:
        raise InputInvalidError(str(exc)) from None
    return destination, evidence, identity


def _blocked(code: str, detail: str, job_id: int) -> int:
    print(f"READY_FOR_SEND_PACKAGE = NO (job_id={job_id})")
    print(f"BLOCKED {code}: {detail}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--destination-json")
    parser.add_argument("--location-json")
    parser.add_argument("--identity-json")
    args = parser.parse_args(argv)

    try:
        database_url = validate_readonly_sqlite_url(args.database_url)
    except UnsafeDatabaseUrlError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        for json_path in (args.destination_json, args.location_json, args.identity_json):
            if json_path is not None:
                validate_local_json_path(json_path)
    except UnsafeInputPathError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    try:
        destination, evidence, identity = _load_inputs(args)
    except InputInvalidError as exc:
        print(f"refused: INPUT_INVALID: {exc}", file=sys.stderr)
        return 2

    engine = create_engine(database_url)
    event.listen(engine, "connect", _query_only)
    try:
        with Session(engine, autoflush=False) as db:
            try:
                job = db.get(JobRecord, args.job_id)
                if job is None:
                    print(f"job {args.job_id} not found", file=sys.stderr)
                    return 2
                try:
                    inputs = load_send_readiness_inputs(
                        db,
                        job,
                        settings=Settings(),
                        destination=destination,
                        location_evidence=evidence,
                        application_identity=identity,
                    )
                except VacancyEvidenceMismatchError as exc:
                    return _blocked(exc.code, "research artifact names a different vacancy", job.id)
                except NoCandidateProfileError:
                    return _blocked("NO_CANDIDATE_PROFILE", "no candidate profile exists", job.id)
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
