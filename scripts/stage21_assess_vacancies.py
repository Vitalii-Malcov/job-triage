"""Stage 21: fill the vacancy assessment cache (`job_fit_assessments`) offline.

    python scripts/stage21_assess_vacancies.py --database DB [--apply]
        [--only-stale] [--limit N] [--snapshot DIR]
    python scripts/stage21_assess_vacancies.py --database-url postgresql+psycopg://... [--apply]

Default is a dry run: it compares every vacancy's current input key with its
cached row and prints how many rows would be inserted (NOT_ASSESSED), updated
(STALE) or left unchanged (FRESH). The dry run opens SQLite with
`mode=ro` and PostgreSQL with `default_transaction_read_only`, so it cannot
write.

`--apply` assesses the non-fresh vacancies (newest first) in batches of 200,
one transaction per batch, and prints progress with an ETA. Ctrl+C keeps every
finished batch; a re-run continues with the rows that are still not fresh
(idempotent). `--only-stale` refreshes existing stale rows only and leaves
not-yet-assessed vacancies out. Writing to the production runtime database
requires `--snapshot DIR` (an SQLite backup-API copy is taken first).

The key of a vacancy is computed BEFORE it is assessed: if an input changes in
between, the stored key no longer matches and the row reads as STALE, never
as current. The script runs no collector or matcher, sends nothing and writes
only `job_fit_assessments`; database roles and the Alembic head are checked
like in the other tracker CLIs.
"""

import argparse
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.core.database_target import DatabaseKind, classify_path
from app.services import fit_cache
from app.services.job_fit_assessment import assess_job

BATCH_SIZE = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fill the Stage 21 vacancy assessment cache")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--only-stale", action="store_true", help="refresh stale rows only")
    parser.add_argument("--limit", type=int, default=None, help="assess at most N vacancies")
    parser.add_argument("--snapshot", default=None, help="back up the SQLite file here first")
    return parser


def _readonly_engine(engine: Engine) -> Engine:
    """A second engine on the same target that cannot write."""
    url = engine.url
    if url.get_backend_name() == "sqlite":
        uri = Path(url.database).resolve().as_uri() + "?mode=ro"
        return create_engine(
            "sqlite://", creator=lambda: sqlite3.connect(uri, uri=True), poolclass=NullPool
        )
    return create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def snapshot(database: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{database.stem}.pre-stage21-{stamp}{database.suffix}"
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    return target


def plan(session: Session, *, only_stale: bool) -> tuple[dict[str, int], list[int]]:
    """Counts per cache state and the job ids to assess (newest first)."""
    current = fit_cache.current_keys(session)
    stored = fit_cache.cached_keys(session)
    counts = {"FRESH": 0, "STALE": 0, "NOT_ASSESSED": 0}
    todo: list[int] = []
    for job_id in sorted(current, reverse=True):
        state = fit_cache.cache_state(stored.get(job_id), current[job_id])
        counts[state] += 1
        if state == "STALE" or (state == "NOT_ASSESSED" and not only_stale):
            todo.append(job_id)
    return counts, todo


def assess_batch(session: Session, job_ids: list[int]) -> int:
    """Assess and upsert one batch in the caller's transaction."""
    inputs = fit_cache.global_inputs(session)
    keys = fit_cache.current_keys(session, job_ids, inputs)
    written = 0
    for job_id in job_ids:
        if job_id not in keys:  # deleted since the plan was made
            continue
        assessment = assess_job(session, job_id)
        fit_cache.upsert_assessment(
            session, job_id=job_id, key=keys[job_id], inputs=inputs, assessment=assessment
        )
        written += 1
    return written


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("REFUSED: --limit must be at least 1", file=sys.stderr)
        return 2
    try:
        engine = resolve_engine(args.database, args.database_url, writes=args.apply)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if args.snapshot and not args.database:
        print("REFUSED: --snapshot works for an SQLite --database only", file=sys.stderr)
        return 2
    if (
        args.apply
        and args.database
        and classify_path(Path(args.database)) is DatabaseKind.PRODUCTION_RUNTIME
        and not args.snapshot
    ):
        print("REFUSED: --apply on the production database needs --snapshot DIR", file=sys.stderr)
        return 2
    try:
        reader = _readonly_engine(engine)
        try:
            with Session(reader) as session:
                if not fit_cache.cache_table_present(session):
                    print("REFUSED: job_fit_assessments is missing", file=sys.stderr)
                    return 2
                counts, todo = plan(session, only_stale=args.only_stale)
        finally:
            reader.dispose()
        if args.limit is not None:
            todo = todo[: args.limit]
        total = sum(counts.values())
        print(
            f"vacancies={total} fresh={counts['FRESH']} stale={counts['STALE']} "
            f"not_assessed={counts['NOT_ASSESSED']} to_assess={len(todo)}"
        )
        if not args.apply:
            print("DRY RUN: nothing written")
            return 0
        if args.snapshot:
            copy = snapshot(Path(args.database), Path(args.snapshot))
            print(f"snapshot: {copy}")
        return _apply(engine, todo)
    finally:
        engine.dispose()


def _apply(engine: Engine, todo: list[int]) -> int:
    factory = sessionmaker(bind=engine)
    done = 0
    started = time.monotonic()
    try:
        for start in range(0, len(todo), BATCH_SIZE):
            batch = todo[start : start + BATCH_SIZE]
            with factory() as session, session.begin():
                done += assess_batch(session, batch)
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (len(todo) - start - len(batch)) / rate if rate else 0.0
            print(f"progress {start + len(batch)}/{len(todo)} rate={rate:.1f}/s eta={eta:.0f}s")
    except KeyboardInterrupt:
        print(f"INTERRUPTED: {done} rows written in finished batches; re-run to continue")
        return 130
    print(f"APPLIED: {done} rows written")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
