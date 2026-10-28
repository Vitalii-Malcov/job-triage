"""Stage 16: operator CLI for Telegram operator control.

Every command needs an explicit database (same target guard as Stage 15:
the protected Stage 14B canonical database, Stage 13D paths and
`ai_agent-db-1` are refused; the database must be at the Alembic head):

    python scripts/stage16_telegram_operator.py --database D:/path/app.db config
    python scripts/stage16_telegram_operator.py --database D:/path/app.db preview 12
    python scripts/stage16_telegram_operator.py --database D:/path/app.db send 12 [--live]
    python scripts/stage16_telegram_operator.py --database D:/path/app.db decisions 12
    python scripts/stage16_telegram_operator.py --database D:/path/app.db notifications [--live]
    python scripts/stage16_telegram_operator.py --database D:/path/app.db override 12 SKIP \
        --reason "..." [--apply]

**Dry run by default.** `preview`, `send` without `--live`, `notifications`
without `--live` and `override` without `--apply` open the database
READ-ONLY, never contact Telegram and write nothing. `send --live` and
`notifications --live` send to the configured operator chat ONLY (the chat
id is never printed, only masked) and require
TELEGRAM_OPERATOR_CONTROL_ENABLED=true. Nothing here submits an
application, sends an e-mail or contacts an employer.

Exit codes: 0 ok, 1 failure, 2 refused input.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    from stage15_application_tracker import TargetRefusedError, resolve_engine

from app.db import telegram_operator_repository as repo
from app.models.telegram_operator import OperatorDecision
from app.services.application_tracker import (
    ApplicationInputError,
    ApplicationNotFoundError,
    ApplicationTracker,
)

NO_EMPLOYER_ACTION = "No employer-facing action occurs (no submission, no e-mail, no portal)."


def mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "missing"
    return f"configured (***{value[-2:]})" if len(value) > 2 else "configured (***)"


def configured(value: str) -> str:
    return "configured" if (value or "").strip() else "missing"


def _read_only(session: Session) -> None:
    """Make this session's transaction refuse writes at the database level."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.execute(text("PRAGMA query_only = ON"))
    elif dialect == "postgresql":
        session.execute(text("SET TRANSACTION READ ONLY"))


def _print(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, default=str, ensure_ascii=False, indent=1))
        return
    if isinstance(payload, list):
        for item in payload:
            print(" | ".join(f"{k}={v}" for k, v in item.items()))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _keyboard_lines(markup: dict | None) -> list[str]:
    if not markup:
        return ["(no buttons)"]
    return [
        "  ".join(f"[{b['text']}] ({b['callback_data']}, {len(b['callback_data'])}B)" for b in row)
        for row in markup["inline_keyboard"]
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 16 Telegram operator control")
    target = parser.add_argument_group("database target")
    target.add_argument("--database", help="local SQLite database file")
    target.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config")
    sub.add_parser("preview").add_argument("application_id", type=int)
    send = sub.add_parser("send")
    send.add_argument("application_id", type=int)
    send.add_argument("--live", action="store_true", help="really send ONE card (default: dry run)")
    sub.add_parser("decisions").add_argument("application_id", type=int)
    notes = sub.add_parser("notifications")
    notes.add_argument("--since", default=None, help="YYYY-MM-DD (UTC); default: 7 days")
    notes.add_argument("--limit", type=int, default=20)
    notes.add_argument("--live", action="store_true", help="really send (default: dry run)")
    override = sub.add_parser("override")
    override.add_argument("application_id", type=int)
    override.add_argument("decision", choices=["APPROVE", "SKIP"])
    override.add_argument("--reason", required=True)
    override.add_argument("--apply", action="store_true", help="write (default: dry run)")
    return parser


def _since(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC) - timedelta(days=7)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise TargetRefusedError("--since must be YYYY-MM-DD") from None


def _is_live(args) -> bool:
    return bool(getattr(args, "live", False) or getattr(args, "apply", False))


def _utf8_console() -> None:
    """Cards contain emoji; a Windows console defaults to cp1252."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def run(argv: list[str] | None = None, *, settings=None, transport=None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    if settings is None:
        from app.core.config import Settings

        settings = Settings()
    try:
        engine = resolve_engine(args.database, args.database_url, writes=_is_live(args))
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    session = sessionmaker(engine)()
    try:
        if not _is_live(args):
            _read_only(session)
        return _dispatch(session, settings, args, transport)
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (ApplicationNotFoundError, ApplicationInputError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _dispatch(session: Session, settings, args, transport) -> int:
    from app.services import telegram_operator_control as control
    from app.services import telegram_operator_notifications as notifications

    tracker = ApplicationTracker()
    if args.command == "config":
        _print(
            {
                "telegram_bot_token": configured(settings.telegram_bot_token),
                "telegram_chat_id": configured(settings.telegram_chat_id),
                "telegram_operator_user_id": configured(settings.telegram_operator_user_id),
                "telegram_operator_control_enabled": settings.telegram_operator_control_enabled,
            },
            args.json,
        )
        return 0
    if args.command in ("preview", "send"):
        preview = control.preview_card(session, args.application_id, tracker=tracker)
        live = args.command == "send" and args.live
        print("MODE: " + ("LIVE SEND (one card)" if live else "DRY RUN (nothing sent or written)"))
        print(f"DESTINATION: operator chat {mask(settings.telegram_chat_id)}")
        print(f"APPLICATION: #{preview.application_id} status={preview.status}")
        print(f"TARGET: {preview.target_key} existing_card={preview.existing_card_state}")
        print(f"APPROVE OFFERED: {preview.approve_offered}")
        if not preview.package.ready:
            print(f"PACKAGE_NOT_READY: {', '.join(preview.package.reasons)}")
        print(f"DB WILL CHANGE: {'card delivery row only' if live else 'no'}")
        print(NO_EMPLOYER_ACTION)
        print(f"--- message ({len(preview.message.text)} chars) ---")
        print(preview.message.text)
        print("--- buttons ---")
        print("\n".join(_keyboard_lines(preview.message.reply_markup)))
        if not live:
            return 0
        if transport is None:
            from app.services.telegram_operator_transport import HttpTelegramOperatorTransport

            transport = HttpTelegramOperatorTransport(settings)
        result = asyncio.run(
            control.send_card(session, settings, args.application_id, transport, tracker=tracker)
        )
        print(f"RESULT: {result.code.value} card_id={result.card_id}")
        return 0 if result.code is control.CardSendCode.SENT else 1
    if args.command == "decisions":
        tracker.get_application(session, args.application_id)
        rows = [
            {
                "id": d.id,
                "decision": d.decision,
                "actor": d.actor,
                "card_id": d.card_id,
                "package": d.review_package_id,
                "supersedes": d.supersedes_decision_id,
                "reason": d.reason,
                "created_at": d.created_at,
            }
            for d in repo.list_decisions_for_application(session, args.application_id)
        ]
        _print(rows, args.json)
        return 0
    if args.command == "notifications":
        since = _since(args.since)
        if not 1 <= args.limit <= 100:
            raise TargetRefusedError("--limit must be between 1 and 100")
        print("MODE: " + ("LIVE SEND" if args.live else "DRY RUN (nothing sent or written)"))
        print(f"DESTINATION: operator chat {mask(settings.telegram_chat_id)}")
        print(NO_EMPLOYER_ACTION)
        if not args.live:
            pending = notifications.find_pending_notifications(
                session, recorded_after=since, limit=args.limit
            )
            for item in pending:
                print(f"--- event {item.event_id} (application {item.application_id}) ---")
                print(item.text)
            print(f"PENDING: {len(pending)}")
            return 0
        if transport is None:
            from app.services.telegram_operator_transport import HttpTelegramOperatorTransport

            transport = HttpTelegramOperatorTransport(settings)
        stats = asyncio.run(
            notifications.deliver_lifecycle_notifications(
                session, settings, transport, recorded_after=since, limit=args.limit
            )
        )
        print(
            f"RESULT: sent={stats.sent} failed={stats.failed} uncertain={stats.uncertain} "
            f"duplicate={stats.duplicate}"
        )
        return 0 if not stats.failed and not stats.uncertain else 1
    # override
    decision = OperatorDecision(args.decision)
    if not args.apply:
        plan = control.plan_override(session, args.application_id, decision, tracker=tracker)
        print(f"DRY RUN: {plan.code.value}: {plan.detail} (nothing written)")
        print(NO_EMPLOYER_ACTION)
        return 0 if plan.code is control.OverrideCode.OVERRIDDEN else 1
    result = control.override_decision(
        session, args.application_id, decision, reason=args.reason, tracker=tracker
    )
    print(f"{result.code.value}: {result.detail} decision_id={result.decision_id}")
    print(NO_EMPLOYER_ACTION)
    return 0 if result.code is control.OverrideCode.OVERRIDDEN else 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
