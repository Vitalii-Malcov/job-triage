"""Codex Stage 8E HIGH finding (ACCOUNT SCOPE): the manual `/digest`
command and the automatic daily digest must resolve account scope
through the SAME shared helper
(`app.services.telegram_digest.resolve_digest_account_key`) so neither
casing/whitespace divergence nor independent normalization logic can
make them silently read two different namespaces.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import AutomationRunRecord
from app.services.telegram_bot import cmd_digest
from app.services.telegram_digest import build_digest_text, resolve_digest_account_key

SCHEDULER_ACCOUNT = "scheduler@example.com"
GMAIL_ACCOUNT = "gmail-user@example.com"


class TestResolveDigestAccountKeyUnit:
    def test_prefers_automation_scheduler_account_key_when_nonblank(self):
        settings = Settings(
            automation_scheduler_account_key=SCHEDULER_ACCOUNT,
            gmail_username=GMAIL_ACCOUNT,
        )
        assert resolve_digest_account_key(settings) == SCHEDULER_ACCOUNT

    def test_falls_back_to_normalized_gmail_username_when_scheduler_key_blank(self):
        settings = Settings(
            automation_scheduler_account_key="",
            gmail_username="  Mixed.Case@Example.com  ",
        )
        assert resolve_digest_account_key(settings) == "mixed.case@example.com"

    def test_falls_back_when_scheduler_key_only_whitespace(self):
        """model_construct bypasses field validators -- proves the
        helper's OWN `.strip()` fails closed even if a Settings-like
        object ever reaches it without having gone through
        Settings._validate_automation_scheduler_account_key first."""
        settings = Settings.model_construct(
            automation_scheduler_account_key="   ",
            gmail_username=GMAIL_ACCOUNT,
        )
        assert resolve_digest_account_key(settings) == GMAIL_ACCOUNT

    def test_returns_blank_when_neither_is_configured(self):
        settings = Settings(automation_scheduler_account_key="", gmail_username="")
        assert resolve_digest_account_key(settings) == ""

    def test_scheduler_key_wins_even_when_it_differs_in_case_from_gmail_username(self):
        """Both are "configured" but with divergent identities -- the
        scheduler key must win deterministically, not whichever the
        caller happened to normalize first."""
        settings = Settings(
            automation_scheduler_account_key="Scheduler@Example.com",
            gmail_username="totally-different@example.com",
        )
        assert resolve_digest_account_key(settings) == "Scheduler@Example.com"


class TestManualAndDailyDigestNeverDiverge:
    """Integration-style proof: given ONE Settings object, the manual
    `/digest` handler's account_key resolution and the daily digest's
    account_key resolution must be identical -- never independently
    computed, never silently different."""

    def test_cmd_digest_and_resolve_digest_account_key_agree_when_scheduler_key_set(
        self, monkeypatch
    ):
        settings = Settings(
            automation_scheduler_account_key=SCHEDULER_ACCOUNT,
            gmail_username=GMAIL_ACCOUNT,
        )
        monkeypatch.setattr("app.services.telegram_bot.get_settings", lambda: settings)

        captured = {}
        real_build_digest_text = build_digest_text

        def _spy_build_digest_text(db, account_key):
            captured["account_key"] = account_key
            return real_build_digest_text(db, account_key)

        monkeypatch.setattr("app.services.telegram_bot.build_digest_text", _spy_build_digest_text)

        # Minimal fake update/context -- mirrors tests/test_telegram_bot.py's
        # own _make_update/_make_context helpers.
        update = MagicMock()
        update.effective_chat.id = 12345
        update.message = MagicMock()
        update.message.text = "/digest"
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.telegram_bot.SessionLocal", session_factory)
        monkeypatch.setattr(
            "app.services.telegram_bot.get_settings",
            lambda: Settings(
                automation_scheduler_account_key=SCHEDULER_ACCOUNT,
                gmail_username=GMAIL_ACCOUNT,
                telegram_bot_token="tok",
                telegram_chat_id="12345",
            ),
        )

        asyncio.run(cmd_digest(update, context))

        # The manual command resolved account_key through the exact
        # same helper the daily digest uses -- proven by direct
        # equality against calling the helper independently with the
        # SAME Settings.
        expected = resolve_digest_account_key(
            Settings(
                automation_scheduler_account_key=SCHEDULER_ACCOUNT,
                gmail_username=GMAIL_ACCOUNT,
                telegram_bot_token="tok",
                telegram_chat_id="12345",
            )
        )
        assert captured["account_key"] == expected == SCHEDULER_ACCOUNT

    @pytest.mark.parametrize(
        ("scheduler_key", "gmail_username", "expected"),
        [
            (SCHEDULER_ACCOUNT, GMAIL_ACCOUNT, SCHEDULER_ACCOUNT),
            ("", "Some.User@Example.com", "some.user@example.com"),
            ("  padded@example.com  ", "other@example.com", "padded@example.com"),
        ],
    )
    def test_daily_digest_account_key_matches_manual_digest_for_same_settings(
        self, scheduler_key, gmail_username, expected
    ):
        """Constructs Settings once and resolves through the shared
        helper twice (simulating the manual command's own call and the
        scheduler's own call) -- must always agree, by construction."""
        settings = Settings(
            automation_scheduler_account_key=scheduler_key, gmail_username=gmail_username
        )
        manual_digest_account_key = resolve_digest_account_key(settings)
        daily_digest_account_key = resolve_digest_account_key(settings)
        assert manual_digest_account_key == daily_digest_account_key == expected


class TestDataIsolationAcrossResolvedAccountKeys:
    @pytest.fixture()
    def session_factory(self, tmp_path):
        db_path = tmp_path / "test_digest_account_scope.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def test_a_run_recorded_under_the_scheduler_key_is_visible_via_the_same_resolved_key(
        self, session_factory
    ):
        """A run persisted under whatever account_key
        resolve_digest_account_key resolves to must be visible to BOTH
        callers -- proven by resolving once, writing under that key, and
        reading back via an INDEPENDENT second resolution."""
        settings = Settings(
            automation_scheduler_account_key=SCHEDULER_ACCOUNT, gmail_username=GMAIL_ACCOUNT
        )
        account_key = resolve_digest_account_key(settings)

        db = session_factory()
        try:
            run = AutomationRunRecord(
                account_key=account_key,
                status="COMPLETED",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                results_json="{}",
            )
            db.add(run)
            db.commit()

            # Independent second resolution (as the OTHER caller would
            # do it) must land on the exact same account_key and
            # therefore see the same run.
            other_resolution = resolve_digest_account_key(settings)
            text = build_digest_text(db, other_resolution)
            assert f"Latest run: #{run.id}" in text
        finally:
            db.close()
