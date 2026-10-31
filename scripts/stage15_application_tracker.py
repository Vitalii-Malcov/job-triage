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
Record a service mail the candidate ALREADY sent (e.g. a phone correction);
the status, follow-up date and employer-contact time never change:
                 record-outbound --application-id ID [--application-id ID ...]
                     --message-id "<id@host>" --recipient ADDR --channel CH
                     --sent-at ISO8601 --kind K --actor A --reason R
                     [--sender ADDR] [--subject S] [--evidence TEXT] [--apply]
State changes:   transition ID STATUS --actor A --reason R [--occurred-at ISO8601] [--apply]
                 override   ID STATUS --actor A --reason R [--occurred-at ISO8601] [--apply]

**Safe by default.** `process-gmail`, `record-outbound`, `transition` and
`override` are DRY RUNS
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db.session_engine import enable_sqlite_foreign_keys
from app.domain.application_lifecycle import InvalidApplicationTransitionError
from app.models.application_lifecycle import (
    ApplicationChannel,
    ApplicationEventOrigin,
    ApplicationLifecycleStatus,
    OutboundMessageKind,
)
from app.services.application_tracker import (
    ApplicationConcurrencyError,
    ApplicationInputError,
    ApplicationNotFoundError,
    ApplicationTracker,
    outbound_correlation_key,
    validate_outbound_message,
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


def _refuse_kind_for_cli(path: Path | None, *, writes: bool, kind=None) -> None:
    """Stage 17A's database roles, applied to the CLI tools too (night audit
    2026-10-27: web/scheduler refused backups and pilots, but every CLI
    opened any SQLite file, so an `--apply` against a backup copy wrote into
    it silently). A backup is never written; a pilot is written only under
    DATABASE_ROLE=pilot; a test/development process never opens the
    production runtime database, not even to read it."""
    from app.core.database_target import (
        DatabaseKind,
        DatabaseRole,
        DatabaseTargetError,
        allowed_kinds,
        classify_path,
        parse_role,
    )

    try:
        role = parse_role(os.environ.get("DATABASE_ROLE"))
    except DatabaseTargetError as exc:
        raise TargetRefusedError(str(exc)) from None
    # `kind` is given for a server URL (Codex review: a PostgreSQL database
    # named *_backup / *pilot* bypassed the guard); a file is classified here.
    kind = kind if kind is not None else classify_path(path)
    if kind is DatabaseKind.PRODUCTION_RUNTIME and role in (
        DatabaseRole.TEST,
        DatabaseRole.DEVELOPMENT,
    ):
        raise TargetRefusedError(
            f"refusing the production runtime database under DATABASE_ROLE={role.value}"
        )
    if not writes:
        return
    if kind is DatabaseKind.BACKUP:
        raise TargetRefusedError(
            "refusing to write to a backup database (a backup is never a live database)"
        )
    if kind is DatabaseKind.PILOT and role is not DatabaseRole.PILOT:
        raise TargetRefusedError("writing to a pilot database needs DATABASE_ROLE=pilot")
    # Codex round 4: a stated role binds writes exactly like the web/scheduler
    # startup (production -> runtime file or PostgreSQL, pilot -> pilot only).
    if role is not None and kind not in allowed_kinds(role):
        raise TargetRefusedError(
            f"refusing to write a {kind.value} database under DATABASE_ROLE={role.value}"
        )


def resolve_engine(
    database: str | None, database_url: str | None, *, writes: bool = True
) -> Engine:
    """`writes` (fail-closed default True) is False only for invocations
    that never write (no --apply / --live)."""
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
        # Role guard by path identity before the existence check, so the
        # production runtime database is refused for its role on every
        # machine, whether or not the file exists there (CI has none).
        _refuse_kind_for_cli(path, writes=writes)
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
        from app.core.database_target import classify_database_url

        server_kind, _ = classify_database_url(database_url)
        _refuse_kind_for_cli(None, writes=writes, kind=server_kind)
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


def decision_summary(results, describe) -> list[str]:
    """Stage 19: the mails that need the operator, each with the next allowed
    step. The result lines above carry every field; this block is what an
    operator reads first. `describe(application_id)` -> "Company | Role"."""
    lines: list[str] = []
    for result in results:
        if result.operator_review is None:
            continue
        what = result.classification or "UNCLASSIFIED"
        if result.application_id is not None and result.proposed_status:
            lines.append(
                f"  mail {result.message_ref}: {what} for application {result.application_id} "
                f"({describe(result.application_id)}) -> proposed {result.proposed_status}"
            )
            lines.append(
                f"    next: transition {result.application_id} {result.proposed_status} "
                '--actor operator --reason "..." (dry run), then add --apply'
            )
        elif result.review_candidates:
            ids = ", ".join(f"{app_id} ({describe(app_id)})" for app_id in result.review_candidates)
            lines.append(
                f"  mail {result.message_ref}: {what}; names no vacancy, same sender as "
                f"application(s) {ids}"
            )
            lines.append("    next: read the mail, then record it on the right application")
        else:
            lines.append(
                f"  mail {result.message_ref}: {what}; correlation {result.correlation_status} "
                f"({result.reason})"
            )
            lines.append("    next: read the mail and decide which application it concerns")
    if lines:
        count = sum(1 for result in results if result.operator_review is not None)
        lines.insert(0, f"NEEDS YOUR DECISION ({count}):")
    else:
        lines.append("NEEDS YOUR DECISION: nothing")
    return lines


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
        **_outbound_detail(event),
    }


def _outbound_detail(event) -> dict:
    """Recipient, Message-ID and purpose of an OUTBOUND_MESSAGE row (only)."""
    if event.event_type.value != "OUTBOUND_MESSAGE":
        return {}
    meta = event.metadata or {}
    return {
        "kind": meta.get("kind"),
        "recipient": meta.get("recipient"),
        "channel": meta.get("channel"),
        "message_id": meta.get("message_id"),
    }


def applicant_addresses(settings) -> list[str]:
    from app.services.application_response_processor import (
        applicant_addresses as service_applicant_addresses,
    )

    return service_applicant_addresses(
        settings, Path(__file__).resolve().parents[1] / "application_identity.local.json"
    )


def _load_messages(db: Session, source: str, since: datetime, settings, mailbox: str = "all"):
    """(messages, per-mailbox fetch reports). `stored` reads the primary
    account's synced rows only and reports nothing."""
    from app.services.application_response_processor import (
        fetch_application_mailboxes,
        stored_inbound_messages,
    )

    if source == "imap":
        return asyncio.run(fetch_application_mailboxes(settings, since=since, selection=mailbox))
    return stored_inbound_messages(db, settings, since=since), []


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
    outbound = sub.add_parser("record-outbound")
    outbound.add_argument(
        "--application-id",
        type=int,
        action="append",
        required=True,
        help="repeat when one mail concerns several applications",
    )
    for flag in ("--message-id", "--recipient", "--actor", "--reason"):
        outbound.add_argument(flag, required=True)
    outbound.add_argument("--channel", required=True, choices=[c.value for c in ApplicationChannel])
    outbound.add_argument("--sent-at", required=True, help="ISO 8601 with UTC offset")
    outbound.add_argument("--kind", required=True, choices=[k.value for k in OutboundMessageKind])
    outbound.add_argument("--sender", default=None)
    outbound.add_argument("--subject", default=None)
    outbound.add_argument("--evidence", default=None)
    outbound.add_argument("--apply", action="store_true", help="write (default: dry run)")
    for name in ("transition", "override"):
        cmd = sub.add_parser(name)
        cmd.add_argument("application_id", type=int)
        cmd.add_argument("status", choices=[s.value for s in ApplicationLifecycleStatus])
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--reason", required=True)
        # Stage 22 P2: when it happened (e.g. the employer mail's Date), so a
        # receipt recorded later keeps its real time; default: now.
        cmd.add_argument(
            "--occurred-at",
            default=None,
            help="ISO 8601 with UTC offset; not in the future, not before the current status",
        )
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
        engine = resolve_engine(
            args.database, args.database_url, writes=bool(getattr(args, "apply", False))
        )
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
        elif args.command == "record-outbound":
            return _record_outbound(session, tracker, args)
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
            if not args.json:

                def describe(application_id: int) -> str:
                    app = tracker.get_application(session, application_id)
                    return f"{app.company_name} | {app.role_title}"

                for line in decision_summary(results, describe):
                    print(line)
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
    from app.services.application_duplicates import (
        VacancyView,
        assess_vacancy,
        classify_duplicate,
        verdict,
    )
    from app.services.application_guard import applied_job_descriptions

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
        location=job.location,
        description=job.description,
        source_reference=job.source_reference,
    )
    applications = [app_repo.to_application(r) for r in app_repo.iter_applications(session)]
    findings = assess_vacancy(
        vacancy, applications, applied_job_descriptions(session, applications)
    )
    result = verdict(findings)
    grade = classify_duplicate(vacancy, findings)
    payload = {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "verdict": result,
        "classification": grade,
        "findings": [
            {
                "application_id": f.application_id,
                "strength": f.strength,
                "signals": list(f.signals),
                "company": f.company,
                "role": f.role,
                "status": f.status,
                "detail": f.detail,
            }
            for f in findings
        ],
    }
    if args.json:
        _print(payload, True)
    else:
        print(f"JOB {job.id}: {job.company} | {job.title}")
        print(f"VERDICT: {result}")
        print(f"CLASSIFICATION: {grade}")
        for f in findings:
            print(
                f"  {f.strength} application {f.application_id} [{f.status}] "
                f"{f.company} | {f.role} ({', '.join(f.signals)})"
            )
            if f.detail:
                print(f"    {f.detail}")
    return 3 if result == "ALREADY_APPLIED" else 0


def _submitted_at(raw: str, now: datetime | None = None) -> datetime:
    """A real past instant: ISO 8601 with an explicit offset, never later
    than now (UTC-aware comparison, no tolerance)."""
    return _past_instant(raw, "--submitted-at", now)


def _past_instant(raw: str, flag: str, now: datetime | None = None) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise TargetRefusedError(f"{flag} must be ISO 8601") from None
    if value.tzinfo is None:
        raise TargetRefusedError(f"{flag} needs an explicit UTC offset (e.g. ...Z)")
    value = value.astimezone(UTC)
    if value > (now or datetime.now(UTC)):
        raise TargetRefusedError(f"{flag} lies in the future")
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
        merge_findings,
    )
    from app.services.application_guard import applied_job_descriptions

    source = _identity_part("--source", args.source)
    reference = _identity_part("--reference", args.reference)
    submitted_at = _submitted_at(args.submitted_at)
    job = None
    if args.job_id is not None:
        job = session.get(JobRecord, args.job_id)
        if job is None:
            raise ApplicationNotFoundError(f"job {args.job_id} does not exist")
        mismatches = job_association_mismatches(
            VacancyView(
                job.id,
                job.source,
                job.company,
                job.title,
                job.url,
                source_reference=job.source_reference,
            ),
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
        description=job.description if job is not None else None,
        source_reference=job.source_reference if job is not None else None,
    )
    applications = [app_repo.to_application(r) for r in app_repo.iter_applications(session)]
    findings = assess_vacancy(
        vacancy, applications, applied_job_descriptions(session, applications)
    )
    if args.destination_url and job is not None:
        findings = merge_findings(
            findings,
            assess_vacancy(VacancyView(None, source, "", "", args.destination_url), applications),
        )
    for f in findings:
        label = "ADVISORY" if f.advisory_only else f.strength
        print(
            f"{label}: application {f.application_id} [{f.status}] {f.company} | "
            f"{f.role} ({', '.join(f.signals)})"
        )
        if f.detail:
            print(f"    {f.detail}")
    # A finding whose only signal is the description overlap is advisory
    # (Stage 22): printed above, never a reason to refuse.
    unconfirmed = sorted(
        f.application_id
        for f in findings
        if f.strength in ("STRONG", "PROBABLE")
        and not f.advisory_only
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
        # Stage 19: the job's location when none is given, so the duplicate
        # guard's same-city agency-mirror check works for CLI submissions.
        location=args.location or (job.location if job is not None else None),
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


def _sent_at(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise TargetRefusedError("--sent-at must be ISO 8601") from None
    if value.tzinfo is None:
        raise TargetRefusedError("--sent-at needs an explicit UTC offset (e.g. ...Z)")
    return value


def _record_outbound(session: Session, tracker: ApplicationTracker, args) -> int:
    """Record ONE mail the candidate already sent as an OUTBOUND_MESSAGE
    event on every given application. Sends nothing; never changes a status.
    Every input check and every application lookup runs for the dry run and
    --apply alike, before any write. Idempotent per (Message-ID,
    application): an already recorded pair is reported and skipped.

    Each application is committed on its own. When a write fails part-way
    (a concurrent update or event-sequence collision), processing stops, the already committed
    applications stay recorded, and a summary of applied / failed /
    unprocessed ids is printed with exit 1; a re-run completes the rest."""
    from app.db import application_repository as app_repo

    ids = list(dict.fromkeys(args.application_id))
    message_id, recipient, sent_at = validate_outbound_message(
        message_id=args.message_id,
        recipient=args.recipient,
        sent_at=_sent_at(args.sent_at),
        actor=args.actor,
        reason=args.reason,
        sender=args.sender,
    )
    applications = [tracker.get_application(session, i) for i in ids]
    applied: list[int] = []
    for position, app in enumerate(applications):
        key = outbound_correlation_key(message_id, app.id)
        if app_repo.get_event_by_correlation_key(session, key) is not None:
            print(f"ALREADY_RECORDED: application {app.id} {app.company_name} (nothing written)")
            continue
        if not args.apply:
            print(
                f"DRY RUN: would record OUTBOUND_MESSAGE {args.kind} to {recipient} "
                f"{message_id} at {sent_at.isoformat()} on application {app.id} "
                f"{app.company_name} [{app.current_status.value} unchanged] (nothing written)"
            )
            continue
        try:
            outcome = tracker.record_outbound_message(
                session,
                app.id,
                message_id=message_id,
                recipient=recipient,
                channel=ApplicationChannel(args.channel),
                sent_at=sent_at,
                kind=OutboundMessageKind(args.kind),
                actor=args.actor,
                reason=args.reason,
                sender=args.sender,
                subject=args.subject,
                evidence_summary=args.evidence,
                related_application_ids=tuple(ids),
            )
        except (
            ApplicationConcurrencyError,
            ApplicationInputError,
            ApplicationNotFoundError,
            # A competing write can take the same event sequence; the
            # tracker has rolled back already (Codex P2 re-review).
            IntegrityError,
        ) as exc:
            unprocessed = [a.id for a in applications[position + 1 :]]
            print(f"FAILED: application {app.id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                f"PARTIAL: applied={applied} failed=[{app.id}] unprocessed={unprocessed}"
                " (applied rows stay recorded; re-run to complete the rest)",
                file=sys.stderr,
            )
            return 1
        if not outcome.duplicate:
            applied.append(app.id)
        state = "DUPLICATE" if outcome.duplicate else "APPLIED"
        print(
            f"{state}: application {outcome.application.id} "
            f"[{outcome.application.current_status.value}] OUTBOUND_MESSAGE "
            f"{args.kind} to {recipient}"
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
    occurred_at = datetime.now(UTC)
    if args.occurred_at is not None:
        occurred_at = _past_instant(args.occurred_at, "--occurred-at")
        if occurred_at < current.last_status_changed_at:
            # history stays in time order: a change never predates the
            # status it leaves
            raise TargetRefusedError(
                "--occurred-at lies before the current status was set "
                f"({current.last_status_changed_at.isoformat()})"
            )
    if not args.apply:
        print(
            f"DRY RUN: would {args.command} application {current.id} "
            f"{current.current_status.value} -> {target.value} at "
            f"{occurred_at.isoformat(timespec='seconds')} (nothing written)"
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
            occurred_at=occurred_at,
            chronological=args.occurred_at is not None,
        )
    else:
        outcome = tracker.override_status(
            session,
            current.id,
            target,
            actor=args.actor,
            reason=args.reason,
            occurred_at=occurred_at,
            chronological=args.occurred_at is not None,
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
