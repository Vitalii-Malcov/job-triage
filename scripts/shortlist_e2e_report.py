"""End-to-end shortlist report on a COPY of the database (final integration check).

    python -m scripts.shortlist_e2e_report --db <copy.db> [--prepare-drafts] [--json out.json]

Runs the real automatic path over every vacancy: stored status/recommendation eligibility,
the candidate match (prepared where missing), the match-score threshold, the live fit
assessment with its target-role / region / seniority criteria, and the shortlist gate. With
``--prepare-drafts`` it also runs the real ``prepare_shortlist_drafts`` (deterministic CV and
Bewerbung drafts, nothing sent) for the vacancies that would be shortlisted.

WRITES TO THE GIVEN DATABASE (matches, drafts). It therefore refuses the production runtime
database and any path that is not an existing regular file. Use a copy made with
scripts/backup_sqlite.py. Nothing is sent: there is no SMTP, Gmail or Telegram call in this path.
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database_target import DatabaseKind, classify_path
from app.db.models import CandidateJobMatchRecord, JobRecord
from app.services.automation_shortlist import prepare_shortlist_drafts
from app.services.candidate_preparation import prepare_candidate_job_match
from app.services.collector_runner import TouchedJob
from app.services.job_fit_assessment import assess_job
from app.services.shortlist_gate import automatic_block_reason, evaluate

ELIGIBLE_STATUSES = frozenset({"NEW", "SAVED"})
ELIGIBLE_RECOMMENDATIONS = frozenset({"APPLY", "MAYBE"})

# Deliberately independent of app.agents.target_role: an observer must not share the rule it checks.
ADMIN_SUPPORT = re.compile(
    r"administrator|administratorin|systemadmin|netzwerkadmin|it-?admin|it-?support|"
    r"helpdesk|help desk|service ?desk|first[- ]level|second[- ]level|1st level|2nd level|"
    r"anwendungssupport|application support|client support|techn\w* support|hotline|"
    r"systemintegrat|systemelektronik",
    re.IGNORECASE,
)
JUNIOR = re.compile(r"junior|werkstudent|trainee|berufseinsteiger|entry|absolvent", re.IGNORECASE)
DEV = re.compile(
    r"python|backend|back-end|software|fastapi|django|api|entwickl|developer|engineer",
    re.IGNORECASE,
)


def _first_reason(assessment) -> str:
    reasons = list(assessment.tier_reasons)
    return reasons[0][:90] if reasons else "-"


def analyse(db: Session, settings: Settings) -> dict:
    jobs = db.scalars(select(JobRecord).order_by(JobRecord.id)).all()
    matches_before = db.scalar(select(func.count(CandidateJobMatchRecord.id))) or 0
    tiers: Counter = Counter()
    gate: Counter = Counter()
    blocked_reasons: Counter = Counter()
    rows = []
    for job in jobs:
        assessment = assess_job(db, job.id)
        tiers[assessment.fit_tier] += 1
        verdict = evaluate(assessment, liveness="UNKNOWN")
        gate[verdict.verdict] += 1
        eligible_pre = (
            job.status in ELIGIBLE_STATUSES and job.recommendation in ELIGIBLE_RECOMMENDATIONS
        )
        match_score = None
        if eligible_pre:
            match = prepare_candidate_job_match(db, job.id, force_recompute=False)
            match_score = match.overall_score if match is not None else None
        block = automatic_block_reason(assessment)
        auto = (
            eligible_pre
            and match_score is not None
            and match_score >= settings.automation_shortlist_min_match_score
            and block is None
        )
        if eligible_pre and block is not None:
            blocked_reasons[block] += 1
        rows.append(
            {
                "id": job.id,
                "title": job.title,
                "status": job.status,
                "recommendation": job.recommendation,
                "eligible_pre": eligible_pre,
                "match_score": match_score,
                "tier": assessment.fit_tier,
                "block": block,
                "gate": verdict.verdict,
                "auto": auto,
                "has_open_points": bool(verdict.questions),
                "reason": _first_reason(assessment),
            }
        )
    db.commit()
    eligible = [r for r in rows if r["eligible_pre"]]
    auto = [r for r in rows if r["auto"]]
    summary = {
        "jobs_total": len(rows),
        "matches_before": matches_before,
        "matches_after": db.scalar(select(func.count(CandidateJobMatchRecord.id))) or 0,
        "fit_tier_all": dict(tiers),
        "gate_verdict_all": dict(gate),
        "eligible_by_status_and_recommendation": len(eligible),
        "eligible_without_match_score": sum(1 for r in eligible if r["match_score"] is None),
        "eligible_below_match_threshold": sum(
            1
            for r in eligible
            if r["match_score"] is not None
            and r["match_score"] < settings.automation_shortlist_min_match_score
        ),
        "eligible_blocked_by_fit_tier": dict(blocked_reasons),
        "eligible_tier_needs_operator_or_stretch": sum(
            1 for r in eligible if r["tier"] in {"NEEDS_OPERATOR", "STRETCH"}
        ),
        "would_enter_automatic_shortlist": len(auto),
        "min_match_score": settings.automation_shortlist_min_match_score,
        "max_per_run": settings.automation_shortlist_max_per_run,
        "junior_dev_titles_total": sum(
            1 for r in rows if JUNIOR.search(r["title"]) and DEV.search(r["title"])
        ),
        "junior_dev_titles_eligible": sum(
            1 for r in eligible if JUNIOR.search(r["title"]) and DEV.search(r["title"])
        ),
        "junior_dev_titles_in_automatic_shortlist": sum(
            1 for r in auto if JUNIOR.search(r["title"]) and DEV.search(r["title"])
        ),
        "admin_support_titles_total": sum(1 for r in rows if ADMIN_SUPPORT.search(r["title"])),
        "admin_support_titles_eligible_pre": sum(
            1 for r in eligible if ADMIN_SUPPORT.search(r["title"])
        ),
        "VIOLATION_admin_support_in_automatic_shortlist": [
            r["id"] for r in auto if ADMIN_SUPPORT.search(r["title"])
        ],
        "VIOLATION_not_suitable_in_automatic_shortlist": [
            r["id"] for r in auto if r["tier"] == "NOT_SUITABLE"
        ],
    }
    return {"summary": summary, "rows": rows}


def prepare_drafts(db: Session, rows: list[dict], settings: Settings) -> dict:
    touched = [
        TouchedJob(
            job_id=r["id"],
            status=r["status"],
            recommendation=r["recommendation"],
            score=db.get(JobRecord, r["id"]).score,
        )
        for r in rows
        if r["eligible_pre"]
    ]
    result = asyncio.run(prepare_shortlist_drafts(db, touched_jobs=touched, settings=settings))
    return {"status": result["status"], "counters": result["counters"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--prepare-drafts", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print("REFUSED: --db must be an existing file", file=sys.stderr)
        return 2
    if classify_path(args.db) is DatabaseKind.PRODUCTION_RUNTIME:
        print(
            "REFUSED: this report writes; use a copy, never the production database",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(f"sqlite:///{args.db.resolve().as_posix()}")
    settings = Settings(_env_file=None)
    with Session(engine) as db:
        report = analyse(db, settings)
        if args.prepare_drafts:
            report["drafts"] = prepare_drafts(db, report["rows"], settings)
    engine.dispose()
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        json.dumps(
            report["summary"] | ({"drafts": report["drafts"]} if "drafts" in report else {}),
            ensure_ascii=False,
            indent=1,
        )
    )
    violations = (
        report["summary"]["VIOLATION_admin_support_in_automatic_shortlist"]
        or report["summary"]["VIOLATION_not_suitable_in_automatic_shortlist"]
    )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
