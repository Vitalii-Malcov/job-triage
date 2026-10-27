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
from urllib.parse import unquote

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
    # A pilot may also live on a PostgreSQL server (database name says so).
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
    if not database or database == ":memory:":
        return None
    return Path(database)


# A PostgreSQL database whose NAME says pilot/backup is treated like the file.
_SERVER_PILOT_NAME = re.compile(r"pilot|stage\d+|scratch", re.IGNORECASE)
_SERVER_BACKUP_NAME = re.compile(r"backup|\bbak\b|_bak|pre[-_]", re.IGNORECASE)


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


# Most sensitive first: an ambiguous spelling is classified as the most
# protected database it could name.
_SENSITIVITY = (
    DatabaseKind.PRODUCTION_RUNTIME,
    DatabaseKind.PILOT,
    DatabaseKind.BACKUP,
    DatabaseKind.OTHER_SQLITE,
)


def _classify_spellings(database: str) -> tuple[DatabaseKind, str | None]:
    """SQLite percent-decodes `file:` URI paths, and SQLAlchemy 2.1+ decodes
    the database part itself; classify both the literal and the decoded
    spelling and keep the most sensitive kind."""
    spellings = {database, unquote(database)}
    kinds: list[tuple[DatabaseKind, str | None]] = []
    for spelling in spellings:
        path = _sqlite_path(spelling)
        if path is None:
            kinds.append((DatabaseKind.MEMORY, None))
        else:
            kinds.append((classify_path(path), path.name))
    for kind in _SENSITIVITY:
        for found, name in kinds:
            if found is kind:
                return found, name
    return kinds[0]


def classify_database_url(database_url: str) -> tuple[DatabaseKind, str | None]:
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        return DatabaseKind.INVALID, None
    backend = url.get_backend_name()
    if backend == "postgresql":
        name = url.database or ""
        if _SERVER_BACKUP_NAME.search(name):
            return DatabaseKind.BACKUP, None
        if _SERVER_PILOT_NAME.search(name):
            return DatabaseKind.PILOT, None
        return DatabaseKind.SERVER, None
    if backend != "sqlite":
        return DatabaseKind.INVALID, None
    kind, name = _classify_spellings(url.database or "")
    # `mode=memory` opens an in-memory database only in URI mode, and the
    # dialect's notion of "URI mode" (true/TRUE/yes/on/1, repeated keys) is
    # not re-implemented here: a URL naming a protected file is ALWAYS
    # classified as that file, whatever its query says. Only an otherwise
    # ordinary name with mode=memory counts as in-memory.
    modes = url.query.get("mode")
    modes = modes if isinstance(modes, tuple) else (modes,)
    if kind is DatabaseKind.OTHER_SQLITE and "memory" in modes:
        return DatabaseKind.MEMORY, None
    return kind, name


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
