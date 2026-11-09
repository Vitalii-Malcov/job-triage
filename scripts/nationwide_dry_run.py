"""Rehearsal of the nationwide remote discovery on a COPY of the database.

    python -m scripts.nationwide_dry_run --db <copy.db> --json out.json [--max-new 400]

1. migrates the COPY to the current schema (Alembic, additive),
2. measures the funnel as it is today (``shortlist_e2e_report.analyse``),
3. runs the real nationwide search against the Bundesagentur Jobsuche API (read-only GET
   requests, paced, bounded) and stores what it finds in the COPY,
4. measures the funnel again and lists the vacancies that are new to the copy, with the
   assessment that decides about them.

WRITES TO THE GIVEN DATABASE ONLY. It refuses the production runtime database, a pilot or backup
database and any path that is not an existing regular file. It sends nothing: Telegram and every
outbound feature are switched off in the settings it builds, and the run never reaches Gmail,
SMTP or an employer.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import Settings
from app.core.database_target import DatabaseKind, classify_path
from app.db.models import JobRecord
from app.services.nationwide_discovery import run_nationwide_remote_discovery
from scripts.shortlist_e2e_report import analyse

REPO = Path(__file__).resolve().parents[1]


def migrate(db_path: Path) -> None:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.resolve().as_posix()}")
    command.upgrade(config, "head")


def dry_run_settings(max_new: int, key: str) -> Settings:
    return Settings(
        _env_file=None,
        bundesagentur_api_key=key,
        nationwide_remote_discovery_enabled=True,
        nationwide_remote_max_new_per_run=max_new,
        telegram_vacancy_feed_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
        min_job_score_to_notify=101,  # no alert can fire
        company_research_auto_max_per_run=0,
    )


def _funnel(rows: list[dict]) -> dict:
    full_remote = [r for r in rows if r["work_format"] == "FULL_REMOTE"]
    return {
        "rows": len(rows),
        "category": dict(Counter(r["category"] for r in rows)),
        "work_format": dict(Counter(r["work_format"] for r in rows)),
        "tier": dict(Counter(r["tier"] for r in rows)),
        "work_time": dict(Counter(r["criteria"].get("work_time", "-") for r in rows)),
        "seniority_blocked": sum(
            1 for r in rows if r["criteria"].get("entry_level") in {"MISMATCH", "BLOCKED"}
        ),
        "language_gap": dict(
            Counter(
                r["criteria"].get("language", "-")
                for r in rows
                if r["criteria"].get("language", "-") not in {"OK", "NOT_STATED", "-"}
            )
        ),
        "full_remote": len(full_remote),
        "full_remote_full_time": sum(
            1 for r in full_remote if r["criteria"].get("work_time") == "FULL_TIME"
        ),
        "primary_or_adjacent": sum(1 for r in rows if r["category"] in {"PRIMARY", "ADJACENT"}),
        "automatic_shortlist": sum(1 for r in rows if r["auto"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--max-new", type=int, default=400)
    parser.add_argument("--api-key", default="jobboerse-jobsuche")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print("REFUSED: --db must be an existing file", file=sys.stderr)
        return 2
    protected = {DatabaseKind.PRODUCTION_RUNTIME, DatabaseKind.PILOT, DatabaseKind.BACKUP}
    if classify_path(args.db) in protected:
        print("REFUSED: this rehearsal writes; use a copy", file=sys.stderr)
        return 2
    out = args.json.resolve()
    if out.exists() or out.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        print("REFUSED: --json must be a new, non-database file", file=sys.stderr)
        return 2

    migrate(args.db)
    settings = dry_run_settings(args.max_new, args.api_key)
    engine = create_engine(f"sqlite:///{args.db.resolve().as_posix()}")
    with Session(engine) as db:
        before = analyse(db, settings)
        before_ids = {r["id"] for r in before["rows"]}
        counters = asyncio.run(run_nationwide_remote_discovery(db, settings))
        after = analyse(db, settings)
        new_ids = {j for (j,) in db.execute(select(JobRecord.id)).all()} - before_ids
        new_rows = [r for r in after["rows"] if r["id"] in new_ids]
        links = {
            j.id: (j.url, j.company, j.location, j.source_reference)
            for j in db.scalars(select(JobRecord).where(JobRecord.id.in_(new_ids)))
        }
    for row in new_rows:
        row["url"], row["company"], row["location"], row["source_reference"] = links[row["id"]]
    report = {
        "run_counters": counters,
        "before": _funnel(before["rows"]),
        "after": _funnel(after["rows"]),
        "new_to_database": _funnel(new_rows),
        "before_summary": before["summary"],
        "after_summary": after["summary"],
        "new_rows": new_rows,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("run_counters", "new_to_database")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
