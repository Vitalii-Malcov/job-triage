"""Read-only liveness check of stored Bundesagentur postings (Stage 22, P0-2).

    python scripts/ba_liveness.py --database PATH [--job-id N ...] [--limit N]
        [--output results.jsonl] [--rate SECONDS] [--plan]

Opens the database read-only and never writes to it. For each selected
`bundesagentur` job (default: the `--limit` most recently seen) the
reference is the stored `jobs.source_reference`, else the one in the job's
own BA URL; see `app.services.ba_liveness` for the states. A 404 is CLOSED
only for a strictly valid BA reference; a source failure is SOURCE_ERROR.

`--plan` lists what would be requested, without any network request.
`--output FILE` appends one JSON line per job and makes a re-run resume:
jobs already in the file with a final state (anything but SOURCE_ERROR) are
skipped. One HTTP attempt (retries included) per `--rate` seconds (default
1.0, minimum 0.5); a rejected key (401/403) stops the run. The
key comes from BUNDESAGENTUR_API_KEY (environment or `.env`), never printed.
Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.collectors.ba_reference import ba_reference_from_url
from app.collectors.bundesagentur import SOURCE_NAME, is_api_key_configured
from app.core.config import get_settings
from app.db.models import JobRecord
from app.services.ba_liveness import (
    KEY_REJECTED,
    Liveness,
    LivenessResult,
    Pacer,
    check_reference,
)
from scripts.stage21_assess_vacancies import _readonly_engine

MIN_RATE_SECONDS = 0.5
MAX_LIMIT = 5000
TIMEOUT_SECONDS = 15.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only BA posting liveness check")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    parser.add_argument("--job-id", type=int, action="append", default=[])
    parser.add_argument("--limit", type=int, default=50, help="most recently seen N jobs")
    parser.add_argument("--output", default=None, help="append JSON lines here; resumes")
    parser.add_argument("--rate", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--plan", action="store_true", help="list only, no network request")
    return parser


def select_jobs(session: Session, job_ids: list[int], limit: int) -> list[tuple[int, str | None]]:
    """(job id, reference) for the selected BA jobs, newest first."""
    query = select(JobRecord.id, JobRecord.url, JobRecord.source_reference).where(
        JobRecord.source == SOURCE_NAME
    )
    if job_ids:
        query = query.where(JobRecord.id.in_(job_ids))
    else:
        query = query.order_by(JobRecord.last_seen_at.desc(), JobRecord.id.desc()).limit(limit)
    return [
        (row.id, row.source_reference or ba_reference_from_url(row.url))
        for row in session.execute(query).all()
    ]


def finished_job_ids(output: Path) -> set[int]:
    """Jobs with a final state in an earlier run's output (resume)."""
    if not output.is_file():
        return set()
    done: set[int] = set()
    for line in output.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("state") != Liveness.SOURCE_ERROR:
            if isinstance(row.get("job_id"), int):
                done.add(row["job_id"])
    return done


def _line(job_id: int, result: LivenessResult) -> str:
    return json.dumps(
        {
            "job_id": job_id,
            "reference": result.reference,
            "state": result.state.value,
            "http_status": result.http_status,
            "reason": result.reason,
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        sort_keys=True,
    )


async def check_all(
    jobs: list[tuple[int, str | None]],
    *,
    api_key: str,
    rate: float,
    output: Path | None,
    client: httpx.AsyncClient | None = None,
    sleep=asyncio.sleep,
    clock=time.monotonic,
) -> Counter:
    """One result line per job; one HTTP attempt per `rate` seconds (retries
    included). A rejected key stops the run: every further request would be
    rejected too."""
    counts: Counter = Counter()
    pacer = Pacer(rate, clock=clock, sleep=sleep)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        for job_id, reference in jobs:
            result = await check_reference(client, api_key, reference, pacer=pacer, sleep=sleep)
            counts[result.state.value] += 1
            line = _line(job_id, result)
            print(line)
            if output is not None:
                with output.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            if result.reason == KEY_REJECTED:
                print("STOPPED: the API key was rejected (401/403); nothing more requested")
                break
    finally:
        if own_client:
            await client.aclose()
    return counts


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.limit <= MAX_LIMIT:
        print(f"REFUSED: --limit must be 1..{MAX_LIMIT}", file=sys.stderr)
        return 2
    if args.rate < MIN_RATE_SECONDS:
        print(f"REFUSED: --rate must be at least {MIN_RATE_SECONDS} s", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else None
    if output is not None and output.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}:
        print("REFUSED: --output must not be a database file", file=sys.stderr)
        return 2
    try:
        engine = resolve_engine(args.database, args.database_url, writes=False)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    reader = _readonly_engine(engine)
    try:
        with Session(reader) as session:
            jobs = select_jobs(session, args.job_id, args.limit)
    finally:
        reader.dispose()
        engine.dispose()
    if output is not None:
        done = finished_job_ids(output)
        jobs = [job for job in jobs if job[0] not in done]
    if args.plan:
        for job_id, reference in jobs:
            print(f"job {job_id}: {reference or '(no reference -> INVALID_REFERENCE)'}")
        print(f"PLAN: {len(jobs)} jobs, no request made")
        return 0
    api_key = get_settings().bundesagentur_api_key
    if not is_api_key_configured(api_key):
        print("REFUSED: BUNDESAGENTUR_API_KEY is not configured", file=sys.stderr)
        return 2
    counts = asyncio.run(check_all(jobs, api_key=api_key, rate=args.rate, output=output))
    summary = " ".join(f"{state}={counts.get(state.value, 0)}" for state in Liveness)
    print(f"SUMMARY: {summary} (nothing written to the database)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
