"""Compute the missing candidate-job matcher analyses (Stage 22 P1).

    python scripts/stage22_match_vacancies.py --database PATH [--apply]
        [--limit N] [--snapshot DIR]

The fit tier needs a CURRENT matcher analysis per vacancy (`skills` is
NOT_EVALUATED without one -- the vacancy then stays NEEDS_OPERATOR). On the
2026-10-30 production copy only 770 of 2 888 vacancies had one. This runs
the existing, local and deterministic matcher
(`app.services.candidate_preparation.prepare_candidate_job_match`: no
network, no model) for every vacancy whose current analysis is missing
(another profile version, a changed posting or a new algorithm version) and
stores it in `candidate_job_matches` (append-only cache rows, unique per
identity). Nothing else is written.

Dry run by default (counts only, read-only). `--apply` on the production
runtime database requires `--snapshot DIR` (a verified SQLite backup-API
copy); backups are refused. Run it BEFORE the Stage 21 cache backfill.
Exit codes: 0 done, 1 some vacancies failed (reported), 2 refused.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.agents.candidate_job_matcher import ALGORITHM_VERSION
from app.core.database_target import DatabaseKind, classify_path
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint, get_cached_match
from app.db.candidate_profile_repository import get_candidate_profile
from app.db.models import JobRecord
from app.services.candidate_preparation import prepare_candidate_job_match
from scripts.stage21_assess_vacancies import (
    SnapshotError,
    _backup,
    _readonly_engine,
    _verify_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compute missing matcher analyses")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--limit", type=int, default=None, help="at most N vacancies")
    parser.add_argument("--snapshot", default=None, help="back up the SQLite file here first")
    return parser


def missing(session: Session) -> tuple[int, list[int]]:
    """(vacancies, ids without a current analysis), newest first."""
    profile = get_candidate_profile(session)
    jobs = session.scalars(select(JobRecord).order_by(JobRecord.id.desc())).all()
    if profile is None:
        return len(jobs), []
    todo = [
        job.id
        for job in jobs
        if get_cached_match(
            session,
            job_id=job.id,
            candidate_profile_version=profile.profile_version,
            job_snapshot_fingerprint=compute_job_snapshot_fingerprint(job),
            algorithm_version=ALGORITHM_VERSION,
        )
        is None
    ]
    return len(jobs), todo


def snapshot(database: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{database.stem}.pre-stage22-match-{stamp}{database.suffix}"
    _backup(database, target)
    _verify_snapshot(database, target)
    return target


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("REFUSED: --limit must be at least 1", file=sys.stderr)
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
        engine = resolve_engine(args.database, args.database_url, writes=args.apply)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        reader = _readonly_engine(engine)
        try:
            with Session(reader) as session:
                if get_candidate_profile(session) is None:
                    print("REFUSED: no candidate profile stored", file=sys.stderr)
                    return 2
                total, todo = missing(session)
        finally:
            reader.dispose()
        if args.limit is not None:
            todo = todo[: args.limit]
        print(f"vacancies={total} without_current_analysis={len(todo)} ({ALGORITHM_VERSION})")
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
        failed = 0
        started = time.monotonic()
        with Session(engine) as session:
            for done, job_id in enumerate(todo, start=1):
                try:
                    prepare_candidate_job_match(session, job_id, force_recompute=False)
                except Exception as exc:  # noqa: BLE001 -- one vacancy never stops the run
                    session.rollback()
                    failed += 1
                    print(f"FAILED: job {job_id} ({type(exc).__name__})")
                if done % 100 == 0 or done == len(todo):
                    rate = done / max(time.monotonic() - started, 1e-9)
                    print(f"progress {done}/{len(todo)} rate={rate:.1f}/s")
        print(f"APPLIED: {len(todo) - failed} analyses, {failed} failed")
        return 1 if failed else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(run())
