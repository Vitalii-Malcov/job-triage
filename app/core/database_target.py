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

The production runtime database is recognised independently of the checkout
the process runs from (Codex audit of 76e8dc1, P2-2: from a second Git
worktree the real file used to classify as OTHER_SQLITE, bypassing the
test-role refusal and the snapshot requirement). A path is
PRODUCTION_RUNTIME when it is, after resolving `..` and symlinks, or as the
same file on disk (hard link), `runtime/job_triage_runtime.db` of this
checkout or of ANY worktree of its Git repository (found from the `.git`
metadata files; Git itself reads `core.worktree`, once per process),
or when its file name is
`job_triage_runtime.db` anywhere outside a backup location.

`check_database_target` is called once at web/scheduler startup, before any
session is used. Error messages name the kind and the file name, never the
URL itself (a PostgreSQL URL can carry a password).
"""

import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DB_NAME = "job_triage_runtime.db"
PRODUCTION_RUNTIME_DB = PROJECT_ROOT / "runtime" / PRODUCTION_DB_NAME
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


def allowed_kinds(role: "DatabaseRole | None") -> frozenset["DatabaseKind"]:
    """The database kinds a process with this role may use (same table as
    the web/scheduler startup check)."""
    return _ALLOWED[role]


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


class _UnsupportedSqliteUri(ValueError):
    """A `file:` URI with a non-local authority."""


def _sqlite_path(database: str) -> Path | None:
    """The filesystem path a SQLite `database` part names, or None for an
    in-memory database. Relative paths resolve against the working
    directory, exactly as SQLite opens them."""
    if database.startswith("file:"):
        database = database[len("file:") :].split("?", 1)[0].split("#", 1)[0]
        if database.startswith("//"):
            # SQLite URI authority: only empty or "localhost" is opened (it is
            # dropped, the rest is the path); anything else is not a local file.
            authority, _, tail = database[2:].partition("/")
            if authority.casefold() not in ("", "localhost"):
                raise _UnsupportedSqliteUri(authority)
            database = "/" + tail
        if re.match(r"^/[A-Za-z]:", database):
            database = database[1:]
    if not database or database == ":memory:":
        return None
    return Path(database)


# A PostgreSQL database whose NAME says pilot/backup is treated like the file.
_SERVER_PILOT_NAME = re.compile(r"pilot|stage\d+|scratch", re.IGNORECASE)
_SERVER_BACKUP_NAME = re.compile(r"backup|\bbak\b|_bak|pre[-_]", re.IGNORECASE)


def _same_file(path: Path, other: Path) -> bool:
    """Same file on disk (catches a hard link under another name, which a
    path comparison cannot -- Codex night-audit review)."""
    try:
        return path.exists() and other.exists() and os.path.samefile(path, other)
    except OSError:
        return False


def _git_text(data: bytes) -> str:
    """Git stores paths as raw bytes: decode without losing any (no newline
    translation, undecodable bytes kept as surrogates like `os.fsdecode`)."""
    return data.decode("utf-8", "surrogateescape")


def _read_pointer(path: Path, prefix: str = "") -> Path | None:
    """A path stored in a Git metadata file, resolved against its directory.
    Like Git, only the exact prefix and trailing CR/LF are removed -- other
    whitespace belongs to the path."""
    try:
        text = _git_text(path.read_bytes()).rstrip("\r\n")
    except OSError:
        return None
    if not text.startswith(prefix) or len(text) == len(prefix):
        return None
    target = Path(text[len(prefix) :])
    return target if target.is_absolute() else path.parent / target


def _git_core_worktrees(common: Path) -> list[str]:
    """Every `core.worktree` value Git itself reads for the repository whose
    Git directory is `common`. Git runs inside that directory with the
    caller's GIT_* variables removed, so Git's own syntax rules, includes,
    `includeIf` conditions (evaluated for this repository) and the main
    worktree's `config.worktree` (`extensions.worktreeConfig`) all apply;
    global/system values only add candidates. No hand-written parser: Codex
    rounds 6-10 showed that re-implementing Git's config syntax misses cases.
    [] when Git is unavailable or fails (residual risk, docs section 11)."""
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "config", "--includes", "--null", "--get-all", "core.worktree"],
            cwd=common,
            env=environment,
            capture_output=True,  # bytes: no newline translation, no code page
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:  # 1: unset; anything else: Git could not read it
        return []
    return [value for value in _git_text(result.stdout).split("\0") if value]


def _core_worktrees(common: Path) -> list[Path]:
    """Main-worktree candidates from `core.worktree`, relative to the Git
    directory like Git. A common directory not named `.git` records its
    main worktree only there."""
    paths = [Path(value) for value in _git_core_worktrees(common)]
    return [path if path.is_absolute() else common / path for path in paths]


def checkout_roots(root: Path) -> list[Path]:
    """`root` plus every worktree of its Git repository: the main worktree
    (parent of the common `.git` directory, or its `core.worktree`) and each
    linked worktree listed under `<common>/worktrees/*/gitdir`. Unreadable
    metadata is skipped. Without `core.worktree`, Git itself does not record
    where the main worktree of a `--separate-git-dir` repository is."""
    roots = {Path(_identity(root))}
    dotgit = root / ".git"
    git_dir = dotgit if dotgit.is_dir() else _read_pointer(dotgit, "gitdir: ")
    if git_dir is None:
        return sorted(roots)
    common = _read_pointer(git_dir / "commondir") or git_dir
    common = Path(os.path.realpath(os.path.abspath(common)))
    if common.name.lower() == ".git":
        roots.add(Path(_identity(common.parent)))
    for configured in _core_worktrees(common):  # e.g. `--separate-git-dir`
        roots.add(Path(_identity(configured)))
    try:
        linked = list((common / "worktrees").iterdir())
    except OSError:
        linked = []
    for entry in linked:
        worktree_git = _read_pointer(entry / "gitdir")
        if worktree_git is not None:
            roots.add(Path(_identity(worktree_git.parent)))
    return sorted(roots)


@lru_cache(maxsize=1)
def known_production_databases() -> tuple[Path, ...]:
    """The runtime database of every worktree of this repository."""
    return tuple(root / "runtime" / PRODUCTION_DB_NAME for root in checkout_roots(PROJECT_ROOT))


def _is_backup(resolved: Path) -> bool:
    return _BACKUP_DIR in {part.lower() for part in resolved.parts} or bool(
        _BACKUP_NAME.search(resolved.name)
    )


def classify_path(path: Path) -> DatabaseKind:
    """Pure: `realpath`/`stat` only, the file is never opened."""
    identity = _identity(path)
    for production in (PRODUCTION_RUNTIME_DB, *known_production_databases()):
        if identity == _identity(production) or _same_file(path, production):
            return DatabaseKind.PRODUCTION_RUNTIME
    resolved = Path(os.path.realpath(os.path.abspath(path)))
    if _is_backup(resolved):
        return DatabaseKind.BACKUP
    # the production file name, wherever it lives (another clone, an old
    # project location, a symlinked directory): fail closed
    if PRODUCTION_DB_NAME in {resolved.name.casefold(), Path(path).name.casefold()}:
        return DatabaseKind.PRODUCTION_RUNTIME
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
        try:
            path = _sqlite_path(spelling)
        except _UnsupportedSqliteUri:
            return DatabaseKind.INVALID, None  # refused for every role
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
    # The query (mode=memory, uri=...) never changes the classification: the
    # dialect's notion of "URI mode" (true/TRUE/yes/on/1, repeated keys) is not
    # re-implemented here, so the NAMED file decides. Whether such a URL may
    # open an in-memory database instead is checked separately
    # (`requests_memory_mode`), and refused for roles that need the real file.
    return _classify_spellings(url.database or "")


def requests_memory_mode(database_url: str) -> bool:
    """True if a SQLite URL could make SQLite open an in-memory database.

    Deliberately conservative instead of re-implementing how the dialect
    re-assembles its URI (it writes query values back unescaped, so an
    encoded `&` or `#` inside a value can smuggle `mode=memory` past an exact
    key/value check): any "memory" in the URL's query -- literal or
    percent-decoded, any key, any value -- or in the query part of a `file:`
    URI filename counts. Never true for PostgreSQL or for a plain path that
    merely contains the text (e.g. a directory named "mode=memory")."""
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        return False
    if url.get_backend_name() != "sqlite":
        return False
    texts = []
    for key, value in url.query.items():
        values = value if isinstance(value, tuple) else (value,)
        texts += [str(key), *(str(v) for v in values)]
    database = url.database or ""
    if database.startswith("file:") and "?" in database:
        texts.append(database.split("?", 1)[1])
    return any("memory" in unquote(text).casefold() for text in texts)


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
    # A URL that names a protected database but asks for mode=memory is
    # contradictory (it may open an empty in-memory database instead of the
    # real file); a production/pilot process must always open the real file.
    protected = target.kind in {
        DatabaseKind.PRODUCTION_RUNTIME,
        DatabaseKind.PILOT,
        DatabaseKind.BACKUP,
    }
    needs_real_file = target.role in (DatabaseRole.PRODUCTION, DatabaseRole.PILOT)
    if (protected or needs_real_file) and requests_memory_mode(settings.database_url):
        raise DatabaseTargetError(
            f"refusing {target.describe()}: this process must open the real database file, "
            "not mode=memory"
        )
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
