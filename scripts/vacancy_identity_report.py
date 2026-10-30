"""Read-only dry run: which stored vacancies are the SAME posting (Stage 22 P1-2).

    python scripts/vacancy_identity_report.py --database PATH [--json]

The job fingerprint is (source, company, title, URL), so one Bundesagentur
posting stored under two URLs (BA page and the employer's `externeURL`, or a
changed URL) is two `jobs` rows. With Stage 22 P0-1 every BA row keeps its
full reference, which identifies the posting. This report groups rows by
(source, source_reference) and shows, per row, everything that points at it:

- with a foreign key (follow on delete): job_reference_tokens,
  job_commute_checks, job_fit_assessments (cache), telegram_vacancy_reviews
  (UNIQUE per job -- two reviews collide on a merge);
- WITHOUT a foreign key (a merge must rewrite or keep them by hand):
  applications, application_package_reviews, bewerbung_drafts,
  candidate_cv_drafts, candidate_job_matches, follow_up_proposals,
  gmail_message_analyses.matched_job_id, response_drafts.matched_job_id.

Nothing is merged or written. Per group it names the row a merge would keep
(the oldest) and the risks that forbid an automatic merge: an application or
approval-bearing row (applications, package reviews, Bewerbung drafts,
follow-ups) is never merged automatically -- approvals and application
history are never moved between vacancies. A real merge is a separate,
approved stage with its own backup.
Exit codes: 0 report written, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import UniqueConstraint, func, inspect, select, text
from sqlalchemy.orm import Session

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.db.models import ApplicationRecord, Base, JobRecord
from scripts.stage21_assess_vacancies import _readonly_engine

# (table, column, has a FK to jobs.id)
REFERENCES = (
    ("job_reference_tokens", "job_id", True),
    ("job_commute_checks", "job_id", True),
    ("job_fit_assessments", "job_id", True),
    ("telegram_vacancy_reviews", "job_id", True),
    ("applications", "job_id", False),
    ("application_package_reviews", "job_id", False),
    ("bewerbung_drafts", "job_id", False),
    ("candidate_cv_drafts", "job_id", False),
    ("candidate_job_matches", "job_id", False),
    ("follow_up_proposals", "job_id", False),
    ("gmail_message_analyses", "matched_job_id", False),
    ("response_drafts", "matched_job_id", False),
)
# Rows carrying application history or an operator approval: never merged
# automatically (approvals are never transferred between vacancies).
PROTECTED = frozenset(
    {"applications", "application_package_reviews", "bewerbung_drafts", "follow_up_proposals"}
)
# Derived cache rows: recomputed after a merge, never a merge risk.
DERIVED = frozenset({"job_fit_assessments", "job_reference_tokens"})


def _unique_column_sets(table_name: str, job_column: str) -> list[tuple[str, ...]]:
    """The OTHER columns of every unique constraint that includes the job
    column (empty tuple: the job column alone is unique)."""
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return []
    sets = []
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            names = [c.name for c in constraint.columns]
            if job_column in names:
                sets.append(tuple(n for n in names if n != job_column))
    return sets


def unique_conflicts(session: Session, job_ids: list[int]) -> list[str]:
    """Tables where moving the group's rows onto one job would violate a
    unique constraint (two rows equal on every other constrained column)."""
    conflicts = []
    for table, column, _fk in REFERENCES:
        if table in DERIVED:
            continue
        for others in _unique_column_sets(table, column):
            cols = ", ".join([column, *others])
            ids = ", ".join(str(int(i)) for i in job_ids)
            rows = session.execute(
                text(f"SELECT {cols} FROM {table} WHERE {column} IN ({ids})")  # noqa: S608
            ).all()
            seen: dict[tuple, int] = {}
            for row in rows:
                key = tuple(row[1:])
                if key in seen and seen[key] != row[0]:
                    conflicts.append(table)
                    break
                seen.setdefault(key, row[0])
    return sorted(set(conflicts))


def linked_applications(session: Session, source: str, reference: str) -> int:
    """Applications carrying the group's external identity, also with job_id NULL."""
    return int(
        session.execute(
            select(func.count())
            .select_from(ApplicationRecord)
            .where(
                ApplicationRecord.source == source,
                func.lower(ApplicationRecord.source_reference) == reference.lower(),
            )
        ).scalar_one()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only vacancy identity dry run")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _reference_counts(session: Session, job_ids: list[int]) -> dict[int, dict[str, int]]:
    counts: dict[int, dict[str, int]] = defaultdict(dict)
    present = set(inspect(session.connection()).get_table_names())
    for table, column, _fk in REFERENCES:
        if table not in present:
            continue
        rows = session.execute(
            text(
                f"SELECT {column}, count(*) FROM {table} "  # noqa: S608 -- fixed identifiers
                f"WHERE {column} IN ({', '.join(str(int(i)) for i in job_ids)}) GROUP BY {column}"
            )
        ).all()
        for job_id, n in rows:
            counts[int(job_id)][table] = int(n)
    return counts


def report(session: Session) -> dict:
    duplicated = (
        select(JobRecord.source, JobRecord.source_reference)
        .where(JobRecord.source_reference.is_not(None))
        .group_by(JobRecord.source, JobRecord.source_reference)
        .having(func.count() > 1)
    ).subquery()
    rows = session.execute(
        select(
            JobRecord.id,
            JobRecord.source,
            JobRecord.source_reference,
            JobRecord.url,
            JobRecord.company,
            JobRecord.title,
            JobRecord.first_seen_at,
        )
        .join(
            duplicated,
            (duplicated.c.source == JobRecord.source)
            & (duplicated.c.source_reference == JobRecord.source_reference),
        )
        .order_by(JobRecord.source, JobRecord.source_reference, JobRecord.id)
    ).all()
    counts = _reference_counts(session, [r.id for r in rows]) if rows else {}
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        groups[(row.source, row.source_reference)].append(row)
    result = []
    for (source, reference), members in groups.items():
        keep = min(members, key=lambda r: (r.first_seen_at, r.id))
        risks = set()
        protected = [m.id for m in members if PROTECTED & set(counts.get(m.id, {}))]
        applications = linked_applications(session, source, reference)
        if protected or applications:
            risks.add("APPLICATION_OR_APPROVAL")
        for table in unique_conflicts(session, [m.id for m in members]):
            risks.add("UNIQUE_CONFLICT:" + table)
        if len({m.company.casefold() for m in members}) > 1:
            risks.add("DIFFERENT_COMPANY_TEXT")
        if len({" ".join(m.title.casefold().split()) for m in members}) > 1:
            # The employer edited the posting (2026-10-30: "Junior Manager" ->
            # "Manager", "(Senior)" added): requirements may differ.
            risks.add("TITLE_CHANGED")
        result.append(
            {
                "source": source,
                "reference": reference,
                "keep": keep.id,
                "automatic_merge_allowed": not risks,
                "applications_by_reference": applications,
                "risks": sorted(risks),
                "rows": [
                    {
                        "job_id": m.id,
                        "url": m.url[:120],
                        "company": m.company[:60],
                        "title": m.title[:80],
                        "first_seen_at": m.first_seen_at.isoformat() if m.first_seen_at else None,
                        "references": counts.get(m.id, {}),
                    }
                    for m in members
                ],
            }
        )
    return {
        "groups": len(result),
        "rows": len(rows),
        "blocked_groups": sum(1 for g in result if not g["automatic_merge_allowed"]),
        "details": result,
    }


def _print(result: dict) -> None:
    print(
        f"groups={result['groups']} rows={result['rows']} "
        f"blocked_groups={result['blocked_groups']} (nothing merged)"
    )
    for group in result["details"]:
        risks = ", ".join(group["risks"]) or "none"
        print(f"{group['source']} {group['reference']}: keep job {group['keep']}; risks: {risks}")
        for row in group["rows"]:
            refs = ", ".join(f"{t}={n}" for t, n in sorted(row["references"].items())) or "-"
            print(f"  job {row['job_id']}: {row['company']} | {row['title']} | {refs}")


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        engine = resolve_engine(args.database, args.database_url, writes=False)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    reader = _readonly_engine(engine)
    try:
        with Session(reader) as session:
            result = report(session)
    finally:
        reader.dispose()
        engine.dispose()
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
