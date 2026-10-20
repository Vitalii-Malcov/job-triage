"""Stage 16: lifecycle notifications (matrix U-X), the bot handler wiring,
the CLI dry-run, the migration, and the no-employer-outbound proof."""

import ast
import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.services.telegram_bot as bot
from app.db.base import Base
from app.db.models import (
    ApplicationEventRecord,
    ApplicationOperatorDecisionRecord,
    ApplicationRecord,
    TelegramLifecycleNotificationRecord,
    TelegramOperatorCardRecord,
)
from app.db.session_engine import enable_sqlite_foreign_keys
from app.models.application_lifecycle import ApplicationEventOrigin
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.telegram_operator import OperatorAction
from app.services import telegram_operator_control as control
from app.services import telegram_operator_notifications as notifications
from app.services.application_response_processor import (
    ApplicationResponseProcessor,
    InboundMessage,
)
from app.services.application_tracker import ApplicationTracker
from app.services.telegram import TelegramSendOutcome
from app.services.telegram_operator_render import (
    LifecycleNotificationView,
    build_callback_data,
    render_lifecycle_notification,
)
from tests.stage15_fixtures import (
    ALD_RECEIPT_DATE_HEADER,
    ALD_RECEIPT_DISPLAY_NAME,
    ALD_RECEIPT_FROM,
    ALD_RECEIPT_SUBJECT,
    ALD_RECEIPT_TEXT,
)
from tests.stage16_fixtures import (
    NOW,
    OPERATOR_CHAT,
    STRANGER_CHAT,
    FakeTransport,
    build_target,
    operator_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SINCE = NOW - timedelta(days=3650)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage16n.db'}", poolclass=NullPool)
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine)
    engine.dispose()


@pytest.fixture()
def db(factory):
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def settings():
    return operator_settings()


def _ald_application(db, *, status=S.SUBMITTED) -> int:
    return (
        ApplicationTracker()
        .create_application(
            db,
            company_name="ALD Vacuum Technologies GmbH",
            role_title="Backend Developer (m/w/div.)",
            source="bundesagentur",
            source_reference="20385-2347902-S",
            application_channel="EMPLOYER_PORTAL",
            initial_status=status,
            origin=ApplicationEventOrigin.IMPORT,
            actor="stage16-test",
            employer_domains=("ald-vt.de",),
            occurred_at=ALD_RECEIPT_DATE_HEADER - timedelta(minutes=5),
        )
        .application.id
    )


def _inbound(subject: str, body: str, *, key: str) -> InboundMessage:
    return InboundMessage(
        account_key="applicant@example.invalid",
        message_key=f"mid:{key}@ald-vt.de",
        message_id_header=f"<{key}@ald-vt.de>",
        reply_to_ids=(),
        from_address=ALD_RECEIPT_FROM,
        from_display_name=ALD_RECEIPT_DISPLAY_NAME,
        to_addresses=("applicant@example.invalid",),
        subject=subject,
        body=body,
        has_html=False,
        occurred_at=ALD_RECEIPT_DATE_HEADER,
    )


def _deliver(db, settings, transport=None):
    transport = transport or FakeTransport()
    stats = asyncio.run(
        notifications.deliver_lifecycle_notifications(db, settings, transport, recorded_after=SINCE)
    )
    return stats, transport


def _ledger_count(db) -> int:
    return db.scalar(select(func.count()).select_from(TelegramLifecycleNotificationRecord))


# --- notifications (U-X) -----------------------------------------------------------


class TestLifecycleNotifications:
    def test_v_received_renders_correctly_from_the_real_ald_receipt(self, db, settings):
        app_id = _ald_application(db)
        results = ApplicationResponseProcessor().process(
            db,
            [_inbound(ALD_RECEIPT_SUBJECT, ALD_RECEIPT_TEXT, key="receipt")],
            apply=True,
            applicant_addresses=["applicant@example.invalid"],
        )
        assert results[0].transition_applied
        stats, transport = _deliver(db, settings)
        assert stats.sent == 1
        assert transport.sent[0] == (
            "📩 ALD Vacuum Technologies GmbH\n"
            "Backend Developer (m/w/div.)\n\n"
            "Status changed:\n"
            "SUBMITTED → RECEIVED\n\n"
            "Source:\n"
            "Gmail receipt confirmation\n\n"
            "No action required.",
            None,
        )
        assert db.get(ApplicationRecord, app_id).current_status == S.RECEIVED.value

    def test_u_same_lifecycle_event_is_notified_once(self, db, settings):
        app_id = _ald_application(db)
        ApplicationTracker().record_receipt(
            db, app_id, origin=ApplicationEventOrigin.OPERATOR, actor="t"
        )
        first, transport = _deliver(db, settings)
        second, transport = _deliver(db, settings, transport)
        assert (first.sent, second.sent) == (1, 0)
        assert len(transport.sent) == 1
        assert _ledger_count(db) == 1
        # Replaying the same event id directly is also a no-op.
        event_id = first.event_ids[0]
        state = asyncio.run(
            notifications.deliver_notification(db, event_id, "x", app_id, transport)
        )
        assert state is None and len(transport.sent) == 1

    def test_w_interview_renders_correctly(self):
        rendered = render_lifecycle_notification(
            LifecycleNotificationView(
                company="Acme GmbH",
                role="Backend Developer",
                event_type="STATUS_CHANGED",
                origin="GMAIL",
                previous_status="RECEIVED",
                new_status="INTERVIEW",
                current_status="INTERVIEW",
                classification="INTERVIEW_INVITATION",
            )
        )
        assert rendered.splitlines()[0] == "📅 Acme GmbH"
        assert "RECEIVED → INTERVIEW" in rendered
        assert "Gmail interview invitation" in rendered
        assert "Nothing is sent automatically." in rendered

    def test_x_rejection_email_is_notified_without_rejecting(self, db, settings):
        app_id = _ald_application(db)
        ApplicationTracker().record_receipt(
            db, app_id, origin=ApplicationEventOrigin.OPERATOR, actor="t"
        )
        results = ApplicationResponseProcessor().process(
            db,
            [
                _inbound(
                    "Ihre Bewerbung als Backend Developer",
                    "Leider müssen wir Ihre Bewerbung absagen.",
                    key="rejection",
                )
            ],
            apply=True,
            applicant_addresses=["applicant@example.invalid"],
        )
        assert results[0].operator_confirmation_required
        stats, transport = _deliver(db, settings)
        assert stats.sent == 2  # the operator-recorded receipt, then the rejection mail
        message = transport.sent[1][0]
        assert message.startswith("⚠️ ALD Vacuum Technologies GmbH")
        assert "Possible rejection detected" in message
        assert "Status unchanged: RECEIVED" in message
        assert "Operator confirmation required" in message
        assert transport.sent[1][1] is None  # no confirmation buttons (Stage 17)
        assert db.get(ApplicationRecord, app_id).current_status == S.RECEIVED.value
        statuses = db.scalars(select(ApplicationEventRecord.new_status)).all()
        assert S.REJECTED.value not in statuses

    def test_non_notifiable_events_are_ignored(self, db, settings):
        target = build_target(db)
        tracker = ApplicationTracker()
        tracker.transition_application(
            db, target.application_id, S.APPROVED, origin=ApplicationEventOrigin.OPERATOR, actor="t"
        )
        stats, transport = _deliver(db, settings)
        assert stats.sent == 0 and transport.sent == [] and _ledger_count(db) == 0

    def test_failed_notification_is_retried_and_uncertain_is_not(self, db, settings):
        app_id = _ald_application(db)
        ApplicationTracker().record_receipt(
            db, app_id, origin=ApplicationEventOrigin.OPERATOR, actor="t"
        )
        failed, _ = _deliver(db, settings, FakeTransport([TelegramSendOutcome.FAILED]))
        retried, transport = _deliver(db, settings)
        assert (failed.failed, retried.sent) == (1, 1)
        assert _ledger_count(db) == 1

        second = build_target(db, reference="20385-0000009-S", status=S.MATCHED)
        tracker = ApplicationTracker()
        for status in (S.PACKAGE_READY, S.APPROVED, S.SUBMITTED, S.RECEIVED):
            tracker.transition_application(
                db, second.application_id, status, origin=ApplicationEventOrigin.OPERATOR, actor="t"
            )
        uncertain, _ = _deliver(db, settings, FakeTransport([TelegramSendOutcome.UNCERTAIN]))
        again, transport = _deliver(db, settings)
        assert uncertain.uncertain == 1
        assert again.sent == 0 and transport.sent == []

    def test_notification_failure_never_changes_the_application(self, db, settings):
        app_id = _ald_application(db)
        ApplicationTracker().record_receipt(
            db, app_id, origin=ApplicationEventOrigin.OPERATOR, actor="t"
        )
        before = (
            db.get(ApplicationRecord, app_id).version,
            db.scalar(select(func.count()).select_from(ApplicationEventRecord)),
        )
        _deliver(db, settings, FakeTransport([TelegramSendOutcome.FAILED] * 3))
        db.expire_all()
        after = (
            db.get(ApplicationRecord, app_id).version,
            db.scalar(select(func.count()).select_from(ApplicationEventRecord)),
        )
        assert before == after

    def test_disabled_flag_sends_nothing(self, db):
        app_id = _ald_application(db)
        ApplicationTracker().record_receipt(
            db, app_id, origin=ApplicationEventOrigin.OPERATOR, actor="t"
        )
        stats, transport = _deliver(db, operator_settings(telegram_operator_control_enabled=False))
        assert stats.sent == 0 and transport.sent == [] and _ledger_count(db) == 0

    def test_find_pending_is_read_only(self, db):
        app_id = _ald_application(db)
        ApplicationTracker().record_receipt(
            db, app_id, origin=ApplicationEventOrigin.OPERATOR, actor="t"
        )
        pending = notifications.find_pending_notifications(db, recorded_after=SINCE)
        assert [p.application_id for p in pending] == [app_id]
        assert _ledger_count(db) == 0


# --- bot handler ---------------------------------------------------------------------


def _update(data: str, *, chat_id: int, user_id: int, chat_type: str = "private"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    query = update.callback_query
    query.id = "cbq-handler"
    query.data = data
    query.from_user.id = user_id
    query.message.message_id = 55
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return update


class TestBotHandler:
    @pytest.fixture()
    def wired(self, monkeypatch, factory, settings):
        monkeypatch.setattr(bot, "SessionLocal", factory)
        monkeypatch.setattr(bot, "get_settings", lambda: settings)
        db = factory()
        target = build_target(db)
        result = asyncio.run(
            control.send_card(db, settings, target.application_id, FakeTransport(), now=NOW)
        )
        token = db.get(TelegramOperatorCardRecord, result.card_id).callback_token
        db.close()
        return {"token": token, "application_id": target.application_id}

    def test_handler_is_registered(self, settings):
        application = bot.build_application(operator_settings(telegram_bot_token="1:abc"))
        callbacks = [h.callback for group in application.handlers.values() for h in group]
        assert bot.on_operator_callback in callbacks

    def test_approve_edits_the_card(self, factory, wired):
        update = _update(
            build_callback_data(OperatorAction.APPROVE, wired["token"]),
            chat_id=int(OPERATOR_CHAT),
            user_id=int(OPERATOR_CHAT),
        )
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        asyncio.run(bot.on_operator_callback(update, context))
        update.callback_query.answer.assert_awaited_once()
        assert "Nothing was sent" in update.callback_query.answer.await_args.args[0]
        update.callback_query.edit_message_text.assert_awaited_once()
        context.bot.send_message.assert_not_awaited()
        db = factory()
        try:
            assert db.get(ApplicationRecord, wired["application_id"]).current_status == "APPROVED"
        finally:
            db.close()

    def test_details_go_to_the_configured_chat_only(self, wired):
        update = _update(
            build_callback_data(OperatorAction.DETAILS, wired["token"]),
            chat_id=int(OPERATOR_CHAT),
            user_id=int(OPERATOR_CHAT),
        )
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        asyncio.run(bot.on_operator_callback(update, context))
        context.bot.send_message.assert_awaited_once()
        assert context.bot.send_message.await_args.kwargs["chat_id"] == OPERATOR_CHAT

    def test_stranger_gets_silence_and_nothing_changes(self, factory, wired):
        update = _update(
            build_callback_data(OperatorAction.APPROVE, wired["token"]),
            chat_id=int(STRANGER_CHAT),
            user_id=int(STRANGER_CHAT),
        )
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        asyncio.run(bot.on_operator_callback(update, context))
        update.callback_query.answer.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
        db = factory()
        try:
            assert (
                db.scalar(select(func.count()).select_from(ApplicationOperatorDecisionRecord)) == 0
            )
        finally:
            db.close()

    def test_card_edit_failure_does_not_undo_the_decision(self, factory, wired):
        update = _update(
            build_callback_data(OperatorAction.SKIP, wired["token"]),
            chat_id=int(OPERATOR_CHAT),
            user_id=int(OPERATOR_CHAT),
        )
        update.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("boom"))
        asyncio.run(bot.on_operator_callback(update, MagicMock()))
        db = factory()
        try:
            assert (
                db.scalar(select(func.count()).select_from(ApplicationOperatorDecisionRecord)) == 1
            )
        finally:
            db.close()


# --- CLI ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command

    path = tmp_path / "cli.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(cfg, "head")
    return path


class TestCli:
    def _run(self, argv, **kwargs):
        from scripts.stage16_telegram_operator import run

        return run(argv, **kwargs)

    def _seed(self, path):
        engine = create_engine(f"sqlite:///{path.as_posix()}", poolclass=NullPool)
        enable_sqlite_foreign_keys(engine)
        session = sessionmaker(engine)()
        try:
            return build_target(session).application_id
        finally:
            session.close()
            engine.dispose()

    def test_preview_is_a_read_only_dry_run(self, migrated_db, capsys):
        app_id = self._seed(migrated_db)
        before = migrated_db.read_bytes()
        code = self._run(
            ["--database", str(migrated_db), "preview", str(app_id)], settings=operator_settings()
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "DRY RUN" in out and "No employer-facing action" in out
        assert "operator chat configured (***42)" in out
        assert OPERATOR_CHAT not in out and "TEST-TOKEN" not in out
        assert "[✅ APPROVE]" in out
        assert migrated_db.read_bytes() == before

    def test_send_without_live_sends_nothing(self, migrated_db, capsys):
        app_id = self._seed(migrated_db)
        transport = FakeTransport()
        code = self._run(
            ["--database", str(migrated_db), "send", str(app_id)],
            settings=operator_settings(),
            transport=transport,
        )
        assert code == 0 and transport.sent == []
        assert "DB WILL CHANGE: no" in capsys.readouterr().out

    def test_send_live_sends_one_card(self, migrated_db, capsys):
        app_id = self._seed(migrated_db)
        transport = FakeTransport()
        argv = ["--database", str(migrated_db), "send", str(app_id), "--live"]
        assert self._run(argv, settings=operator_settings(), transport=transport) == 0
        assert self._run(argv, settings=operator_settings(), transport=transport) == 1
        assert len(transport.sent) == 1
        assert "RESULT: ALREADY_SENT" in capsys.readouterr().out

    def test_config_prints_only_configured_or_missing(self, migrated_db, capsys):
        code = self._run(["--database", str(migrated_db), "config"], settings=operator_settings())
        out = capsys.readouterr().out
        assert code == 0
        assert "telegram_bot_token: configured" in out
        assert "telegram_operator_user_id: missing" in out
        assert OPERATOR_CHAT not in out and "TEST-TOKEN" not in out

    def test_preview_prints_emoji_on_a_cp1252_console(self, migrated_db, tmp_path):
        import os
        import subprocess
        import sys

        app_id = self._seed(migrated_db)
        env = {
            **os.environ,
            "PYTHONIOENCODING": "cp1252",
            "PYTHONPATH": str(PROJECT_ROOT),
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "stage16_telegram_operator.py"),
                "--database",
                str(migrated_db),
                "preview",
                str(app_id),
            ],
            cwd=tmp_path,  # no .env here
            env=env,
            capture_output=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        out = completed.stdout.decode("utf-8")
        assert "💼 Acme Vacuum GmbH" in out and "operator chat missing" in out

    def test_canonical_database_is_refused(self, capsys):
        canonical = PROJECT_ROOT / "stage14b_canonical_v12.db"
        code = self._run(["--database", str(canonical), "config"], settings=operator_settings())
        assert code == 2
        assert "REFUSED" in capsys.readouterr().err

    def test_override_dry_run_writes_nothing(self, migrated_db, capsys):
        app_id = self._seed(migrated_db)
        before = migrated_db.read_bytes()
        code = self._run(
            ["--database", str(migrated_db), "override", str(app_id), "SKIP", "--reason", "x"],
            settings=operator_settings(),
        )
        assert code == 1  # no final decision to override yet
        assert "NO_FINAL_DECISION" in capsys.readouterr().out
        assert migrated_db.read_bytes() == before


# --- migration ---------------------------------------------------------------------


def test_migration_round_trip_on_sqlite(tmp_path):
    from alembic.config import Config

    from alembic import command

    path = tmp_path / "migrate.db"
    url = f"sqlite:///{path.as_posix()}"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url, poolclass=NullPool)
    tables = {
        "telegram_operator_cards",
        "application_operator_decisions",
        "telegram_lifecycle_notifications",
    }
    try:
        assert tables <= set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            triggers = set(
                conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                ).scalars()
            )
        assert {
            "trg_application_operator_decisions_no_update",
            "trg_application_operator_decisions_no_delete",
        } <= triggers
        command.downgrade(cfg, "c8e4a2f6d1b9")
        assert not tables & set(inspect(engine).get_table_names())
        assert {"applications", "application_events"} <= set(inspect(engine).get_table_names())
        command.upgrade(cfg, "head")
        assert tables <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_stage15_migrations_are_not_rewritten():
    versions = PROJECT_ROOT / "alembic" / "versions"
    stage16 = (versions / "e3a9c5d7f2b4_add_telegram_operator_control_tables.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | Sequence[str] | None = "c8e4a2f6d1b9"' in stage16
    for name in (
        "b5d2e8f4a1c7_add_application_lifecycle_tables.py",
        "c8e4a2f6d1b9_application_events_append_only_triggers.py",
    ):
        source = (versions / name).read_text(encoding="utf-8")
        assert "telegram_operator" not in source and "operator_decisions" not in source


# --- no employer outbound (call graph) ------------------------------------------------

STAGE16_MODULES = (
    "app.services.telegram_operator_control",
    "app.services.telegram_operator_notifications",
    "app.services.telegram_operator_render",
    "app.services.telegram_operator_transport",
    "app.db.telegram_operator_repository",
    "app.models.telegram_operator",
)
EMPLOYER_OUTBOUND_MODULES = frozenset(
    {
        "smtplib",
        "app.providers.email.smtp",
        "app.providers.email.outbound_base",
        "app.providers.email.imap_draft",
        "app.providers.email.draft_base",
        "app.services.gmail_application_draft",
        "app.services.response_draft_send",
        "app.services.response_draft",
        "app.services.follow_up_send",
        "app.services.follow_up",
        "app.services.automation",
        "app.services.telegram_bewerbung",
        "app.services.telegram_bewerbung_approval",
    }
)


def _module_file(name: str) -> Path | None:
    base = PROJECT_ROOT / Path(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _closure(starts) -> set[str]:
    """Every module transitively imported (including function-level
    imports) from `starts`, following the project's own packages."""
    seen: set[str] = set()
    stack = list(starts)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_file(name)
        if path is None:
            continue
        for imported in _imports(path):
            if imported.split(".")[0] in {"app", "scripts", "smtplib"}:
                stack.append(imported)
    return seen


def test_stage16_services_cannot_reach_any_employer_outbound_module():
    reached = _closure(STAGE16_MODULES)
    assert not reached & EMPLOYER_OUTBOUND_MODULES
    # Not even read-only mailbox access: Stage 16 services never open IMAP.
    assert "app.providers.email.imap" not in reached


def test_stage16_cli_cannot_reach_any_employer_outbound_module():
    reached = _closure(["scripts.stage16_telegram_operator"])
    assert not reached & EMPLOYER_OUTBOUND_MODULES


def test_the_only_network_call_is_the_telegram_transport():
    """httpx (the only HTTP client) is reached solely via app.services.telegram,
    whose sender targets api.telegram.org."""
    reached = _closure(STAGE16_MODULES)
    for name in reached:
        path = _module_file(name)
        if path is None or name == "app.services.telegram":
            continue
        assert "httpx" not in _imports(path), name
    source = (PROJECT_ROOT / "app/services/telegram.py").read_text(encoding="utf-8")
    assert 'f"https://api.telegram.org/bot{bot_token}/sendMessage"' in source


def test_operator_callback_handler_calls_no_employer_code():
    tree = ast.parse((PROJECT_ROOT / "app/services/telegram_bot.py").read_text(encoding="utf-8"))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_operator_callback"
    )
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert not called & {
        "handle_create",
        "handle_apply",
        "handle_decision",
        "send_response_draft",
        "send_follow_up",
        "send_telegram_message",
    }
    assert "handle_callback" in called
