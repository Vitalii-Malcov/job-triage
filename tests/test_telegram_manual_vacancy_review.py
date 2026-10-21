"""`/review <job_id>`: the operator opens ONE existing vacancy as a normal
Stage 9A review card although the automatic recommendation was not APPLY.

Invariants: the matcher verdict (`JobRecord.score` / `.recommendation`) is
never mutated; the only network call is the card to the configured operator
chat; no application, Bewerbung preparation, draft or Gmail row is created;
the existing review state machine decides idempotency (no duplicate card,
DELIVERY_UNCERTAIN never retried, operator SKIPPED never reopened)."""

import json
import smtplib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.services.telegram_bot as bot
import app.services.telegram_vacancy_feed as feed
from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    ApplicationRecord,
    BewerbungDraftRecord,
    GmailApplicationDraftRecord,
    JobRecord,
    TelegramBewerbungPreparationRecord,
    TelegramVacancyReviewRecord,
)
from app.db.telegram_vacancy_review_repository import (
    MANUAL_HOLD_PREFIX,
    MAX_DELIVERY_ATTEMPTS,
    claim_for_sending,
    dequeue,
    ensure_review,
    get_review_for_job,
    mark_sent,
    mark_uncertain,
    reconcile_stale_sending,
    record_decision,
)
from app.services.telegram import TelegramSendOutcome, TelegramSendResult
from app.services.telegram_bewerbung import ELIGIBLE_REVIEW_STATES
from app.services.telegram_vacancy_feed import (
    MANUAL_REVIEW_MARKER,
    build_callback_data,
    build_card_keyboard,
)

OPERATOR_CHAT_ID = 12345
STRANGER_ID = 666
JOB_ID = 376


class FakeSender:
    def __init__(self, outcome=TelegramSendOutcome.SENT):
        self.outcome = outcome
        self.calls: list[dict] = []

    async def __call__(self, bot_token, chat_id, text, *, reply_markup=None, timeout_seconds=5.0):
        self.calls.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        message_id = 4242 if self.outcome is TelegramSendOutcome.SENT else None
        return TelegramSendResult(self.outcome, message_id=message_id)


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'manual_review.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def settings():
    return Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_chat_id=str(OPERATOR_CHAT_ID),
        min_job_score_to_notify=80,
        outbound_sending_enabled=False,
        telegram_gmail_draft_enabled=False,
    )


@pytest.fixture()
def sender(monkeypatch, session_factory, settings):
    fake = FakeSender()
    monkeypatch.setattr(bot, "SessionLocal", session_factory)
    monkeypatch.setattr(bot, "get_settings", lambda: settings)
    monkeypatch.setattr(feed, "send_telegram_message", fake)
    monkeypatch.setattr(
        feed, "get_candidate_skills_for_scoring", lambda db: frozenset({"python", "sql"})
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError("SMTP must never be used by /review")

    monkeypatch.setattr(smtplib, "SMTP", _forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _forbidden)
    return fake


def _insert_job(session_factory, *, job_id=JOB_ID, score=71, recommendation="MAYBE", status="NEW"):
    db = session_factory()
    try:
        db.add(
            JobRecord(
                id=job_id,
                fingerprint=f"{job_id:064d}",
                source="bundesagentur",
                title="Data & Analytics Engineer:in",
                company="Deutsche Bahn AG",
                location="Frankfurt am Main",
                url=f"https://example.com/jobs/{job_id}",
                description="Python, SQL und Datenpipelines.",
                skills_json="[]",
                data_confidence=0.9,
                skill_source="description_extracted",
                must_have_skills_json=json.dumps(["Python", "SQL"]),
                nice_to_have_skills_json="[]",
                posting_type="ARBEIT",
                score=score,
                recommendation=recommendation,
                status=status,
            )
        )
        db.commit()
        ensure_review(db, job_id, eligible=False)
    finally:
        db.close()


def _make_update(chat_id=OPERATOR_CHAT_ID, user_id=None, chat_type="private"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_user.id = chat_id if user_id is None else user_id
    update.message.reply_text = AsyncMock()
    return update


def _context(*args: str):
    context = MagicMock()
    context.args = list(args)
    return context


async def _review(*args, **update_kwargs):
    update = _make_update(**update_kwargs)
    await bot.cmd_review(update, _context(*args))
    return update


def _reply(update) -> str:
    update.message.reply_text.assert_awaited_once()
    return update.message.reply_text.await_args.args[0]


def _job(session_factory, job_id=JOB_ID) -> JobRecord:
    db = session_factory()
    try:
        return db.get(JobRecord, job_id)
    finally:
        db.close()


def _review_row(session_factory, job_id=JOB_ID) -> TelegramVacancyReviewRecord:
    db = session_factory()
    try:
        return get_review_for_job(db, job_id)
    finally:
        db.close()


def _count(session_factory, model) -> int:
    db = session_factory()
    try:
        return db.scalar(select(func.count()).select_from(model))
    finally:
        db.close()


def _set_review_state(session_factory, state: str, *, job_id=JOB_ID) -> None:
    db = session_factory()
    try:
        review = get_review_for_job(db, job_id)
        if state in ("TELEGRAM_SENT", "DELIVERY_UNCERTAIN", "SAVED", "SKIPPED"):
            review.state = "QUEUED_FOR_REVIEW"
            db.commit()
            claim = claim_for_sending(db, review)
            if state == "DELIVERY_UNCERTAIN":
                mark_uncertain(db, review, claim_token=claim, last_error="uncertain")
            else:
                mark_sent(db, review, claim_token=claim, message_id=1)
                if state in ("SAVED", "SKIPPED"):
                    assert record_decision(db, review, state)
        else:
            review.state = state
            db.commit()
    finally:
        db.close()


class TestJob376Acceptance:
    @pytest.mark.asyncio
    async def test_maybe_discovered_sends_one_normal_card(self, session_factory, sender):
        _insert_job(session_factory)

        update = await _review(str(JOB_ID))

        assert len(sender.calls) == 1
        call = sender.calls[0]
        assert call["chat_id"] == str(OPERATOR_CHAT_ID)
        review = _review_row(session_factory)
        assert review.state == "TELEGRAM_SENT"
        assert review.telegram_message_id == 4242
        assert review.state in ELIGIBLE_REVIEW_STATES
        assert call["reply_markup"] == build_card_keyboard(review.callback_token)
        assert "Data & Analytics Engineer:in" in call["text"]
        assert "Deutsche Bahn AG" in call["text"]
        assert "71/100" in call["text"]
        assert "MAYBE" in call["text"]
        assert MANUAL_REVIEW_MARKER in call["text"]
        assert MANUAL_REVIEW_MARKER in _reply(update)

    @pytest.mark.asyncio
    async def test_score_and_recommendation_and_status_unchanged(self, session_factory, sender):
        _insert_job(session_factory)

        await _review(str(JOB_ID))

        job = _job(session_factory)
        assert (job.score, job.recommendation, job.status) == (71, "MAYBE", "NEW")

    @pytest.mark.asyncio
    async def test_normal_buttons_present(self, session_factory, sender):
        _insert_job(session_factory)

        await _review(str(JOB_ID))

        buttons = [b for row in sender.calls[0]["reply_markup"]["inline_keyboard"] for b in row]
        assert [b["text"] for b in buttons] == [
            "✅ Bewerbung erstellen",
            "📄 Details",
            "⭐ Speichern",
            "❌ Überspringen",
        ]

    @pytest.mark.asyncio
    async def test_command_creates_no_application_bewerbung_or_gmail_rows(
        self, session_factory, sender
    ):
        _insert_job(session_factory)

        await _review(str(JOB_ID))

        assert _count(session_factory, ApplicationRecord) == 0
        assert _count(session_factory, TelegramBewerbungPreparationRecord) == 0
        assert _count(session_factory, BewerbungDraftRecord) == 0
        assert _count(session_factory, GmailApplicationDraftRecord) == 0
        assert _count(session_factory, TelegramVacancyReviewRecord) == 1
        # Exactly one outbound message, to the configured operator chat only.
        assert [c["chat_id"] for c in sender.calls] == [str(OPERATOR_CHAT_ID)]

    @pytest.mark.asyncio
    async def test_bewerbung_button_then_reaches_stage9b_flow(
        self, session_factory, sender, settings, monkeypatch
    ):
        _insert_job(session_factory)
        await _review(str(JOB_ID))
        review = _review_row(session_factory)

        draft_settings = settings.model_copy(update={"telegram_bewerbung_draft_enabled": True})
        monkeypatch.setattr(bot, "get_settings", lambda: draft_settings)
        handle_apply = AsyncMock(return_value=MagicMock(notice=None))
        monkeypatch.setattr(bot, "handle_apply", handle_apply)
        press = _make_update()
        press.callback_query.data = build_callback_data(feed.ACTION_APPLY, review.callback_token)
        press.callback_query.answer = AsyncMock()

        await bot.on_vacancy_callback(press, _context())

        handle_apply.assert_awaited_once()
        assert handle_apply.await_args.args[2] == review.id

    @pytest.mark.asyncio
    async def test_review_row_created_when_missing(self, session_factory, sender):
        _insert_job(session_factory)
        db = session_factory()
        try:
            db.delete(get_review_for_job(db, JOB_ID))
            db.commit()
        finally:
            db.close()

        await _review(str(JOB_ID))

        assert _review_row(session_factory).state == "TELEGRAM_SENT"
        assert len(sender.calls) == 1


class TestAuthorization:
    @pytest.mark.asyncio
    async def test_foreign_chat_gets_silence(self, session_factory, sender):
        _insert_job(session_factory)

        update = await _review(str(JOB_ID), chat_id=STRANGER_ID)

        update.message.reply_text.assert_not_awaited()
        assert sender.calls == []
        assert _review_row(session_factory).state == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_other_user_in_operator_chat_rejected(self, session_factory, sender):
        _insert_job(session_factory)

        update = await _review(str(JOB_ID), user_id=STRANGER_ID, chat_type="group")

        assert "nicht erlaubt" in _reply(update)
        assert sender.calls == []
        assert _review_row(session_factory).state == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_configured_operator_user_mismatch_rejected(
        self, session_factory, sender, settings, monkeypatch
    ):
        _insert_job(session_factory)
        scoped = settings.model_copy(update={"telegram_operator_user_id": "777"})
        monkeypatch.setattr(bot, "get_settings", lambda: scoped)

        update = await _review(str(JOB_ID))

        assert "nicht erlaubt" in _reply(update)
        assert sender.calls == []


class TestSyntax:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args",
        [(), ("abc",), ("-1",), ("0",), ("1.5",), ("376", "extra"), ("+376",), ("٣٧٦",)],
    )
    async def test_usage_error(self, session_factory, sender, args):
        _insert_job(session_factory)

        update = await _review(*args)

        assert _reply(update).startswith("Usage: /review <job_id>")
        assert sender.calls == []
        assert _review_row(session_factory).state == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_nonexistent_job(self, session_factory, sender):
        update = await _review("999999")

        assert "Job #999999 nicht gefunden" in _reply(update)
        assert sender.calls == []
        assert _count(session_factory, TelegramVacancyReviewRecord) == 0


class TestEligibility:
    @pytest.mark.asyncio
    async def test_machine_skip_refused(self, session_factory, sender):
        _insert_job(session_factory, recommendation="SKIP", score=20)

        update = await _review(str(JOB_ID))

        assert "Automatische Empfehlung ist SKIP" in _reply(update)
        assert sender.calls == []
        assert _review_row(session_factory).state == "DISCOVERED"
        job = _job(session_factory)
        assert (job.score, job.recommendation) == (20, "SKIP")

    @pytest.mark.asyncio
    async def test_apply_job_can_be_shown(self, session_factory, sender):
        _insert_job(session_factory, recommendation="APPLY", score=90)

        await _review(str(JOB_ID))

        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "TELEGRAM_SENT"

    @pytest.mark.asyncio
    async def test_job_status_not_new_refused(self, session_factory, sender):
        _insert_job(session_factory, status="APPLIED")

        update = await _review(str(JOB_ID))

        assert "Status APPLIED" in _reply(update)
        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_existing_application_refused(self, session_factory, sender):
        _insert_job(session_factory)
        db = session_factory()
        try:
            now = datetime.now(UTC)
            db.add(
                ApplicationRecord(
                    job_id=JOB_ID,
                    company_name="Deutsche Bahn AG",
                    role_title="Data & Analytics Engineer:in",
                    source="bundesagentur",
                    application_channel="EMAIL",
                    current_status="SUBMITTED",
                    last_status_changed_at=now,
                    submitted_at=now,
                )
            )
            db.commit()
        finally:
            db.close()

        update = await _review(str(JOB_ID))

        assert "Bewerbung" in _reply(update) and "existiert bereits" in _reply(update)
        assert sender.calls == []
        assert _review_row(session_factory).state == "DISCOVERED"


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_repeat_does_not_duplicate_card(self, session_factory, sender):
        _insert_job(session_factory)

        await _review(str(JOB_ID))
        update = await _review(str(JOB_ID))

        assert len(sender.calls) == 1
        assert "bereits gesendet" in _reply(update)
        assert _count(session_factory, TelegramVacancyReviewRecord) == 1

    @pytest.mark.asyncio
    async def test_already_sent_not_resent(self, session_factory, sender):
        _insert_job(session_factory)
        _set_review_state(session_factory, "TELEGRAM_SENT")

        update = await _review(str(JOB_ID))

        assert "bereits gesendet" in _reply(update)
        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_delivery_uncertain_not_retried(self, session_factory, sender):
        _insert_job(session_factory)
        _set_review_state(session_factory, "DELIVERY_UNCERTAIN")

        update = await _review(str(JOB_ID))

        assert "unsicher" in _reply(update)
        assert sender.calls == []
        assert _review_row(session_factory).state == "DELIVERY_UNCERTAIN"

    @pytest.mark.asyncio
    async def test_operator_skipped_not_reopened(self, session_factory, sender):
        _insert_job(session_factory)
        _set_review_state(session_factory, "SKIPPED")

        update = await _review(str(JOB_ID))

        assert "übersprungen" in _reply(update)
        assert sender.calls == []
        assert _review_row(session_factory).state == "SKIPPED"

    @pytest.mark.asyncio
    async def test_saved_not_resent(self, session_factory, sender):
        _insert_job(session_factory)
        _set_review_state(session_factory, "SAVED")

        update = await _review(str(JOB_ID))

        assert "bereits gesendet" in _reply(update)
        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_sending_in_flight_not_touched(self, session_factory, sender):
        _insert_job(session_factory)
        _set_review_state(session_factory, "SENDING")

        update = await _review(str(JOB_ID))

        assert "wird gerade gesendet" in _reply(update)
        assert sender.calls == []
        assert _review_row(session_factory).state == "SENDING"

    @pytest.mark.asyncio
    async def test_delivery_failed_not_retried(self, session_factory, sender):
        _insert_job(session_factory)
        _set_review_state(session_factory, "DELIVERY_FAILED")

        update = await _review(str(JOB_ID))

        assert "fehlgeschlagen" in _reply(update)
        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_already_queued_is_delivered_once(self, session_factory, sender):
        _insert_job(session_factory)
        _set_review_state(session_factory, "QUEUED_FOR_REVIEW")

        await _review(str(JOB_ID))
        await _review(str(JOB_ID))

        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "TELEGRAM_SENT"


class TestDeliveryOutcomes:
    @pytest.mark.asyncio
    async def test_uncertain_send_marked_and_not_retried(self, session_factory, sender):
        _insert_job(session_factory)
        sender.outcome = TelegramSendOutcome.UNCERTAIN

        first = await _review(str(JOB_ID))
        second = await _review(str(JOB_ID))

        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "DELIVERY_UNCERTAIN"
        assert "unsicher" in _reply(first)
        assert "unsicher" in _reply(second)

    @pytest.mark.asyncio
    async def test_failed_send_reported(self, session_factory, sender):
        _insert_job(session_factory)
        sender.outcome = TelegramSendOutcome.FAILED

        update = await _review(str(JOB_ID))

        assert "nicht gesendet" in _reply(update)
        assert _review_row(session_factory).state == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_failed_maybe_card_is_not_sent_by_regular_sweep(
        self, session_factory, sender, settings
    ):
        """A provably failed manual send is held out of the queue; the
        regular sweep never sees it (no automatic resend of a manual card)
        -- the operator can simply /review again."""
        _insert_job(session_factory)
        sender.outcome = TelegramSendOutcome.FAILED
        await _review(str(JOB_ID))

        stats = await _sweep(session_factory, settings)

        assert stats.sent == 0 and stats.failed == 0
        assert len(sender.calls) == 1
        sender.outcome = TelegramSendOutcome.SENT
        await _review(str(JOB_ID))
        assert _review_row(session_factory).state == "TELEGRAM_SENT"
        assert len(sender.calls) == 2


async def _sweep(session_factory, settings):
    db = session_factory()
    try:
        return await feed.deliver_queued_vacancy_cards(
            db, settings, sleep=AsyncMock(return_value=None)
        )
    finally:
        db.close()


def _recollect(session_factory, settings, job_id=JOB_ID) -> None:
    db = session_factory()
    try:
        feed.record_collected_job(db, db.get(JobRecord, job_id), settings)
    finally:
        db.close()


def _assert_verdict_unchanged(session_factory, score, recommendation) -> None:
    job = _job(session_factory)
    assert (job.score, job.recommendation, job.status) == (score, recommendation, "NEW")


class TestManualFailureHasNoAutomaticRetry:
    """Codex 1671ebc P1 #1 / P1 #2: a known-unsent MANUAL card may only be
    retried by another explicit `/review`, never by the automatic sweep or
    a re-collection; a pre-send failure never strands the claim."""

    @pytest.mark.asyncio
    async def test_maybe_manual_failed_held_for_explicit_retry(
        self, session_factory, sender, settings
    ):
        _insert_job(session_factory)
        sender.outcome = TelegramSendOutcome.FAILED

        await _review(str(JOB_ID))

        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "DISCOVERED"
        stats = await _sweep(session_factory, settings)
        _recollect(session_factory, settings)
        await _sweep(session_factory, settings)
        assert stats.sent == 0
        assert len(sender.calls) == 1
        _assert_verdict_unchanged(session_factory, 71, "MAYBE")

        sender.outcome = TelegramSendOutcome.SENT
        await _review(str(JOB_ID))
        assert len(sender.calls) == 2
        assert _review_row(session_factory).state == "TELEGRAM_SENT"
        _assert_verdict_unchanged(session_factory, 71, "MAYBE")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("initial_state", ["DISCOVERED", "QUEUED_FOR_REVIEW"])
    async def test_apply_manual_failed_not_resent_by_sweep(
        self, session_factory, sender, settings, initial_state
    ):
        _insert_job(session_factory, recommendation="APPLY", score=90)
        _set_review_state(session_factory, initial_state)
        sender.outcome = TelegramSendOutcome.FAILED

        await _review(str(JOB_ID))

        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "DISCOVERED"
        stats = await _sweep(session_factory, settings)
        assert stats.sent == 0 and stats.failed == 0
        # A re-collection of the still-eligible APPLY job must not re-queue it.
        _recollect(session_factory, settings)
        assert _review_row(session_factory).state == "DISCOVERED"
        stats = await _sweep(session_factory, settings)
        assert stats.sent == 0
        assert len(sender.calls) == 1
        _assert_verdict_unchanged(session_factory, 90, "APPLY")

        sender.outcome = TelegramSendOutcome.SENT
        await _review(str(JOB_ID))
        assert len(sender.calls) == 2
        assert _review_row(session_factory).state == "TELEGRAM_SENT"
        _assert_verdict_unchanged(session_factory, 90, "APPLY")

    @pytest.mark.asyncio
    async def test_explicit_retries_bounded_by_attempt_cap(self, session_factory, sender):
        _insert_job(session_factory)
        sender.outcome = TelegramSendOutcome.FAILED

        for _ in range(MAX_DELIVERY_ATTEMPTS):
            await _review(str(JOB_ID))
        update = await _review(str(JOB_ID))

        assert len(sender.calls) == MAX_DELIVERY_ATTEMPTS
        assert _review_row(session_factory).state == "DELIVERY_FAILED"
        assert "fehlgeschlagen" in _reply(update)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("recommendation,score", [("MAYBE", 71), ("APPLY", 90)])
    async def test_pre_send_skill_read_failure_releases_claim(
        self, session_factory, sender, settings, monkeypatch, caplog, recommendation, score
    ):
        _insert_job(session_factory, recommendation=recommendation, score=score)

        def _boom(db):
            raise RuntimeError("secret-db-detail")

        monkeypatch.setattr(feed, "get_candidate_skills_for_scoring", _boom)

        update = await _review(str(JOB_ID))

        assert sender.calls == []
        review = _review_row(session_factory)
        assert review.state not in ("SENDING", "DELIVERY_UNCERTAIN")
        assert review.state == "DISCOVERED"
        assert review.claim_token is None
        assert review.attempt_count == 0
        assert "nicht gesendet" in _reply(update)
        assert "secret-db-detail" not in caplog.text
        assert "secret-db-detail" not in _reply(update)

        # Stale reconciliation can never turn it into DELIVERY_UNCERTAIN.
        db = session_factory()
        try:
            assert reconcile_stale_sending(db, ttl_seconds=0.0) == 0
        finally:
            db.close()
        monkeypatch.setattr(
            feed, "get_candidate_skills_for_scoring", lambda db: frozenset({"python"})
        )
        _recollect(session_factory, settings)
        stats = await _sweep(session_factory, settings)
        assert stats.sent == 0 and sender.calls == []

        await _review(str(JOB_ID))
        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "TELEGRAM_SENT"
        _assert_verdict_unchanged(session_factory, score, recommendation)

    @pytest.mark.asyncio
    async def test_telegram_sent_never_manually_resent(self, session_factory, sender, settings):
        _insert_job(session_factory, recommendation="APPLY", score=90)
        await _review(str(JOB_ID))

        await _review(str(JOB_ID))
        await _sweep(session_factory, settings)

        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "TELEGRAM_SENT"

    @pytest.mark.asyncio
    async def test_delivery_uncertain_never_manually_resent(
        self, session_factory, sender, settings
    ):
        _insert_job(session_factory, recommendation="APPLY", score=90)
        sender.outcome = TelegramSendOutcome.UNCERTAIN
        await _review(str(JOB_ID))

        sender.outcome = TelegramSendOutcome.SENT
        update = await _review(str(JOB_ID))
        await _sweep(session_factory, settings)

        assert len(sender.calls) == 1
        assert "unsicher" in _reply(update)
        assert _review_row(session_factory).state == "DELIVERY_UNCERTAIN"


def _raise_runtime(*args, **kwargs):
    raise RuntimeError("pre-send boom")


class TestManualPreSendFailureConsumesNoAttempt:
    """Codex aadca62 P1: any manual failure before `send_telegram_message`
    starts releases the claim to the manual hold without consuming a
    delivery attempt -- it can never exhaust the cap into DELIVERY_FAILED."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target", ["render_vacancy_card", "build_vacancy_card", "build_card_keyboard"]
    )
    async def test_pre_send_failure_releases_to_manual_hold(
        self, session_factory, sender, monkeypatch, target
    ):
        _insert_job(session_factory)
        original = getattr(feed, target)
        monkeypatch.setattr(feed, target, _raise_runtime)

        update = await _review(str(JOB_ID))

        assert sender.calls == []
        review = _review_row(session_factory)
        assert review.state == "DISCOVERED"
        assert review.last_error.startswith(MANUAL_HOLD_PREFIX)
        assert review.attempt_count == 0
        assert review.claim_token is None
        assert "nicht gesendet" in _reply(update)
        _assert_verdict_unchanged(session_factory, 71, "MAYBE")

        monkeypatch.setattr(feed, target, original)
        await _review(str(JOB_ID))
        assert len(sender.calls) == 1
        assert _review_row(session_factory).state == "TELEGRAM_SENT"
        _assert_verdict_unchanged(session_factory, 71, "MAYBE")

    @pytest.mark.asyncio
    async def test_pre_send_failure_keeps_previous_attempt_count(
        self, session_factory, sender, monkeypatch
    ):
        _insert_job(session_factory)
        sender.outcome = TelegramSendOutcome.FAILED
        await _review(str(JOB_ID))
        assert _review_row(session_factory).attempt_count == 1
        monkeypatch.setattr(feed, "build_card_keyboard", _raise_runtime)

        await _review(str(JOB_ID))

        review = _review_row(session_factory)
        assert len(sender.calls) == 1
        assert (review.state, review.attempt_count) == ("DISCOVERED", 1)
        assert review.claim_token is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["render_vacancy_card", "build_card_keyboard"])
    async def test_repeated_pre_send_failure_never_exhausts_cap(
        self, session_factory, sender, settings, monkeypatch, target
    ):
        _insert_job(session_factory, recommendation="APPLY", score=90)
        original = getattr(feed, target)
        monkeypatch.setattr(feed, target, _raise_runtime)

        for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
            await _review(str(JOB_ID))
            review = _review_row(session_factory)
            assert review.state == "DISCOVERED"
            assert review.attempt_count == 0
        stats = await _sweep(session_factory, settings)
        _recollect(session_factory, settings)
        assert _review_row(session_factory).state == "DISCOVERED"
        assert stats.sent == 0 and sender.calls == []

        monkeypatch.setattr(feed, target, original)
        await _review(str(JOB_ID))
        assert len(sender.calls) == 1
        review = _review_row(session_factory)
        assert (review.state, review.attempt_count) == ("TELEGRAM_SENT", 1)
        _assert_verdict_unchanged(session_factory, 90, "APPLY")

    @pytest.mark.asyncio
    async def test_real_telegram_failed_still_counts_an_attempt(self, session_factory, sender):
        _insert_job(session_factory)
        sender.outcome = TelegramSendOutcome.FAILED

        await _review(str(JOB_ID))
        await _review(str(JOB_ID))

        review = _review_row(session_factory)
        assert len(sender.calls) == 2
        assert (review.state, review.attempt_count) == ("DISCOVERED", 2)
        assert review.last_error == f"{MANUAL_HOLD_PREFIX}{TelegramSendOutcome.FAILED.value}"


class TestAutomaticFeedUnchanged:
    @pytest.mark.asyncio
    async def test_automatic_render_failure_consumes_attempt_and_requeues(
        self, session_factory, sender, settings, monkeypatch
    ):
        _insert_job(session_factory, recommendation="APPLY", score=90)
        _recollect(session_factory, settings)
        monkeypatch.setattr(feed, "render_vacancy_card", _raise_runtime)

        stats = await _sweep(session_factory, settings)

        review = _review_row(session_factory)
        assert stats.failed == 1 and sender.calls == []
        assert (review.state, review.attempt_count) == ("QUEUED_FOR_REVIEW", 1)
        assert review.last_error == "render:RuntimeError"
        assert review.claim_token is None

    @pytest.mark.asyncio
    async def test_automatic_apply_failure_still_retried_by_next_sweep(
        self, session_factory, sender, settings
    ):
        _insert_job(session_factory, recommendation="APPLY", score=90)
        _recollect(session_factory, settings)
        assert _review_row(session_factory).state == "QUEUED_FOR_REVIEW"
        sender.outcome = TelegramSendOutcome.FAILED

        first = await _sweep(session_factory, settings)

        assert first.failed == 1
        assert _review_row(session_factory).state == "QUEUED_FOR_REVIEW"
        sender.outcome = TelegramSendOutcome.SENT
        second = await _sweep(session_factory, settings)
        assert second.sent == 1
        assert len(sender.calls) == 2
        assert _review_row(session_factory).state == "TELEGRAM_SENT"
        _assert_verdict_unchanged(session_factory, 90, "APPLY")

    @pytest.mark.asyncio
    async def test_automatic_dequeued_row_requeued_on_recollection(
        self, session_factory, sender, settings
    ):
        """A row the sweep dequeued (not a manual failure) is still promoted
        back to the queue when a re-collection finds it eligible."""
        _insert_job(session_factory, recommendation="APPLY", score=90)
        _set_review_state(session_factory, "QUEUED_FOR_REVIEW")
        db = session_factory()
        try:
            assert dequeue(db, get_review_for_job(db, JOB_ID))
        finally:
            db.close()

        _recollect(session_factory, settings)

        assert _review_row(session_factory).state == "QUEUED_FOR_REVIEW"


class TestOversizedJobId:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["9" * 40, str(2**63), "1" + "0" * 19])
    async def test_oversized_id_safe_usage_reply(self, session_factory, sender, raw):
        _insert_job(session_factory)

        update = await _review(raw)

        assert _reply(update).startswith("Usage: /review <job_id>")
        assert sender.calls == []
        assert _review_row(session_factory).state == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_max_int64_id_is_plain_not_found(self, session_factory, sender):
        update = await _review(str(2**63 - 1))

        assert "nicht gefunden" in _reply(update)
        assert sender.calls == []


def test_help_lists_review_command():
    assert "/review <job_id>" in bot.HELP_TEXT


def test_review_command_registered():
    application = bot.build_application(Settings(_env_file=None, telegram_bot_token="123:abc"))
    commands = {
        command
        for handlers in application.handlers.values()
        for handler in handlers
        for command in getattr(handler, "commands", ())
    }
    assert "review" in commands
