"""Stage 14D: run the application explicitly against the canonical database.

The operator's normal `.env` is never rewritten. Instead the canonical
target is selected per process through environment overrides, which
pydantic-settings ranks above `.env`:

- `DATABASE_URL` = `sqlite:///<absolute canonical path>`;
- `POSTGRES_HOST` = "" so the discrete-parts PostgreSQL URL builder can
  never silently replace the canonical target;
- every flag in `startup_diagnostics.SAFETY_FLAGS` (outbound sending,
  Alembic auto-upgrade, Telegram/Gmail drafting and approval, automation,
  digests, auto research) = false.

`canonical_settings` builds a `Settings` under exactly those overrides and
fails closed (`CanonicalRuntimeError`) unless the result targets the
canonical file and every safety flag is false. `run_preflight` adds a
READ-ONLY inspection (SQLite `mode=ro` URI plus `PRAGMA query_only`) of the
migration revision, and verifies the file's SHA-256 is identical before and
after. Nothing here prints a URL with credentials: the canonical target is
a local SQLite file and only its path and digest are reported.
"""

import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.startup_diagnostics import SAFETY_FLAGS


class CanonicalRuntimeError(ValueError):
    """The canonical runtime cannot be selected safely."""


def validate_canonical_db_path(path: Path) -> Path:
    """Absolute, existing regular `.db` file, else `CanonicalRuntimeError`.
    Never opens the file. Locality (UNC/mapped drives) is the caller's
    string-level check; see scripts/stage14d_canonical_runtime.py."""
    if not path.is_absolute():
        raise CanonicalRuntimeError("canonical database path must be absolute")
    if path.suffix.lower() != ".db":
        raise CanonicalRuntimeError("canonical database path must name a .db file")
    if not path.exists():
        raise CanonicalRuntimeError("canonical database file does not exist")
    if not path.is_file():
        raise CanonicalRuntimeError("canonical database path is not a regular file")
    return path


def canonical_database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def canonical_environment(path: Path) -> dict[str, str]:
    """The exact per-process overrides that select the canonical runtime."""
    overrides = {"DATABASE_URL": canonical_database_url(path), "POSTGRES_HOST": ""}
    overrides.update({flag.upper(): "false" for flag in SAFETY_FLAGS})
    return overrides


@contextmanager
def _environment(overrides: Mapping[str, str]) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def enabled_safety_flags(settings: Settings) -> list[str]:
    return [flag for flag in SAFETY_FLAGS if getattr(settings, flag) is not False]


def canonical_settings(path: Path) -> Settings:
    """`Settings()` exactly as a canonical-runtime process would build it."""
    path = validate_canonical_db_path(path)
    with _environment(canonical_environment(path)):
        settings = Settings()
    if settings.database_url != canonical_database_url(path):
        raise CanonicalRuntimeError("DATABASE_URL override did not select the canonical database")
    enabled = enabled_safety_flags(settings)
    if enabled:
        raise CanonicalRuntimeError(f"safety flags still enabled: {', '.join(enabled)}")
    return settings


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _query_only(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA query_only = ON")
    finally:
        cursor.close()


def read_database_revisions(path: Path) -> list[str]:
    """Alembic revisions of `path`, read through a read-only connection."""
    engine = create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true", poolclass=NullPool)
    event.listen(engine, "connect", _query_only)
    try:
        with engine.connect() as connection:
            return sorted(
                row[0]
                for row in connection.execute(text("SELECT version_num FROM alembic_version"))
            )
    finally:
        engine.dispose()


@dataclass(frozen=True)
class PreflightReport:
    database_path: Path
    sha256_before: str
    sha256_after: str
    expected_sha256: str | None
    database_revisions: tuple[str, ...]
    application_heads: tuple[str, ...]
    matcher_version: str
    flags: dict[str, bool]
    problems: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.problems


def run_preflight(
    path: Path,
    *,
    expected_sha256: str | None = None,
    revisions_reader=read_database_revisions,
) -> PreflightReport:
    """Read-only preflight of the canonical runtime. Raises
    `CanonicalRuntimeError` only when the target cannot be selected at all
    (missing file, override not effective); every other finding is a
    problem code on the report."""
    from app.agents.candidate_job_matcher import ALGORITHM_VERSION
    from app.services.readiness import expected_alembic_heads

    path = validate_canonical_db_path(path)
    before = file_sha256(path)
    problems: list[str] = []
    try:
        settings = canonical_settings(path)
        flags = {flag: bool(getattr(settings, flag)) for flag in SAFETY_FLAGS}
    except CanonicalRuntimeError as exc:
        problems.append(f"RUNTIME_SETTINGS: {exc}")
        flags = {}
    try:
        revisions = tuple(revisions_reader(path))
    except Exception as exc:  # noqa: BLE001 -- reported as a problem, never echoed raw
        revisions = ()
        problems.append(f"DATABASE_UNREADABLE: {type(exc).__name__}")
    heads = expected_alembic_heads()
    after = file_sha256(path)

    if expected_sha256 is not None and before != expected_sha256.lower():
        problems.append("DATABASE_SHA256_UNEXPECTED")
    if before != after:
        problems.append("DATABASE_CHANGED_DURING_PREFLIGHT")
    if len(heads) != 1:
        problems.append("APPLICATION_HAS_MULTIPLE_MIGRATION_HEADS")
    if revisions != heads:
        problems.append("DATABASE_NOT_AT_MIGRATION_HEAD")
    return PreflightReport(
        database_path=path,
        sha256_before=before,
        sha256_after=after,
        expected_sha256=expected_sha256,
        database_revisions=revisions,
        application_heads=tuple(heads),
        matcher_version=ALGORITHM_VERSION,
        flags=flags,
        problems=tuple(problems),
    )


def _state(value: bool) -> str:
    return "true" if value else "false"


def _yes(value: bool) -> str:
    return "YES" if value else "NO"


def render_preflight(report: PreflightReport) -> str:
    flags = report.flags
    unchanged = report.sha256_before == report.sha256_after
    at_head = report.database_revisions == report.application_heads
    lines = [
        "DATABASE TARGET:",
        f"  sqlite file {report.database_path}",
        f"  sha256 {report.sha256_before}"
        + (
            ""
            if report.expected_sha256 is None
            else f" (expected {report.expected_sha256.lower()})"
        ),
        f"  unchanged by preflight: {_yes(unchanged)}",
        "OUTBOUND STATE:",
    ]
    if flags:
        lines += [
            f"  {flag}={_state(flags[flag])}"
            for flag in SAFETY_FLAGS
            if flag != "alembic_auto_upgrade"
        ]
    else:
        lines.append("  UNKNOWN (settings could not be built)")
    lines += [
        "ALEMBIC STATE:",
        "  alembic_auto_upgrade="
        + (_state(flags["alembic_auto_upgrade"]) if "alembic_auto_upgrade" in flags else "UNKNOWN"),
        f"  database revision: {', '.join(report.database_revisions) or 'UNKNOWN'}",
        "MATCHER VERSION:",
        f"  {report.matcher_version}",
        "MIGRATION HEAD:",
        f"  application head: {', '.join(report.application_heads)}",
        f"  database at head: {_yes(at_head)}",
        f"RESULT: {'PASS' if report.ok else 'FAIL'}",
    ]
    lines += [f"  problem: {problem}" for problem in report.problems]
    return "\n".join(lines)
