"""Stage 17A: explicit, fail-closed selection of the database a process uses.

`.env` has historically pointed `DATABASE_URL` at a pilot database
(`stage11_pilot.db`) while the operator's real data lives in the runtime
database (`runtime/job_triage_runtime.db`). A shell that forgot the
`DATABASE_URL` override therefore silently ran against the pilot. This
module removes that implicit switch:

- every database URL is classified into a `DatabaseKind` (pure: never
  connects, never opens the file);
- `DATABASE_ROLE` (optional) states what the process is for -- `production`,
  `pilot`, `test` or `development` -- and each role accepts only the kinds
  that fit it;
- without a role, a PILOT or BACKUP database is refused: using one requires
  the explicit `DATABASE_ROLE=pilot`. A backup is never a live database.

`check_database_target` is called once at web/scheduler startup, before any
session is used. Error messages name the kind and the file name, never the
URL itself (a PostgreSQL URL can carry a password).
"""

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_RUNTIME_DB = PROJECT_ROOT / "runtime" / "job_triage_runtime.db"
# Historical pilot databases live in the project root or runtime/ and are
# named `stage<N>..._<...>.db` (stage10_pilot.db, stage11_pilot.db,
# stage14b_canonical_v12.db, runtime/stage16_pilot_scratch.db).
_PILOT_DIRS = (PROJECT_ROOT, PROJECT_ROOT / "runtime")
_PILOT_NAME = re.compile(r"^stage\d+[a-z]?_[\w.-]*\.db$", re.IGNORECASE)
_BACKUP_NAME = re.compile(r"\.pre-|\.bak$|backup", re.IGNORECASE)
_BACKUP_DIR = "job-triage-backups"


class DatabaseKind(StrEnum):
    PRODUCTION_RUNTIME = "PRODUCTION_RUNTIME"
    PILOT = "PILOT"
    BACKUP = "BACKUP"
    OTHER_SQLITE = "OTHER_SQLITE"
    MEMORY = "MEMORY"
    SERVER = "SERVER"  # PostgreSQL (compose production, CI test databases)
    INVALID = "INVALID"


class DatabaseRole(StrEnum):
    PRODUCTION = "production"
    PILOT = "pilot"
    TEST = "test"
    DEVELOPMENT = "development"


_ALLOWED: dict[DatabaseRole | None, frozenset[DatabaseKind]] = {
    DatabaseRole.PRODUCTION: frozenset({DatabaseKind.PRODUCTION_RUNTIME, DatabaseKind.SERVER}),
    DatabaseRole.PILOT: frozenset({DatabaseKind.PILOT}),
    DatabaseRole.TEST: frozenset(
        {DatabaseKind.OTHER_SQLITE, DatabaseKind.MEMORY, DatabaseKind.SERVER}
    ),
    DatabaseRole.DEVELOPMENT: frozenset(
        {DatabaseKind.OTHER_SQLITE, DatabaseKind.MEMORY, DatabaseKind.SERVER}
    ),
    None: frozenset(
        {
            DatabaseKind.PRODUCTION_RUNTIME,
            DatabaseKind.OTHER_SQLITE,
            DatabaseKind.MEMORY,
            DatabaseKind.SERVER,
        }
    ),
}


class DatabaseTargetError(ValueError):
    """The configured database does not fit the process's role."""


@dataclass(frozen=True)
class DatabaseTarget:
    kind: DatabaseKind
    role: DatabaseRole | None
    # File name only (never a full URL); None for server/memory databases.
    file_name: str | None

    def describe(self) -> str:
        role = self.role.value if self.role else "unset"
        name = f" file={self.file_name}" if self.file_name else ""
        return f"database_kind={self.kind.value} database_role={role}{name}"


def _identity(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _sqlite_path(database: str) -> Path | None:
    """The filesystem path a SQLite `database` part names, or None for an
    in-memory database. Relative paths resolve against the working
    directory, exactly as SQLite opens them."""
    if database.startswith("file:"):
        database = database[len("file:") :].split("?", 1)[0]
        if database.startswith("/") and re.match(r"^/[A-Za-z]:", database):
            database = database[1:]
    if not database or database == ":memory:" or "mode=memory" in database:
        return None
    return Path(database)


def classify_path(path: Path) -> DatabaseKind:
    identity = _identity(path)
    if identity == _identity(PRODUCTION_RUNTIME_DB):
        return DatabaseKind.PRODUCTION_RUNTIME
    resolved = Path(os.path.realpath(os.path.abspath(path)))
    if _BACKUP_DIR in {part.lower() for part in resolved.parts} or _BACKUP_NAME.search(
        resolved.name
    ):
        return DatabaseKind.BACKUP
    if _PILOT_NAME.match(resolved.name) and any(
        _identity(resolved.parent) == _identity(directory) for directory in _PILOT_DIRS
    ):
        return DatabaseKind.PILOT
    return DatabaseKind.OTHER_SQLITE


def classify_database_url(database_url: str) -> tuple[DatabaseKind, str | None]:
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        return DatabaseKind.INVALID, None
    backend = url.get_backend_name()
    if backend == "postgresql":
        return DatabaseKind.SERVER, None
    if backend != "sqlite":
        return DatabaseKind.INVALID, None
    path = _sqlite_path(url.database or "")
    if path is None:
        return DatabaseKind.MEMORY, None
    return classify_path(path), path.name


def parse_role(raw: str | None) -> DatabaseRole | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    try:
        return DatabaseRole(value)
    except ValueError:
        allowed = ", ".join(role.value for role in DatabaseRole)
        raise DatabaseTargetError(f"DATABASE_ROLE must be one of: {allowed}") from None


def resolve_database_target(database_url: str, database_role: str | None) -> DatabaseTarget:
    role = parse_role(database_role)
    kind, file_name = classify_database_url(database_url)
    return DatabaseTarget(kind=kind, role=role, file_name=file_name)


def check_database_target(settings) -> DatabaseTarget:
    """Fail closed unless `settings.database_url` fits `settings.database_role`."""
    target = resolve_database_target(settings.database_url, settings.database_role)
    if target.kind is DatabaseKind.INVALID:
        raise DatabaseTargetError("DATABASE_URL is not a supported SQLite/PostgreSQL URL")
    if target.kind not in _ALLOWED[target.role]:
        hint = (
            " A pilot database needs the explicit DATABASE_ROLE=pilot."
            if target.kind is DatabaseKind.PILOT
            else ""
        )
        raise DatabaseTargetError(
            f"refusing {target.describe()}: this database kind is not allowed for the "
            f"process role.{hint} The production database is {PRODUCTION_RUNTIME_DB.name} "
            "in runtime/ (set DATABASE_URL explicitly, see docs/DAILY_OPERATIONS.md)."
        )
    return target
