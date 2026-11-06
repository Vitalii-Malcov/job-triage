"""Task 23: refuse a second web process while the state is process-local.

With `SHARED_STATE_BACKEND=memory` (default) rate limits, the failed-authentication budget, UI
logout revocations and the cookie nonce live in ONE process. A second process (`--workers 2`,
two launches against the same database) would silently multiply every limit and reject the other
process's cookies. This exclusive, non-blocking lock turns that misconfiguration into a clear
startup failure instead.

The lock is an OS file lock on a file named after the database IDENTITY (the resolved SQLite file,
or PostgreSQL host:port/name without credentials; hashed), in `SINGLE_PROCESS_LOCK_DIR` (default:
the system temp directory). It covers every launcher that shares that directory - processes of one
host, but NOT containers with separate filesystems: those need a shared directory/volume or the
`database` backend; the operating system releases it when the process dies, so a crash never
leaves a stale lock. It is NOT taken with `SHARED_STATE_BACKEND=database` (several workers are then
supported) or when `SINGLE_PROCESS_LOCK_ENABLED=false`.
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)


class SingleProcessLockError(RuntimeError):
    """The single-process lock could not be taken (a second process, or the lock
    location cannot be provisioned)."""


class SecondWebProcessError(SingleProcessLockError):
    """Another web process already holds this database's single-process lock."""


def database_identity(database_url: str) -> str:
    """What makes two URLs the SAME database: the resolved file for SQLite (so `./a.db`, `a.db`
    and an absolute spelling agree; the case is folded on Windows), host:port/name for
    PostgreSQL - never the credentials. Anything unparsable falls back to the raw text."""
    try:
        url = make_url(database_url)
    except Exception:  # noqa: BLE001 - identity only; never echo the value
        return database_url
    backend = url.get_backend_name()
    if backend == "sqlite":
        if not url.database or url.database == ":memory:":
            return "sqlite::memory:"
        database = url.database
        if str(url.query.get("uri", "")).casefold() in {"1", "true", "yes"} and database.startswith(
            "file:"
        ):
            # SQLite URI form: `file:jobs.db?...` / `file:///abs/jobs.db` name the same file
            database = database[len("file:") :].split("?", 1)[0]
            if database.startswith("//"):
                database = database[2:]
            if len(database) > 2 and database[0] == "/" and database[2] == ":":
                database = database[1:]  # `/E:/dir/jobs.db` (file:///E:/...) -> `E:/dir/jobs.db`
        path = str(Path(database).resolve())
        return "sqlite:" + (path.casefold() if os.name == "nt" else path)
    # The EFFECTIVE connection target: query-string overrides win over the authority part and
    # an omitted port means the dialect default (PostgreSQL 5432). No credentials.
    query = url.query
    host = str(query.get("host") or url.host or "").casefold()
    embedded = ""
    head, separator, tail = host.rpartition(":")
    if separator and head and tail.isdigit() and ":" not in head:  # `host=name:port`
        host, embedded = head, tail
    # port precedence: query `port` > the port embedded in the query `host` > the authority port
    port = str(query.get("port") or embedded or url.port or "")
    if not port and backend == "postgresql":
        port = "5432"
    name = str(query.get("dbname") or url.database or "")
    return f"{backend}:{host}:{port}/{name}"


def lock_path_for(database_url: str, directory: str = "") -> Path:
    digest = hashlib.sha256(database_identity(database_url).encode("utf-8")).hexdigest()[:16]
    base = Path(directory) if directory else Path(tempfile.gettempdir())
    return base / f"jobtriage-web-{digest}.lock"


class SingleProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
        except OSError as exc:
            raise SingleProcessLockError(
                f"cannot provision the single-process lock file ({type(exc).__name__}); "
                "check SINGLE_PROCESS_LOCK_DIR or set SHARED_STATE_BACKEND=database"
            ) from None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SecondWebProcessError(
                "another web process is already running against this database: with the "
                "default in-memory state only ONE process is supported (rate limits and UI "
                "sessions are per process). Run a single process (--workers 1), or set "
                "SHARED_STATE_BACKEND=database to share that state between workers."
            ) from None
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            # The file stays: removing it would let a later process lock a new file while
            # this one still holds the old one. Closing releases the lock.
            handle.close()


def acquire_if_required(settings) -> SingleProcessLock | None:
    """The held lock, or None when this configuration does not need one."""
    if settings.shared_state_backend != "memory" or not settings.single_process_lock_enabled:
        return None
    lock = SingleProcessLock(lock_path_for(settings.database_url, settings.single_process_lock_dir))
    lock.acquire()
    return lock
