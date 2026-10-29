"""Night audit 2026-10-27: read-only pre-submit check of one package.

    python scripts/presubmit_check.py --database DB --package DIR \
        --company "Example GmbH" --role "Tester (m/w/d)" \
        [--attach cv=.playwright-mcp/upload/x/Lebenslauf.pdf] [--attach letter=...] \
        [--no-letter] [--missing-field "Zeugnisnote"] [--missing-field "Letzter Notenauszug"]
    python scripts/presubmit_check.py --database DB --package DIR --company C --role R --bind

`--bind` writes `_manifest.json` into the package (SHA-256 of each PDF and
its source text + the current profile version) -- run it right after the
PDFs were rendered and looked at; it never changes the documents. Without
it a package can be NEEDS_OPERATOR at best.

Contact e-mail/phone, profile version and the confirmed German level come
from the candidate profile in the database (opened read-only); they are
compared, never printed. Sibling packages (the other folders next to
--package) are used to detect a file from another vacancy's package.

Exit codes: 0 READY, 3 NEEDS_OPERATOR, 4 BLOCKED, 2 refused input. No
network, no browser, nothing written (except `_manifest.json` with --bind).
READY is never an instruction to submit -- the final click stays with the
candidate.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.db.models import CandidateProfileRecord
from app.services.job_fit_assessment import confirmed_german_level
from app.services.pre_submit_validation import Expectation, validate_package, write_manifest

EXIT = {"READY": 0, "NEEDS_OPERATOR": 3, "BLOCKED": 4}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only pre-submit package check")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    parser.add_argument("--package", required=True, help="the vacancy's package folder")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--attach", action="append", default=[], help="KIND=PATH (cv|letter)")
    parser.add_argument("--no-letter", action="store_true", help="the form takes no Anschreiben")
    parser.add_argument(
        "--bind", action="store_true", help="write _manifest.json binding PDFs to their source"
    )
    parser.add_argument(
        "--missing-field",
        action="append",
        default=[],
        help="a required portal field the candidate has no confirmed value for",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    package = Path(args.package)
    if not package.is_dir():
        print("REFUSED: --package is not a directory", file=sys.stderr)
        return 2
    attached = {}
    for item in args.attach:
        kind, _, path = item.partition("=")
        if kind not in ("cv", "letter") or not path:
            print("REFUSED: --attach must be cv=PATH or letter=PATH", file=sys.stderr)
            return 2
        attached[kind] = Path(path)
    try:
        engine = resolve_engine(args.database, args.database_url, writes=False)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        with sessionmaker(bind=engine)() as session:
            profile = session.scalars(select(CandidateProfileRecord).limit(1)).first()
            if profile is None:
                print("REFUSED: no candidate profile in this database", file=sys.stderr)
                return 2
            # Same trust rule as the fit assessment and CV generation: only a
            # directly asserted AND confirmed level (Codex round 12).
            confirmed = confirmed_german_level(session)
            german = None if confirmed == "UNKNOWN" else confirmed
            expected = Expectation(
                company=args.company,
                role=args.role,
                contact_email=profile.contact_email,
                contact_phone=profile.contact_phone,
                profile_version=profile.profile_version,
                german_level=german,
                letter_required=not args.no_letter,
            )
            current_version = profile.profile_version
    finally:
        engine.dispose()
    if args.bind:
        bound = write_manifest(package, current_version)
        print(f"BOUND: {sorted(bound['documents'])} to profile v{current_version}")
        for kind, reason in sorted(bound.get("refused", {}).items()):
            print(f"NOT BOUND: {kind} -- {reason}")
    report = validate_package(
        package,
        expected,
        attached=attached or None,
        sibling_packages=[p for p in package.parent.iterdir() if p.is_dir()],
        missing_required_fields=args.missing_field,
        current_profile_version=current_version,
    )
    print(f"PACKAGE {report.package}: {report.outcome}")
    for finding in report.findings:
        print(f"  {finding.severity:<5} {finding.code:<34} {finding.detail}")
    return EXIT[report.outcome]


if __name__ == "__main__":
    raise SystemExit(run())
