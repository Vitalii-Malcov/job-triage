"""Fill `jobs.source_reference` for existing Bundesagentur rows (Stage 22 P0-1).

    python scripts/backfill_source_reference.py --database PATH [--apply]
        [--snapshot DIR] [--json]

Only rows of source `bundesagentur` whose `source_reference` is still empty
are considered, and only a reference derivable from the row's own public BA
URL (`arbeitsagentur.de/jobsuche/jobdetail/<ref>`, strict shape) is written.
A row whose URL is the employer's own (`externeURL`) has no reference here;
the next collector run stores it (the search result carries it). Nothing is
ever overwritten: each write is `... WHERE source_reference IS NULL`.

Dry run by default: prints the counts and every reference shared by more
than one row (the same BA posting stored twice -- reported, not merged).
`--apply` on the production runtime database requires `--snapshot DIR` (a
verified SQLite backup-API copy, as for the Stage 21 backfill); backups are
refused by the shared target guard. Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.collectors.ba_reference import ba_reference_from_url
from app.collectors.bundesagentur import SOURCE_NAME
from app.core.database_target import DatabaseKind, classify_path
from app.db.models import JobRecord
from scripts.stage21_assess_vacancies import SnapshotError, _backup, _verify_snapshot

MAX_REPORTED_GROUPS = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fill jobs.source_reference from BA job URLs")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--snapshot", default=None, help="back up the SQLite file here first")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def plan(session: Session) -> dict:
    """Read-only: what an apply would write, and what it cannot fill."""
    rows = session.execute(
        select(JobRecord.id, JobRecord.url, JobRecord.source_reference).where(
            JobRecord.source == SOURCE_NAME
        )
    ).all()
    updates: dict[int, str] = {}
    already = external = 0
    stored = Counter(row.source_reference for row in rows if row.source_reference)
    for row in rows:
        if row.source_reference:
            already += 1
            continue
        reference = ba_reference_from_url(row.url)
        if reference is None:
            external += 1
        else:
            updates[row.id] = reference
    combined = stored + Counter(updates.values())
    shared = sorted((ref, n) for ref, n in combined.items() if n > 1)
    return {
        "ba_rows": len(rows),
        "already_set": already,
        "to_fill": len(updates),
        "not_derivable": external,
        "shared_references": len(shared),
        "shared_reference_rows": sum(n for _ref, n in shared),
        "shared_examples": [{"reference": r, "rows": n} for r, n in shared[:MAX_REPORTED_GROUPS]],
        "updates": updates,
    }


def apply(session: Session, updates: dict[int, str]) -> int:
    """One transaction; never overwrites (compare-and-set on NULL)."""
    written = 0
    for job_id, reference in sorted(updates.items()):
        result = session.execute(
            update(JobRecord)
            .where(JobRecord.id == job_id, JobRecord.source_reference.is_(None))
            .values(source_reference=reference)
        )
        written += result.rowcount
    return written


def snapshot(database: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{database.stem}.pre-source-reference-{stamp}{database.suffix}"
    _backup(database, target)
    _verify_snapshot(database, target)
    return target


def _report(result: dict, as_json: bool) -> None:
    public = {k: v for k, v in result.items() if k != "updates"}
    if as_json:
        print(json.dumps(public, sort_keys=True))
        return
    print(
        f"ba_rows={public['ba_rows']} already_set={public['already_set']} "
        f"to_fill={public['to_fill']} not_derivable={public['not_derivable']} "
        f"shared_references={public['shared_references']} "
        f"(rows {public['shared_reference_rows']})"
    )
    for group in public["shared_examples"]:
        print(f"  shared: {group['reference']} x{group['rows']}")


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        engine = resolve_engine(args.database, args.database_url, writes=args.apply)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        with Session(engine) as session:
            result = plan(session)
        _report(result, args.json)
        if not args.apply:
            print("DRY RUN: nothing written")
            return 0
        if args.snapshot:
            try:
                copy = snapshot(Path(args.database), Path(args.snapshot))
            except SnapshotError as exc:
                print(f"REFUSED: {exc}; nothing written", file=sys.stderr)
                return 2
            print(f"snapshot: {copy} (verified)")
        with Session(engine) as session, session.begin():
            written = apply(session, result["updates"])
        print(f"APPLIED: {written} rows written")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(run())
