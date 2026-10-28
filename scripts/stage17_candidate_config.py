"""Stage 17B: operator CLI for the persistent candidate search configuration.

    python scripts/stage17_candidate_config.py --database DB show
    python scripts/stage17_candidate_config.py --database DB history
    python scripts/stage17_candidate_config.py --database DB apply --config FILE.json \
        --actor operator --reason "confirmed 27.10.2026"       # DRY RUN: prints the plan
    python scripts/stage17_candidate_config.py --database DB apply --config FILE.json \
        --actor ... --reason ... --expected-version N --apply  # writes it

(DB = D:/AI_agent/runtime/job_triage_runtime.db for production.)

The configuration file is operator-confirmed data (schema
`jobtriage.candidate-config/1`, see app/models/candidate_search.py) and is
kept OUTSIDE the repository: it holds contact details.

**Safe by default.** `apply` is a dry run unless `--apply` is given, and
`--apply` additionally needs `--expected-version` equal to the version the
dry run reported, so exactly the reviewed plan is written. A plan with any
REFUSED line writes nothing (a stored value from a more authoritative source
is never overwritten; skills/languages are never removed). Every write adds
one append-only `candidate_profile_revisions` row. The same database target
guard as the Stage 15 tracker applies (local file at the Alembic head, the
protected canonical database refused). Exit codes: 0 ok, 1 failure, 2 refused.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

try:
    from scripts.stage14a_send_readiness import UnsafeInputPathError, validate_local_json_path
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage14a_send_readiness import UnsafeInputPathError, validate_local_json_path
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.db.candidate_profile_repository import (
    CandidateProfileVersionConflictError,
    get_candidate_profile,
)
from app.db.candidate_search_repository import (
    CandidateConfigRefusedError,
    apply_config_plan,
    load_search_config,
    plan_config_change,
)
from app.db.models import CandidateProfileRevisionRecord
from app.models.candidate_search import CandidateConfigSpec


def _show(session: Session) -> int:
    config = load_search_config(session)
    if config is None:
        print("no candidate profile stored")
        return 1
    print(f"profile_version={config.profile_version}")
    print(f"location={config.location_city}, {config.location_country}")
    for name, value in config.search.model_dump(mode="json").items():
        trust = config.search_trust.get(name)
        origin = f"  [{trust.source}/{trust.confidence}]" if trust else ""
        print(f"search.{name}={json.dumps(value, ensure_ascii=False)}{origin}")
    for name, value in config.contact.model_dump().items():
        trust = config.contact_trust.get(name)
        origin = f"  [{trust.source}/{trust.confidence}]" if trust else ""
        print(f"contact.{name}={value}{origin}")
    return 0


def _history(session: Session) -> int:
    rows = session.scalars(
        select(CandidateProfileRevisionRecord).order_by(CandidateProfileRevisionRecord.id)
    ).all()
    if not rows:
        print("no revisions recorded")
    for row in rows:
        fields = ", ".join(json.loads(row.changed_fields_json))
        print(
            f"revision {row.id} | version {row.profile_version} | {row.recorded_at.isoformat()} "
            f"| {row.actor} | {row.source} | {row.reason} | {fields}"
        )
    return 0


def _load_spec(path: str) -> CandidateConfigSpec:
    validate_local_json_path(path)
    return CandidateConfigSpec.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def _apply(session: Session, args) -> int:
    try:
        spec = _load_spec(args.config)
    except (UnsafeInputPathError, OSError, ValueError, ValidationError) as exc:
        detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        print(f"REFUSED: configuration file is invalid: {detail}", file=sys.stderr)
        return 2
    profile = get_candidate_profile(session)
    if profile is None:
        print("REFUSED: no candidate profile stored", file=sys.stderr)
        return 2
    plan = plan_config_change(profile, spec)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}: profile_version={plan.base_version} source={spec.source}")
    for change in plan.changes:
        old = json.dumps(change.old, ensure_ascii=False, default=str)
        new = json.dumps(change.new, ensure_ascii=False, default=str)
        print(f"CHANGE {change.area}.{change.name}: {old} -> {new}")
    for refusal in plan.refusals:
        print(f"REFUSED {refusal}")
    if plan.refusals:
        print("nothing written: resolve every REFUSED line first", file=sys.stderr)
        return 2
    if plan.is_noop:
        print("NO_CHANGE: the stored configuration already matches the file")
        return 0
    if not args.apply:
        print(f"dry run only; re-run with --apply --expected-version {plan.base_version}")
        return 0
    if args.expected_version != plan.base_version:
        print(
            f"REFUSED: --expected-version {args.expected_version} does not match the "
            f"current profile_version {plan.base_version}",
            file=sys.stderr,
        )
        return 2
    try:
        version = apply_config_plan(session, plan, actor=args.actor, reason=args.reason)
    except (CandidateConfigRefusedError, CandidateProfileVersionConflictError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"APPLIED: profile_version={version} ({len(plan.changes)} change(s), revision recorded)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 17 candidate search configuration")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("history")
    apply = sub.add_parser("apply")
    apply.add_argument("--config", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--reason", required=True)
    apply.add_argument("--expected-version", type=int)
    apply.add_argument("--apply", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        engine = resolve_engine(
            args.database, args.database_url, writes=bool(getattr(args, "apply", False))
        )
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        with sessionmaker(bind=engine)() as session:
            if args.command == "show":
                return _show(session)
            if args.command == "history":
                return _history(session)
            return _apply(session, args)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(run())
