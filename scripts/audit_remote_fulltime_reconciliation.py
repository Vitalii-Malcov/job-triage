"""Read-only: reconcile "FULL_REMOTE + Vollzeit" counts across database snapshots.

    python scripts/audit_remote_fulltime_reconciliation.py \
        --db prod=copy_of_production.db --db scratch=nationwide_scratch.db --out report.md

Each database is opened with SQLite ``mode=ro`` (nothing can be written) and every vacancy is
assessed with the CURRENT code (`assess_job`, `classify`). For every snapshot it prints the counts
under each definition that was used in earlier reports, then one row per vacancy that is
FULL_REMOTE (triage work_format) or remote-compatible in ANY snapshot, with the work-time answer
from the text alone, from the structured BA flag and from the final criterion.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.agents.job_fit_signals import classify_work_time  # noqa: E402
from app.db.models import JobRecord  # noqa: E402
from app.services.job_fit_assessment import assess_job  # noqa: E402
from app.services.triage_category import classify  # noqa: E402


def snapshot(label: str, path: Path) -> list[dict]:
    engine = create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")
    rows: list[dict] = []
    with Session(engine) as db:
        for job in db.scalars(select(JobRecord).order_by(JobRecord.id)):
            a = assess_job(db, job.id)
            st = {c.name: c for c in a.criteria}
            triage = classify(a)
            text = f"{job.title}\n{job.location or ''}\n{job.description or ''}"
            key = job.source_reference or job.url
            rows.append(
                dict(
                    key=key,
                    snapshot=label,
                    id=job.id,
                    title=job.title,
                    company=job.company,
                    source=job.source,
                    region=job.workplace_region or job.location or "",
                    remote=st["remote"].status,
                    work_format=triage.work_format,
                    text_work_time=classify_work_time(text).mode,
                    flag=job.full_time_offered,
                    work_time=st["work_time"].status,
                    tier=a.fit_tier,
                    triage=f"{triage.category}/{triage.code}",
                )
            )
    engine.dispose()
    return rows


def counts(rows: list[dict]) -> dict[str, int]:
    r = rows
    return {
        "rows": len(r),
        "flag set": sum(x["flag"] is not None for x in r),
        "A work_format FULL_REMOTE": sum(x["work_format"] == "FULL_REMOTE" for x in r),
        "B  A and work_time FULL_TIME (funnel definition)": sum(
            x["work_format"] == "FULL_REMOTE" and x["work_time"] == "FULL_TIME" for x in r
        ),
        "C remote criterion COMPATIBLE": sum(x["remote"] == "COMPATIBLE" for x in r),
        "D  C and work_time FULL_TIME": sum(
            x["remote"] == "COMPATIBLE" and x["work_time"] == "FULL_TIME" for x in r
        ),
        "E  A and TEXT says FULL_TIME": sum(
            x["work_format"] == "FULL_REMOTE" and x["text_work_time"] == "FULL_TIME" for x in r
        ),
        "F  A and FULL_TIME only through the BA flag": sum(
            x["work_format"] == "FULL_REMOTE"
            and x["work_time"] == "FULL_TIME"
            and x["text_work_time"] != "FULL_TIME"
            for x in r
        ),
        "G  A and work_time UNKNOWN": sum(
            x["work_format"] == "FULL_REMOTE" and x["work_time"] == "UNKNOWN" for x in r
        ),
        "H  A and work_time PART_TIME/CONFLICT": sum(
            x["work_format"] == "FULL_REMOTE" and x["work_time"] in {"PART_TIME", "CONFLICT"}
            for x in r
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="append", required=True, help="label=path")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    # the report must never be written over an input: `mode=ro` only protects SQL, not the file
    inputs = {Path(spec.partition("=")[2]).resolve() for spec in args.db}
    for target in (args.out, args.json):
        if target is not None and (
            target.resolve() in inputs
            or target.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm"}
        ):
            print("REFUSED: --out/--json must not be a database file or an input", file=sys.stderr)
            return 2
    snaps = {}
    for spec in args.db:
        label, _, p = spec.partition("=")
        snaps[label] = snapshot(label, Path(p))
    lines = ["# FULL_REMOTE + Vollzeit reconciliation (read-only)", ""]
    labels = list(snaps)
    table = {lab: counts(snaps[lab]) for lab in labels}
    lines += ["| Definition | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)]
    for k in table[labels[0]]:
        lines.append(f"| {k} | " + " | ".join(str(table[lab][k]) for lab in labels) + " |")
    by_key = {lab: {} for lab in labels}  # a reference shared by several rows: keep the first id
    for lab in labels:
        for row in snaps[lab]:
            by_key[lab].setdefault(row["key"], row)
    union = {}
    for lab in labels:
        for key, row in by_key[lab].items():
            if row["work_format"] == "FULL_REMOTE" or row["remote"] == "COMPATIBLE":
                union.setdefault(key, {})[lab] = row
    lines += ["", f"## Union of FULL_REMOTE / remote-compatible vacancies: {len(union)}", ""]
    head = "| ref | title | employer | source | region | " + " | ".join(
        f"{lab}: id, format, work_time (text/flag), tier" for lab in labels
    )
    lines += [head + " | verdict |", "|" + "---|" * (6 + len(labels))]
    verdicts = Counter()
    for key, per in sorted(union.items(), key=lambda kv: min(v["id"] for v in kv[1].values())):
        first = next(iter(per.values()))
        cells = []
        for lab in labels:
            r = per.get(lab) or by_key[lab].get(key)
            cells.append(
                "absent"
                if r is None
                else f"{r['id']}, {r['work_format']}, {r['work_time']} ({r['text_work_time']}/"
                f"{r['flag']}), {r['tier']}"
            )
        best = [r for r in per.values() if r["work_format"] == "FULL_REMOTE"]
        if best and all(r["work_time"] == "FULL_TIME" for r in best):
            how = (
                "text" if any(r["text_work_time"] == "FULL_TIME" for r in best) else "BA flag only"
            )
            verdict = f"CONFIRMED FULL_REMOTE + FULL_TIME ({how})"
        elif best:
            verdict = "FULL_REMOTE, work time " + "/".join(sorted({r["work_time"] for r in best}))
        else:
            verdict = "remote-compatible only (not FULL_REMOTE)"
        absent = [lab for lab in labels if key not in by_key[lab]]
        if absent:
            verdict += "; absent in " + ",".join(absent)
        verdicts[verdict] += 1
        lines.append(
            f"| {key} | {first['title'][:45]} | {first['company'][:25]} | {first['source']} | "
            f"{first['region'][:18]} | " + " | ".join(cells) + f" | {verdict} |"
        )
    lines += ["", "## Verdict counts", ""] + [f"* {v}: {k}" for k, v in verdicts.most_common()]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(table, indent=1), encoding="utf-8")
    print("\n".join(lines[: 4 + len(table[labels[0]])]))
    print("verdicts:", dict(verdicts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
