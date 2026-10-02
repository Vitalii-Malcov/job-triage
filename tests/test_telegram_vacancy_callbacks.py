"""Stage 9A: Telegram vacancy-card button callbacks
(`app.services.telegram_bot.on_vacancy_callback`) -- authorization,
callback-token validation, Details/Save/Skip behavior, and the
"Bewerbung erstellen" placeholder doing nothing."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.telegram_bot as bot
from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord, TelegramVacancyReviewRecord
from app.db.repositories import upsert_job
from app.db.telegram_vacancy_review_repository import (
    claim_for_sending,
    ensure_review,
    mark_sent,
)
from app.models.job import Job, JobScore
from app.services.telegram_vacancy_feed import build_callback_data

AUTHORIZED_CHAT_ID = 12345
STRANGER_CHAT_ID = 666


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vacancy_callbacks.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def patch_bot_dependencies(monkeypatch, session_factory):
    settings = Settings(telegram_bot_token="test-token", telegram_chat_id=str(AUTHORIZED_CHAT_ID))
    monkeypatch.setattr(bot, "SessionLocal", session_factory)
    monkeypatch.setattr(bot, "get_settings", lambda: settings)


@pytest.fixture()
def sent_review(session_factory):
    db = session_factory()
    try:
        job = Job(
            source="test",
            title="Python Developer",
            company="Acme GmbH",
            url="https://example.com/jobs/1",
            description="Python und SQL.",
        )
        record, _ = upsert_job(db, job, JobScore(score=90, recommendation="APPLY"))
        record.must_have_skills_json = json.dumps(["Python", "SQL"])
        db.commit()
        review = ensure_review(db, record.id, eligible=True)
        claim_for_sending(db, review)
        mark_sent(db, review, message_id=1)
        return {"id": review.id, "job_id": record.id, "token": review.callback_token}
    finally:
        db.close()


def _state(session_factory, review_id: int) -> str:
    db = session_factory()
    try:
        return db.get(TelegramVacancyReviewRecord, review_id).state
    finally:
        db.close()


def _make_update(data: str, chat_id: int = AUTHORIZED_CHAT_ID) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


class TestAuthorization:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["s", "x", "d", "a"])
    async def test_unauthorized_chat_gets_silence_and_changes_nothing(
        self, session_factory, sent_review, action
    ):
        update = _make_update(build_callback_data(action, sent_review["token"]), STRANGER_CHAT_ID)
        context = _make_context()

        await bot.on_vacancy_callback(update, context)

        update.callback_query.answer.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
        assert _state(session_factory, sent_review["id"]) == "TELEGRAM_SENT"

    def test_handler_registered_for_vacancy_callbacks(self):
        application = bot.build_application(
            Settings(telegram_bot_token="123:abc", telegram_chat_id=str(AUTHORIZED_CHAT_ID))
        )
        callbacks = [
            h.callback for group in application.handlers.values() for h in group if h is not None
        ]
        assert bot.on_vacancy_callback in callbacks


class TestCallbackValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data",
        [
            "garbage",
            "vf:s",
            "vf:z:AAAAAAAAAAAAAAAA",
            "vf:s:1",
            "vf:s:../../etc",
            "vf:s:" + "A" * 64,
        ],
    )
    async def test_malformed_or_id_like_callback_rejected(self, session_factory, sent_review, data):
        update = _make_update(data)

        await bot.on_vacancy_callback(update, _make_context())

        update.callback_query.answer.assert_awaited_once()
        assert _state(session_factory, sent_review["id"]) == "TELEGRAM_SENT"

    @pytest.mark.asyncio
    async def test_job_id_in_place_of_token_is_rejected(self, session_factory, sent_review):
        update = _make_update(build_callback_data("s", str(sent_review["job_id"])))

        await bot.on_vacancy_callback(update, _make_context())

        assert _state(session_factory, sent_review["id"]) == "TELEGRAM_SENT"

    @pytest.mark.asyncio
    async def test_unknown_well_formed_token_rejected(self, session_factory, sent_review):
        update = _make_update(build_callback_data("s", "Z" * 16))

        await bot.on_vacancy_callback(update, _make_context())

        update.callback_query.answer.assert_awaited_once_with("Unbekannte oder abgelaufene Stelle.")
        assert _state(session_factory, sent_review["id"]) == "TELEGRAM_SENT"


class TestActions:
    @pytest.mark.asyncio
    async def test_save_persists(self, session_factory, sent_review):
        update = _make_update(build_callback_data("s", sent_review["token"]))

        await bot.on_vacancy_callback(update, _make_context())

        update.callback_query.answer.assert_awaited_once_with("⭐ Gespeichert.")
        assert _state(session_factory, sent_review["id"]) == "SAVED"

    @pytest.mark.asyncio
    async def test_skip_persists(self, session_factory, sent_review):
        update = _make_update(build_callback_data("x", sent_review["token"]))

        await bot.on_vacancy_callback(update, _make_context())

        update.callback_query.answer.assert_awaited_once_with("❌ Übersprungen.")
        assert _state(session_factory, sent_review["id"]) == "SKIPPED"

    @pytest.mark.asyncio
    async def test_repeated_save_is_idempotent(self, session_factory, sent_review):
        data = build_callback_data("s", sent_review["token"])
        await bot.on_vacancy_callback(_make_update(data), _make_context())

        update = _make_update(data)
        await bot.on_vacancy_callback(update, _make_context())

        update.callback_query.answer.assert_awaited_once_with("Bereits gespeichert.")
        assert _state(session_factory, sent_review["id"]) == "SAVED"

    @pytest.mark.asyncio
    async def test_save_does_not_touch_application_status(self, session_factory, sent_review):
        await bot.on_vacancy_callback(
            _make_update(build_callback_data("s", sent_review["token"])), _make_context()
        )

        db = session_factory()
        try:
            assert db.get(JobRecord, sent_review["job_id"]).status == "NEW"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_details_sends_details_to_the_same_chat(self, session_factory, sent_review):
        update = _make_update(build_callback_data("d", sent_review["token"]))
        context = _make_context()

        await bot.on_vacancy_callback(update, context)

        context.bot.send_message.assert_awaited_once()
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == AUTHORIZED_CHAT_ID
        assert "Python Developer" in kwargs["text"]
        assert "Required: Python, SQL" in kwargs["text"]
        assert _state(session_factory, sent_review["id"]) == "TELEGRAM_SENT"

    @pytest.mark.asyncio
    async def test_apply_button_is_a_no_op_placeholder(self, session_factory, sent_review):
        update = _make_update(build_callback_data("a", sent_review["token"]))
        context = _make_context()

        await bot.on_vacancy_callback(update, context)

        update.callback_query.answer.assert_awaited_once()
        assert update.callback_query.answer.await_args.kwargs == {"show_alert": True}
        context.bot.send_message.assert_not_awaited()
        assert _state(session_factory, sent_review["id"]) == "TELEGRAM_SENT"
        db = session_factory()
        try:
            assert db.get(JobRecord, sent_review["job_id"]).status == "NEW"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_undelivered_card_cannot_be_decided(self, session_factory):
        db = session_factory()
        try:
            record, _ = upsert_job(
                db,
                Job(source="t", title="Dev", company="X", url="https://example.com/2"),
                JobScore(score=90, recommendation="APPLY"),
            )
            review = ensure_review(db, record.id, eligible=True)
            token, review_id = review.callback_token, review.id
        finally:
            db.close()
        update = _make_update(build_callback_data("s", token))

        await bot.on_vacancy_callback(update, _make_context())

        update.callback_query.answer.assert_awaited_once_with(
            "Für diese Stelle gerade nicht möglich."
        )
        assert _state(session_factory, review_id) == "QUEUED_FOR_REVIEW"
