"""Stage 13B operator CLI: SQLite pilot archive + CandidateProfile-only transfer
into a FRESH PostgreSQL deployment. See docs/STAGE_13B_REHEARSAL.md.

Subcommands (each exits non-zero on ANY invariant violation; there is no
--force / --overwrite / --ignore-digest / --skip-provenance escape hatch):

  archive  --source PILOT.db --archive-dir DIR
      Byte-copy the SQLite pilot to a new, read-only archive file outside the
      repository and validate the COPY read-only (integrity_check, single
      Alembic revision == application head, exactly one candidate profile).
      The source is only ever read; it is never opened through SQLite at all.
      Refuses a source with a -wal/-journal sidecar or in WAL mode instead of
      checkpointing it.

  export   --out SNAPSHOT.json        (source: $TRANSFER_SOURCE_DATABASE_URL)
      Pure read (SQLite opened mode=ro; PostgreSQL in a READ ONLY transaction).
      Never creates a profile. Writes a new file (never overwrites), 0600 where
      the platform supports it, and refuses any path inside the repository.

  verify   --snapshot SNAPSHOT.json
      Structural + digest validation only; touches no database.

  import   --snapshot SNAPSHOT.json   (target: $TRANSFER_TARGET_DATABASE_URL)
      PostgreSQL only. The target must be at the application head, hold no
      candidate profile and no operational rows. One atomic transaction.

  compare  (source: $TRANSFER_SOURCE_DATABASE_URL, target: $TRANSFER_TARGET_DATABASE_URL)
      Read-only on both. Semantic digests, typed field-by-field comparison,
      scoring skill projection, generation-trust decisions, and the target's
      historical operational tables (all must be empty).

Database URLs come ONLY from environment variables (never CLI arguments) and
are never printed. Output is a JSON summary of counts, digests, revisions and
statuses -- never a candidate value. Expected failures print one sanitized
`ERROR category=... detail=...` line, never a traceback.

Usage:
    python -m scripts.candidate_profile_transfer_stage13b archive \\
        --source stage11_pilot.db --archive-dir C:/path/outside/repo
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.candidate_profile_repository import get_candidate_skills_for_scoring
from app.services import candidate_profile_transfer as transfer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL_ENV = "TRANSFER_SOURCE_DATABASE_URL"
TARGET_URL_ENV = "TRANSFER_TARGET_DATABASE_URL"
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_SIDECARS = ("-wal", "-journal", "-shm")

logger = logging.getLogger("stage13b.transfer")


class CliError(transfer.TransferError):
    category = "invalid_invocation"


class ArchiveError(transfer.TransferError):
    category = "archive_invalid"


# --- filesystem helpers -----------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refuse_inside_repository(path: Path, what: str) -> Path:
    resolved = path.resolve()
    if resolved == PROJECT_ROOT or resolved.is_relative_to(PROJECT_ROOT):
        raise CliError(f"{what} must be outside the repository")
    return resolved


def write_new_private_file(path: Path, data: bytes) -> None:
    """Create `path` exclusively (never overwrite), owner-only where the
    platform honours POSIX modes (Windows ignores them; keep such files in a
    user-private directory)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise CliError("output file already exists; refusing to overwrite") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


# --- engines ------------------------------------------------------------------------


def _url_from_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CliError(f"environment variable {name} is not set")
    return value


def _same_database(a: str, b: str) -> bool:
    left, right = make_url(a), make_url(b)
    if left.get_backend_name() == right.get_backend_name() == "sqlite":
        return Path(left.database or "").resolve() == Path(right.database or "").resolve()
    keys = ("host", "port", "database")
    return left.get_backend_name() == right.get_backend_name() and all(
        getattr(left, k) == getattr(right, k) for k in keys
    )


def read_only_engine(database_url: str) -> Engine:
    """SQLite: `mode=ro` URI (a missing file is never created, writes fail).
    PostgreSQL: every transaction is READ ONLY (`postgresql_readonly`)."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        database = url.database
        if not database or database == ":memory:" or not Path(database).is_file():
            raise CliError("SQLite source database file does not exist")
        ro_url = url.set(
            database=Path(database).resolve().as_uri(), query={"mode": "ro", "uri": "true"}
        )
        return create_engine(ro_url, poolclass=NullPool, connect_args={"timeout": 1.0})
    if url.get_backend_name() == "postgresql":
        engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 10})
        return engine.execution_options(postgresql_readonly=True)
    raise CliError("unsupported database dialect")


def writable_postgres_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise CliError("import target must be PostgreSQL")
    return create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 10})


@contextmanager
def session_for(engine: Engine) -> Iterator[Session]:
    session = Session(engine, autoflush=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    return Path(url.database).resolve()


# --- archive ------------------------------------------------------------------------


def _check_sqlite_source(source: Path) -> None:
    if not source.is_file():
        raise ArchiveError("source pilot database does not exist")
    for suffix in SQLITE_SIDECARS:
        if Path(f"{source}{suffix}").exists():
            raise ArchiveError(
                f"source has a {suffix} sidecar (open writer or hot journal); stop every "
                "process using it -- it is never checkpointed or modified here"
            )
    with source.open("rb") as handle:
        header = handle.read(100)
    if len(header) < 100 or header[:16] != SQLITE_HEADER:
        raise ArchiveError("source is not a SQLite 3 database file")
    if header[18] != 1 or header[19] != 1:
        raise ArchiveError(
            "source is in WAL mode; a single-file byte copy is not a proven-consistent snapshot"
        )


def validate_sqlite_archive(archive: Path) -> dict:
    """Read-only validation of the archive copy (`mode=ro&immutable=1`: SQLite
    neither writes nor creates any sidecar)."""
    uri = f"{archive.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        tables = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        revisions = (
            [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]
            if "alembic_version" in tables
            else []
        )
        counts = {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables
        }
    finally:
        conn.close()
    return {
        "integrity_check": "ok" if integrity == ["ok"] else "FAILED",
        "alembic_revisions": revisions,
        "candidate_profile_count": counts.get("candidate_profiles"),
        "table_counts": counts,
    }


def archive_sqlite(source: Path, archive_dir: Path) -> dict:
    source = source.resolve()
    archive_dir = refuse_inside_repository(archive_dir, "--archive-dir")
    if not archive_dir.is_dir():
        raise CliError("--archive-dir does not exist")
    _check_sqlite_source(source)

    source_stat_before = source.stat()
    source_sha_before = sha256_file(source)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = (
        archive_dir / f"{source.stem}.stage13b-archive.{stamp}.{source_sha_before[:16]}.sqlite"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(archive, flags, 0o600)
    except FileExistsError:
        raise CliError("archive file already exists; refusing to overwrite") from None
    with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    os.chmod(archive, stat.S_IREAD)

    source_sha_after = sha256_file(source)
    source_stat_after = source.stat()
    archive_sha = sha256_file(archive)
    if not (source_sha_before == source_sha_after == archive_sha):
        raise ArchiveError("source changed during copy or archive differs from source")
    if (source_stat_before.st_size, source_stat_before.st_mtime_ns) != (
        source_stat_after.st_size,
        source_stat_after.st_mtime_ns,
    ):
        raise ArchiveError("source size/mtime changed during archiving")

    archive_mtime = archive.stat().st_mtime_ns
    validation = validate_sqlite_archive(archive)
    if sha256_file(archive) != archive_sha or archive.stat().st_mtime_ns != archive_mtime:
        raise ArchiveError("read-only validation altered the archive")
    for suffix in SQLITE_SIDECARS:
        if Path(f"{archive}{suffix}").exists():
            raise ArchiveError("read-only validation created a SQLite sidecar")

    head = transfer.application_head()
    problems = []
    if validation["integrity_check"] != "ok":
        problems.append("integrity_check")
    if validation["alembic_revisions"] != [head]:
        problems.append("alembic_revision")
    if validation["candidate_profile_count"] != 1:
        problems.append("candidate_profile_count")
    summary = {
        "status": "PASS" if not problems else "FAIL",
        "archive_path": str(archive),
        "byte_size": archive.stat().st_size,
        "source_sha256": source_sha_before,
        "archive_sha256": archive_sha,
        "source_unchanged": True,
        "archive_unchanged_after_validation": True,
        "application_head": head,
        **validation,
    }
    if problems:
        summary["problems"] = problems
    return summary


# --- export / verify / import / compare ---------------------------------------------


def _dialect(database_url: str) -> str:
    return make_url(database_url).get_backend_name()


def _export_from(database_url: str) -> tuple[transfer.TransferProfile, str]:
    with session_for(read_only_engine(database_url)) as db:
        profile = transfer.export_profile(db)
        revision = transfer.require_head_revision(db)
    return profile, revision


def command_export(out: Path) -> dict:
    out = refuse_inside_repository(out, "--out")
    if not out.parent.is_dir():
        raise CliError("output directory does not exist")
    source_url = _url_from_env(SOURCE_URL_ENV)
    sqlite_path = _sqlite_path(source_url)
    sha_before = sha256_file(sqlite_path) if sqlite_path and sqlite_path.is_file() else None

    profile, revision = _export_from(source_url)
    snapshot = transfer.build_snapshot(
        profile, source_dialect=_dialect(source_url), source_revision=revision
    )
    data = transfer.serialize_snapshot(snapshot)
    transfer.parse_snapshot(data)  # the file we write must itself pass import validation
    if sqlite_path is not None and sha256_file(sqlite_path) != sha_before:
        raise transfer.SourceProfileInvalidError("source database changed during export")
    write_new_private_file(out, data)
    return {
        "status": "PASS",
        "snapshot_path": str(out),
        "snapshot_sha256": hashlib.sha256(data).hexdigest(),
        "semantic_digest": snapshot["semantic_digest"],
        "source_dialect": snapshot["source"]["dialect"],
        "source_revision": revision,
        "source_sqlite_sha256_unchanged": True if sqlite_path is not None else "n/a",
        "counts": _counts(profile),
    }


def _counts(profile: transfer.TransferProfile) -> dict:
    counts = {c: len(getattr(profile, c)) for c in transfer.COLLECTIONS}
    counts["field_trust_entries"] = len(profile.field_trust)
    counts["job_preferences_present"] = profile.job_preferences is not None
    return counts


def _read_snapshot(path: Path) -> transfer.ParsedSnapshot:
    if not path.is_file():
        raise CliError("snapshot file does not exist")
    return transfer.parse_snapshot(path.read_bytes())


def command_verify(snapshot_path: Path) -> dict:
    parsed = _read_snapshot(snapshot_path)
    return {
        "status": "PASS",
        "semantic_digest": parsed.digest,
        "source_dialect": parsed.source_dialect,
        "source_revision": parsed.source_revision,
        "counts": _counts(parsed.profile),
    }


def command_import(snapshot_path: Path) -> dict:
    parsed = _read_snapshot(snapshot_path)
    target_url = _url_from_env(TARGET_URL_ENV)
    source_url = os.environ.get(SOURCE_URL_ENV, "").strip()
    if source_url and _same_database(source_url, target_url):
        raise CliError("source and target databases must be distinct")
    with session_for(writable_postgres_engine(target_url)) as db:
        digest = transfer.import_profile(db, parsed)
    return {
        "status": "PASS",
        "target_dialect": "postgresql",
        "semantic_digest": digest,
        "profile_version": transfer.FRESH_PROFILE_VERSION,
        "counts": _counts(parsed.profile),
    }


def compare_databases(source_url: str, target_url: str) -> dict:
    """All checks read-only. Status PASS only if every check passes."""
    with session_for(read_only_engine(source_url)) as db:
        source = transfer.export_profile(db)
        source_revision = transfer.require_head_revision(db)
        source_scoring = get_candidate_skills_for_scoring(db)
        source_trust = transfer.trust_decisions(transfer.read_profile_response(db))
    with session_for(read_only_engine(target_url)) as db:
        target = transfer.export_profile(db)
        target_revision = transfer.require_head_revision(db)
        target_scoring = get_candidate_skills_for_scoring(db)
        target_trust = transfer.trust_decisions(transfer.read_profile_response(db))
        historical = transfer.historical_state_counts(db)
        profile_version = transfer.read_profile_response(db).profile_version

    source_digest = transfer.semantic_digest(source)
    target_digest = transfer.semantic_digest(target)
    mismatches = transfer.compare_profiles(source, target)
    populated = sorted(name for name, count in historical.items() if count)
    checks = {
        "semantic_digest_equal": source_digest == target_digest,
        "typed_semantic_equal": not mismatches,
        "scoring_skill_projection_equal": source_scoring == target_scoring,
        "generation_trust_equal": source_trust == target_trust,
        "historical_state_absent": not populated,
        "revisions_equal": source_revision == target_revision,
        "target_profile_version_fresh": profile_version == transfer.FRESH_PROFILE_VERSION,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "source_semantic_digest": source_digest,
        "target_semantic_digest": target_digest,
        "source_revision": source_revision,
        "target_revision": target_revision,
        "typed_mismatch_paths": mismatches,
        "scoring_skill_count": len(target_scoring),
        "top_level_usable_fields": sum(target_trust["top_level"].values()),
        "historical_tables_checked": len(historical),
        "historical_tables_populated": populated,
        "counts": _counts(target),
    }


def command_compare() -> dict:
    source_url = _url_from_env(SOURCE_URL_ENV)
    target_url = _url_from_env(TARGET_URL_ENV)
    if _same_database(source_url, target_url):
        raise CliError("source and target databases must be distinct")
    return compare_databases(source_url, target_url)


# --- entry point --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 13B CandidateProfile-only transfer (see module docstring)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    archive = sub.add_parser("archive", help="byte-copy + read-only validate the SQLite pilot")
    archive.add_argument("--source", type=Path, required=True)
    archive.add_argument("--archive-dir", type=Path, required=True)
    export = sub.add_parser("export", help=f"read-only export from ${SOURCE_URL_ENV}")
    export.add_argument("--out", type=Path, required=True)
    verify = sub.add_parser("verify", help="validate a snapshot file")
    verify.add_argument("--snapshot", type=Path, required=True)
    imp = sub.add_parser("import", help=f"import into an EMPTY PostgreSQL ${TARGET_URL_ENV}")
    imp.add_argument("--snapshot", type=Path, required=True)
    sub.add_parser("compare", help="read-only source/target semantic + trust comparison")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict:
    if args.command == "archive":
        return archive_sqlite(args.source, args.archive_dir)
    if args.command == "export":
        return command_export(args.out)
    if args.command == "verify":
        return command_verify(args.snapshot)
    if args.command == "import":
        return command_import(args.snapshot)
    return command_compare()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run(args)
    except transfer.TransferError as exc:
        print(f"ERROR category={exc.category} detail={exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- never a traceback: it may carry values
        print(f"ERROR category=unexpected error_type={type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
