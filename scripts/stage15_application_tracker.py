"""Stage 15: operator CLI for the application tracker.

Every command needs an explicit database:

    python scripts/stage15_application_tracker.py --database D:/path/app.db list
    python scripts/stage15_application_tracker.py --database-url postgresql+psycopg://... list

Read commands:   list [--status S ...] | show ID | history ID      (add --json)
Gmail:           process-gmail [--source imap|stored] [--since YYYY-MM-DD] [--apply]
State changes:   transition ID STATUS --actor A --reason R [--apply]
                 override   ID STATUS --actor A --reason R [--apply]

**Safe by default.** `process-gmail`, `transition` and `override` are DRY RUNS
unless `--apply` is given; a dry run writes nothing. Gmail access is read-only
IMAP (`--source imap`) or the already-synced `gmail_messages` table
(`--source stored`, no network). Nothing here sends, drafts, replies, posts
or notifies.

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

from app.domain.application_lifecycle import InvalidApplicationTransitionError
from app.models.application_lifecycle import (
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
PROTECTED_DATABASE_SHA256 = frozenset(
    {"874d2cab6fc78efe3574dd1fb123c06e60662da48c32c240227313778f41e35d"}
)
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
        if _PROTECTED_PATH.search(str(path)):
            raise TargetRefusedError("Stage 13D databases are out of scope for this tool")
        if not path.is_file():
            raise TargetRefusedError("database file does not exist")
        if _sha256(path) in PROTECTED_DATABASE_SHA256:
            raise TargetRefusedError("refusing the protected Stage 14B canonical database")
        engine = create_engine(f"sqlite:///{path.as_posix()}", poolclass=NullPool)
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
    addresses = [settings.gmail_username] if settings.gmail_username else []
    identity_path = Path(__file__).resolve().parents[1] / "application_identity.local.json"
    if identity_path.is_file():
        from app.models.application_identity import load_application_identity

        identity = load_application_identity(identity_path)
        if identity and identity.email:
            addresses.append(identity.email)
    return addresses


def _load_messages(db: Session, source: str, since: datetime, settings):
    from app.services.application_response_processor import (
        fetch_inbound_messages,
        inbound_from_record,
    )

    if source == "imap":
        return asyncio.run(fetch_inbound_messages(settings, since=since))
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
    return [inbound_from_record(r) for r in records]


def process_gmail(
    db: Session,
    *,
    source: str,
    since: datetime,
    apply: bool,
    settings,
    only_application_ids: frozenset[int] | None = None,
):
    from app.services.application_response_processor import ApplicationResponseProcessor

    messages = _load_messages(db, source, since, settings)
    return ApplicationResponseProcessor().process(
        db,
        messages,
        apply=apply,
        applicant_addresses=applicant_addresses(settings),
        only_application_ids=only_application_ids,
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
    for name in ("show", "history"):
        sub.add_parser(name).add_argument("application_id", type=int)
    gmail = sub.add_parser("process-gmail")
    gmail.add_argument("--source", choices=("imap", "stored"), default="imap")
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
            rows = [_application_row(a) for a in tracker.list_applications(session, statuses)]
            _print(rows, args.json)
        elif args.command == "show":
            _print(
                _application_row(tracker.get_application(session, args.application_id)), args.json
            )
        elif args.command == "history":
            events = tracker.get_application_history(session, args.application_id)
            _print([_event_row(e) for e in events], args.json)
        elif args.command == "process-gmail":
            if settings is None:
                from app.core.config import Settings

                settings = Settings()
            results = process_gmail(
                session,
                source=args.source,
                since=_since(args.since),
                apply=args.apply,
                settings=settings,
            )
            print(f"MODE: {'APPLY' if args.apply else 'DRY RUN (nothing written)'}")
            _print([r.as_dict() for r in results], args.json)
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
