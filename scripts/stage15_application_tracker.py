"""Stage 15: operator CLI for the application tracker.

Every command needs an explicit database:

    python scripts/stage15_application_tracker.py --database D:/path/app.db list
    python scripts/stage15_application_tracker.py --database-url postgresql+psycopg://... list

Read commands:   list [--status S ...] | show ID | history ID      (add --json)
Before applying: check-job JOB_ID   (duplicate check against every application)
Gmail:           process-gmail [--source imap|stored] [--mailbox primary|secondary|all]
                               [--since YYYY-MM-DD] [--apply]
Record a submission the operator made by hand (portal / e-mail / job board):
                 record-submission --company C --role R --source S --reference REF
                     --channel CH --submitted-at ISO8601 --actor A --evidence TEXT
                     [--job-id N] [--location L] [--destination-url U]
                     [--employer-domain D ...] [--confirm-distinct-from ID ...] [--apply]
State changes:   transition ID STATUS --actor A --reason R [--apply]
                 override   ID STATUS --actor A --reason R [--apply]

**Safe by default.** `process-gmail`, `transition` and `override` are DRY RUNS
unless `--apply` is given; a dry run writes nothing. `check-job` is read-only
and exits 3 when the vacancy was already applied to. Gmail access is read-only
IMAP (`--source imap`) or the already-synced `gmail_messages` table
(`--source stored`, no network). `--mailbox all` (the default) reads every
configured applicant mailbox: the primary GMAIL_* account and the
XING_MAILBOX_* account, from which applications have also been sent.
Nothing here sends, drafts, replies, posts or notifies.

**Target guard.** The database must be at the application's Alembic head (no
auto-upgrade), a SQLite path must be a plain local file, and the protected
Stage 14B canonical database (by SHA-256) and anything under a Stage 13D
path are refused outright. Exit codes: 0 ok, 1 failure, 2 refused input.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db.session_engine import enable_sqlite_foreign_keys
from app.domain.application_lifecycle import InvalidApplicationTransitionError
from app.models.application_lifecycle import (
    ApplicationChannel,
    ApplicationEventOrigin,
    ApplicationLifecycleStatus,
)
from app.services.application_tracker import (
    ApplicationInputError,
    ApplicationNotFoundError,
    ApplicationTracker,
)

try:
    from scripts.stage14a_send_readiness import UnsafeInputPathError, validate_local_json_path
except ModuleNotFoundError:  # run as `python scripts/...`
    from stage14a_send_readiness import UnsafeInputPathError, validate_local_json_path

# Databases this tool must never open: the frozen Stage 14B canonical DB.
# Codex S15 P1: the PATH/FILE IDENTITY is the primary guard (the file's hash
# changes the moment anyone writes it, so a hash-only guard expires); the
# historical SHA-256 stays as defense in depth for byte-identical copies.
PROTECTED_DATABASE_PATHS: tuple[Path, ...] = (
    Path(__file__).resolve().parents[1] / "stage14b_canonical_v12.db",
)
PROTECTED_DATABASE_SHA256 = frozenset(
    {"874d2cab6fc78efe3574dd1fb123c06e60662da48c32c240227313778f41e35d"}
)


def _identity(path: Path) -> str:
    """Windows-safe comparable identity: absolute, `..` collapsed, symlinks
    and junctions resolved, separators and case normalized."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _spellings(path: Path, sep: str = os.sep) -> tuple[Path, ...]:
    """`path`, plus -- where `\\` is NOT a separator (POSIX) -- the same
    spelling with `\\` read as `/`. On POSIX `canon\\x.db` is a literal file
    name; refusing it as well keeps the guard identical on every OS."""
    raw = str(path)
    if sep != "\\" and "\\" in raw:
        return (path, Path(raw.replace("\\", "/")))
    return (path,)


def is_protected_database(path: Path) -> bool:
    """True when `path` names a protected database by any spelling or alias:
    same normalized real path (relative, `..`, `/` vs `\\` on every OS, case,
    symlink, junction), or the same file (`samefile`: hard links)."""
    targets = {_identity(spelling) for spelling in _spellings(path)}
    for protected in PROTECTED_DATABASE_PATHS:
        if _identity(protected) in targets:
            return True
        try:
            if path.exists() and protected.exists() and os.path.samefile(path, protected):
                return True
        except OSError:
            continue
    return False


_PROTECTED_PATH = re.compile(r"stage[-_ ]?13d", re.IGNORECASE)
_PROTECTED_URL = re.compile(r"ai_agent-db-1", re.IGNORECASE)


class TargetRefusedError(Exception):
    """The database target is not acceptable (exit 2)."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_engine(database: str | None, database_url: str | None) -> Engine:
    if bool(database) == bool(database_url):
        raise TargetRefusedError("give exactly one of --database or --database-url")
    if database:
        try:
            validate_local_json_path(database)
        except UnsafeInputPathError as exc:
            raise TargetRefusedError(str(exc)) from None
        path = Path(os.path.abspath(database))
        # Identity first: refused before the file is even opened or hashed.
        if is_protected_database(path):
            raise TargetRefusedError("refusing the protected Stage 14B canonical database")
        if _PROTECTED_PATH.search(_identity(path)) or _PROTECTED_PATH.search(str(path)):
            raise TargetRefusedError("Stage 13D databases are out of scope for this tool")
        if not path.is_file():
            raise TargetRefusedError("database file does not exist")
        if _sha256(path) in PROTECTED_DATABASE_SHA256:
            raise TargetRefusedError("refusing the protected Stage 14B canonical database")
        engine = create_engine(f"sqlite:///{path.as_posix()}", poolclass=NullPool)
        enable_sqlite_foreign_keys(engine)
    else:
        url = make_url(database_url)
        if not url.drivername.startswith("postgresql"):
            raise TargetRefusedError("--database-url must be a PostgreSQL URL")
        if _PROTECTED_URL.search(database_url) or _PROTECTED_PATH.search(database_url):
            raise TargetRefusedError("this database is out of scope for this tool")
        engine = create_engine(url, poolclass=NullPool)
    _require_head(engine)
    return engine


def _require_head(engine: Engine) -> None:
    from app.services.readiness import expected_alembic_heads

    heads = tuple(expected_alembic_heads())
    try:
        with engine.connect() as connection:
            revisions = tuple(
                row[0]
                for row in connection.execute(text("SELECT version_num FROM alembic_version"))
            )
    except Exception:
        raise TargetRefusedError("database has no readable alembic_version table") from None
    if revisions != heads:
        raise TargetRefusedError(
            f"database is at {', '.join(revisions) or 'no revision'}, application head is "
            f"{', '.join(heads)}; upgrade it explicitly first (no auto-upgrade)"
        )


def _json_default(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _print(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, default=_json_default, ensure_ascii=False, indent=1))
        return
    if isinstance(payload, list):
        for item in payload:
            print(" | ".join(f"{k}={_json_default(v)}" for k, v in item.items()))
    else:
        for key, value in payload.items():
            print(f"{key}: {_json_default(value)}")


def _application_row(app) -> dict:
    return {
        "id": app.id,
        "status": app.current_status.value,
        "company": app.company_name,
        "role": app.role_title,
        "location": app.location,
        "reference": app.source_reference,
        "submitted_at": app.submitted_at,
        "received_at": app.received_at,
        "last_change": app.last_status_changed_at,
        "next_followup_at": app.next_followup_at,
        "followup_required": app.followup_required,
    }


def _event_row(event) -> dict:
    return {
        "seq": event.sequence,
        "type": event.event_type.value,
        "old": event.old_status.value if event.old_status else None,
        "new": event.new_status.value if event.new_status else None,
        "origin": event.origin.value,
        "actor": event.actor,
        "occurred_at": event.occurred_at,
        "recorded_at": event.recorded_at,
        "reason": event.reason,
        "summary": event.evidence_summary,
        "correlation": (event.correlation_key or "")[:24] or None,
    }


def applicant_addresses(settings) -> list[str]:
    """The applicant's own addresses: mail FROM any of them is never an
    employer response (all monitored mailboxes plus the identity file)."""
    addresses = [a for a in (settings.gmail_username, settings.xing_mailbox_username) if a]
    identity_path = Path(__file__).resolve().parents[1] / "application_identity.local.json"
    if identity_path.is_file():
        from app.models.application_identity import load_application_identity

        identity = load_application_identity(identity_path)
        if identity and identity.email:
            addresses.append(identity.email)
    return addresses


def _load_messages(db: Session, source: str, since: datetime, settings, mailbox: str = "all"):
    """(messages, per-mailbox fetch reports). `stored` reads the primary
    account's synced rows only and reports nothing."""
    from app.services.application_response_processor import (
        fetch_application_mailboxes,
        inbound_from_record,
    )

    if source == "imap":
        return asyncio.run(fetch_application_mailboxes(settings, since=since, selection=mailbox))
    from sqlalchemy import select

    from app.db.models import GmailMessageRecord
    from app.providers.email.base import normalize_account_key

    account = normalize_account_key(settings.gmail_username or "")
    records = db.scalars(
        select(GmailMessageRecord)
        .where(
            GmailMessageRecord.account_key == account,
            GmailMessageRecord.direction == "INBOUND",
            GmailMessageRecord.provider_arrival_at >= since,
        )
        .order_by(GmailMessageRecord.provider_arrival_at, GmailMessageRecord.id)
    ).all()
    return [inbound_from_record(r) for r in records], []


def process_gmail(
    db: Session,
    *,
    source: str,
    since: datetime,
    apply: bool,
    settings,
    only_application_ids: frozenset[int] | None = None,
    mailbox: str = "all",
    reports: list | None = None,
):
    """Process inbound mail; per-mailbox fetch reports are appended to
    `reports` when given (a failed mailbox never hides the others)."""
    from app.services.application_response_processor import (
        ApplicationResponseProcessor,
        monitored_account_keys,
    )

    messages, fetch_reports = _load_messages(db, source, since, settings, mailbox)
    if reports is not None:
        reports.extend(fetch_reports)
    return ApplicationResponseProcessor().process(
        db,
        messages,
        apply=apply,
        applicant_addresses=applicant_addresses(settings),
        only_application_ids=only_application_ids,
        monitored_account_keys=monitored_account_keys(settings),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 15 application tracker (local only)")
    target = parser.add_argument_group("database target")
    target.add_argument("--database", help="local SQLite database file")
    target.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list")
    lst.add_argument(
        "--status", action="append", choices=[s.value for s in ApplicationLifecycleStatus]
    )
    lst.add_argument("--limit", type=int, default=200, help="page size (1-500, default 200)")
    lst.add_argument("--after-id", type=int, default=None, help="continue after this id")
    for name in ("show", "history"):
        sub.add_parser(name).add_argument("application_id", type=int)
    sub.add_parser("check-job").add_argument("job_id", type=int)
    record = sub.add_parser("record-submission")
    for flag in ("--company", "--role", "--source", "--reference", "--actor", "--evidence"):
        record.add_argument(flag, required=True)
    record.add_argument("--channel", required=True, choices=[c.value for c in ApplicationChannel])
    record.add_argument("--submitted-at", required=True, help="ISO 8601 with UTC offset")
    record.add_argument("--job-id", type=int, default=None)
    record.add_argument("--location", default=None)
    record.add_argument("--destination-url", default=None)
    record.add_argument("--employer-domain", action="append", default=[])
    record.add_argument(
        "--confirm-distinct-from",
        type=int,
        action="append",
        default=[],
        help="application id the operator verified is a DIFFERENT vacancy",
    )
    record.add_argument("--apply", action="store_true", help="write (default: dry run)")
    gmail = sub.add_parser("process-gmail")
    gmail.add_argument("--source", choices=("imap", "stored"), default="imap")
    gmail.add_argument(
        "--mailbox",
        choices=("primary", "secondary", "all"),
        default="all",
        help="imap only: primary=GMAIL_*, secondary=XING_MAILBOX_*, all=both (default)",
    )
    gmail.add_argument("--since", default=None, help="YYYY-MM-DD (UTC); default: 30 days")
    gmail.add_argument("--apply", action="store_true", help="write results (default: dry run)")
    for name in ("transition", "override"):
        cmd = sub.add_parser(name)
        cmd.add_argument("application_id", type=int)
        cmd.add_argument("status", choices=[s.value for s in ApplicationLifecycleStatus])
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--reason", required=True)
        cmd.add_argument("--apply", action="store_true", help="write (default: dry run)")
    return parser


def _since(raw: str | None) -> datetime:
    if raw is None:
        from datetime import timedelta

        return datetime.now(UTC) - timedelta(days=30)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise TargetRefusedError("--since must be YYYY-MM-DD") from None


def run(argv: list[str] | None = None, *, settings=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        engine = resolve_engine(args.database, args.database_url)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    tracker = ApplicationTracker()
    session = sessionmaker(engine)()
    try:
        if args.command == "list":
            statuses = tuple(ApplicationLifecycleStatus(s) for s in args.status or ()) or None
            if not 1 <= args.limit <= 500:
                raise TargetRefusedError("--limit must be between 1 and 500")
            page = tracker.list_applications(
                session, statuses, limit=args.limit, after_id=args.after_id
            )
            _print([_application_row(a) for a in page], args.json)
            if len(page) == args.limit:
                # Never truncate silently: tell the operator how to continue.
                print(
                    f"MORE: page is full; continue with --after-id {page[-1].id}", file=sys.stderr
                )
        elif args.command == "show":
            _print(
                _application_row(tracker.get_application(session, args.application_id)), args.json
            )
        elif args.command == "history":
            events = tracker.get_application_history(session, args.application_id)
            _print([_event_row(e) for e in events], args.json)
        elif args.command == "check-job":
            return _check_job(session, args)
        elif args.command == "record-submission":
            return _record_submission(session, tracker, args)
        elif args.command == "process-gmail":
            if settings is None:
                from app.core.config import Settings

                settings = Settings()
            reports: list = []
            results = process_gmail(
                session,
                source=args.source,
                since=_since(args.since),
                apply=args.apply,
                settings=settings,
                mailbox=args.mailbox,
                reports=reports,
            )
            print(f"MODE: {'APPLY' if args.apply else 'DRY RUN (nothing written)'}")
            for report in reports:
                print(
                    f"MAILBOX: {report.label} fetched={report.fetched} skipped={report.skipped}"
                    f" pages={report.pages} deadline_exceeded={report.deadline_exceeded}"
                    f" error={report.error} complete={report.complete}",
                    file=sys.stderr,
                )
            if args.source == "imap" and not reports:
                print("WARNING: no applicant mailbox is configured", file=sys.stderr)
            _print([r.as_dict() for r in results], args.json)
            # Exit 1 = at least one mailbox was NOT read completely (fetch
            # error, deadline, or messages left unread by the per-run cap,
            # page limit or as unreadable); results above are still valid
            # for what WAS read, and a re-run is idempotent.
            if not all(r.complete for r in reports):
                print(
                    "INCOMPLETE: at least one mailbox was not read completely"
                    " (see MAILBOX lines); re-run later or narrow --since",
                    file=sys.stderr,
                )
                return 1
        else:
            return _change_status(session, tracker, args)
        return 0
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (
        ApplicationNotFoundError,
        ApplicationInputError,
        InvalidApplicationTransitionError,
    ) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def _check_job(session: Session, args) -> int:
    """Read-only duplicate check of one collected vacancy. Exit 0 when no
    STRONG duplicate exists, 3 when the vacancy was already applied to."""
    from app.db import application_repository as app_repo
    from app.db.models import JobRecord
    from app.services.application_duplicates import VacancyView, assess_vacancy, verdict

    job = session.get(JobRecord, args.job_id)
    if job is None:
        raise ApplicationNotFoundError(f"job {args.job_id} does not exist")
    vacancy = VacancyView(
        job_id=job.id,
        source=job.source,
        company=job.company,
        title=job.title,
        url=job.url,
        fingerprint=job.fingerprint,
    )
    applications = [app_repo.to_application(r) for r in app_repo.iter_applications(session)]
    findings = assess_vacancy(vacancy, applications)
    result = verdict(findings)
    payload = {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "verdict": result,
        "findings": [
            {
                "application_id": f.application_id,
                "strength": f.strength,
                "signals": list(f.signals),
                "company": f.company,
                "role": f.role,
                "status": f.status,
            }
            for f in findings
        ],
    }
    if args.json:
        _print(payload, True)
    else:
        print(f"JOB {job.id}: {job.company} | {job.title}")
        print(f"VERDICT: {result}")
        for f in findings:
            print(
                f"  {f.strength} application {f.application_id} [{f.status}] "
                f"{f.company} | {f.role} ({', '.join(f.signals)})"
            )
    return 3 if result == "ALREADY_APPLIED" else 0


def _submitted_at(raw: str, now: datetime | None = None) -> datetime:
    """A real past instant: ISO 8601 with an explicit offset, never later
    than now (UTC-aware comparison, no tolerance)."""
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise TargetRefusedError("--submitted-at must be ISO 8601") from None
    if value.tzinfo is None:
        raise TargetRefusedError("--submitted-at needs an explicit UTC offset (e.g. ...Z)")
    value = value.astimezone(UTC)
    if value > (now or datetime.now(UTC)):
        raise TargetRefusedError("--submitted-at lies in the future")
    return value


def _identity_part(flag: str, value: str) -> str:
    """Source/reference are stored exactly as given (a provider reference is
    never rewritten), so an empty or whitespace-padded value is refused
    instead of silently trimmed into a different identity."""
    if not value or not value.strip():
        raise TargetRefusedError(f"{flag} must not be empty")
    if value != value.strip():
        raise TargetRefusedError(f"{flag} must not start or end with whitespace")
    return value


def submission_correlation_key(source: str, reference: str) -> str:
    """Injective identity of one manual submission: SHA-256 over the JSON
    array [source, reference], so no separator inside either value can make
    two different pairs collide ("a:b"+"c" vs "a"+"b:c").

    The `submission-v2:` prefix can never equal a legacy key
    (`email-submission:...` / `portal-submission:...`, written for the
    first real applications); those records stay as they are and are found
    -- like every record -- by the structural (source, source_reference)
    lookup that runs before any write."""
    canonical = json.dumps([source, reference], ensure_ascii=False, separators=(",", ":"))
    return "submission-v2:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_submission(session: Session, tracker: ApplicationTracker, args) -> int:
    """Record ONE submission the operator already made by hand, as
    SUBMITTED. Never submits anything itself. Idempotent: the same
    source + reference is reported as ALREADY_RECORDED and nothing is
    written. Any other STRONG duplicate (same job row, same employer and
    role, same URL ...) or PROBABLE one (identical title elsewhere: agency
    mirror) refuses with exit 3 unless every such application id is passed
    to --confirm-distinct-from. With --job-id, the claimed company, role,
    source and reference must match that collected job (exit 4 otherwise);
    every check runs identically for the dry run and --apply, before any
    write."""
    from app.db import application_repository as app_repo
    from app.db.models import JobRecord
    from app.services.application_duplicates import (
        VacancyView,
        assess_vacancy,
        job_association_mismatches,
    )

    source = _identity_part("--source", args.source)
    reference = _identity_part("--reference", args.reference)
    submitted_at = _submitted_at(args.submitted_at)
    job = None
    if args.job_id is not None:
        job = session.get(JobRecord, args.job_id)
        if job is None:
            raise ApplicationNotFoundError(f"job {args.job_id} does not exist")
        mismatches = job_association_mismatches(
            VacancyView(job.id, job.source, job.company, job.title, job.url),
            company=args.company,
            role=args.role,
            source=source,
            reference=reference,
            destination_url=args.destination_url,
        )
        if mismatches:
            print(
                f"REFUSED: job {job.id} ({job.source}) {job.company} | {job.title} does not "
                f"match this submission ({', '.join(mismatches)} differ); check --job-id, "
                "or omit it when the submission has no collected job",
                file=sys.stderr,
            )
            return 4
    existing = tracker.find_by_source_reference(session, source, reference)
    if existing is not None:
        print(
            f"ALREADY_RECORDED: application {existing.id} [{existing.current_status.value}] "
            f"{existing.company_name} | {existing.role_title} (nothing written)"
        )
        return 0
    vacancy = VacancyView(
        job_id=args.job_id,
        source=source,
        company=args.company,
        title=args.role,
        url=job.url if job is not None else args.destination_url,
        fingerprint=None,
    )
    applications = [app_repo.to_application(r) for r in app_repo.iter_applications(session)]
    findings = assess_vacancy(vacancy, applications)
    if args.destination_url and job is not None:
        findings += [
            f
            for f in assess_vacancy(
                VacancyView(None, source, "", "", args.destination_url), applications
            )
            if f.application_id not in {g.application_id for g in findings}
        ]
    for f in findings:
        print(
            f"{f.strength}: application {f.application_id} [{f.status}] {f.company} | "
            f"{f.role} ({', '.join(f.signals)})"
        )
    unconfirmed = sorted(
        f.application_id
        for f in findings
        if f.strength in ("STRONG", "PROBABLE")
        and f.application_id not in set(args.confirm_distinct_from)
    )
    if unconfirmed:
        print(
            "REFUSED: already applied? STRONG/PROBABLE duplicate of application(s) "
            f"{', '.join(map(str, unconfirmed))}; verify by hand, then pass "
            "--confirm-distinct-from for each id if it is really a different vacancy",
            file=sys.stderr,
        )
        return 3
    channel = ApplicationChannel(args.channel)
    if not args.apply:
        print(
            f"DRY RUN: would record {args.company} | {args.role} ({source} "
            f"{reference}, {channel.value}) as SUBMITTED at {submitted_at.isoformat()} "
            "(nothing written)"
        )
        return 0
    outcome = tracker.create_application(
        session,
        company_name=args.company,
        role_title=args.role,
        source=source,
        source_reference=reference,
        application_channel=channel,
        initial_status=ApplicationLifecycleStatus.SUBMITTED,
        origin=ApplicationEventOrigin.OPERATOR,
        actor=args.actor,
        occurred_at=submitted_at,
        location=args.location,
        destination_url=args.destination_url,
        employer_domains=tuple(args.employer_domain),
        job_id=args.job_id,
        correlation_key=submission_correlation_key(source, reference),
        evidence_hash=hashlib.sha256(args.evidence.encode("utf-8")).hexdigest(),
        evidence_summary=args.evidence,
        reason="Operator-recorded manual submission",
    )
    state = "ALREADY_RECORDED" if outcome.duplicate else "APPLIED"
    print(
        f"{state}: application {outcome.application.id} "
        f"[{outcome.application.current_status.value}] next follow-up "
        f"{outcome.application.next_followup_at}"
    )
    return 0


def _change_status(session: Session, tracker: ApplicationTracker, args) -> int:
    from app.domain.application_lifecycle import validate_transition

    target = ApplicationLifecycleStatus(args.status)
    current = tracker.get_application(session, args.application_id)
    if args.command == "transition":
        validate_transition(current.current_status, target)
    elif target == current.current_status:
        raise ApplicationInputError("override target equals the current status")
    if not args.apply:
        print(
            f"DRY RUN: would {args.command} application {current.id} "
            f"{current.current_status.value} -> {target.value} (nothing written)"
        )
        return 0
    if args.command == "transition":
        outcome = tracker.transition_application(
            session,
            current.id,
            target,
            origin=ApplicationEventOrigin.OPERATOR,
            actor=args.actor,
            reason=args.reason,
            occurred_at=datetime.now(UTC),
        )
    else:
        outcome = tracker.override_status(
            session, current.id, target, actor=args.actor, reason=args.reason
        )
    print(
        f"APPLIED: application {outcome.application.id} {outcome.previous_status} -> "
        f"{outcome.application.current_status.value} (event {outcome.event.sequence})"
    )
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
