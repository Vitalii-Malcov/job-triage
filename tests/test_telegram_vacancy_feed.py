"""Stage 9A: Telegram vacancy feed -- card rendering/length safety,
callback data, delivery sweep (dedup, retry, uncertain, lease) and the
collector integration behind `telegram_vacancy_feed_enabled`."""

import json

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.services.telegram_vacancy_feed as feed
from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord, TelegramVacancyReviewRecord
from app.db.repositories import upsert_job
from app.db.telegram_vacancy_review_repository import (
    MAX_DELIVERY_ATTEMPTS,
    claim_for_sending,
    ensure_review,
    get_review_for_job,
)
from app.models.job import Job, JobScore
from app.services.collector_runner import run_bundesagentur
from app.services.telegram import (
    TelegramSendOutcome,
    TelegramSendResult,
    send_telegram_message,
)
from app.services.telegram_vacancy_feed import (
    CARD_SOFT_LIMIT,
    DETAILS_SOFT_LIMIT,
    VacancyCard,
    build_callback_data,
    build_card_keyboard,
    build_vacancy_card,
    deliver_queued_vacancy_cards,
    parse_callback_data,
    render_vacancy_card,
    render_vacancy_details,
)

TOKEN = "AbCdEfGh12345_-x"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vacancy_feed.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _settings(**overrides) -> Settings:
    data = dict(
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
        telegram_vacancy_feed_enabled=True,
        min_job_score_to_notify=80,
        bundesagentur_api_key="upstream-key",
        company_research_auto_enabled=False,
    )
    data.update(overrides)
    return Settings(**data)


def _record(**overrides) -> JobRecord:
    data = dict(
        id=1,
        fingerprint="f" * 64,
        source="bundesagentur",
        title="Junior Python Developer",
        company="Example GmbH",
        location="Frankfurt am Main",
        url="https://example.com/jobs/1",
        description="Wir suchen Verstärkung. Deutschkenntnisse B2 erforderlich.",
        skills_json="[]",
        data_confidence=0.9,
        skill_source="description_extracted",
        must_have_skills_json=json.dumps(["Python", "FastAPI", "SQL"]),
        nice_to_have_skills_json=json.dumps(["Docker"]),
        posting_type="ARBEIT",
        score=82,
        recommendation="APPLY",
        status="NEW",
    )
    data.update(overrides)
    return JobRecord(**data)


def _persist_job(db, *, suffix="1", score=90, recommendation="APPLY") -> JobRecord:
    job = Job(
        source="test",
        title=f"Python Developer {suffix}",
        company="Acme GmbH",
        url=f"https://example.com/jobs/{suffix}",
        must_have_skills=["Python", "SQL"],
    )
    record, _ = upsert_job(db, job, JobScore(score=score, recommendation=recommendation))
    record.must_have_skills_json = json.dumps(["Python", "SQL"])
    db.commit()
    return record


class FakeSender:
    """Replaces send_telegram_message; scripts per-call outcomes."""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls: list[dict] = []

    async def __call__(self, bot_token, chat_id, text, *, reply_markup=None, timeout_seconds=5.0):
        self.calls.append({"text": text, "reply_markup": reply_markup, "chat_id": chat_id})
        outcome = self.outcomes.pop(0) if self.outcomes else TelegramSendOutcome.SENT
        message_id = 1000 + len(self.calls) if outcome is TelegramSendOutcome.SENT else None
        return TelegramSendResult(outcome, message_id)


async def _no_sleep(_seconds):
    return None


@pytest.fixture()
def sender(monkeypatch):
    fake = FakeSender()
    monkeypatch.setattr(feed, "send_telegram_message", fake)
    monkeypatch.setattr(
        feed, "get_candidate_skills_for_scoring", lambda db: frozenset({"python", "sql"})
    )
    return fake


class TestRendering:
    def test_full_card_matches_expected_layout(self):
        card = build_vacancy_card(_record(), {"python", "fastapi", "sql"})
        text = render_vacancy_card(card)

        assert text.splitlines()[:5] == [
            "🐍 Junior Python Developer",
            "Firma: Example GmbH",
            "📍 Frankfurt am Main",
            "🇩🇪 Deutsch: B2",
            "🔎 Quelle: bundesagentur",
        ]
        assert "Match: 82%" in text
        assert "✅ Python\n✅ FastAPI\n✅ SQL\n⚠️ Docker preferred" in text
        assert "Why it may fit:" in text
        assert "- 3 of 3 required technologies in your profile: Python, FastAPI, SQL" in text
        assert text.endswith("https://example.com/jobs/1")

    def test_missing_required_skill_is_flagged_not_hidden(self):
        card = build_vacancy_card(_record(), {"python"})
        text = render_vacancy_card(card)

        assert "❌ FastAPI required" in text
        assert "Watch out:" in text
        assert "Required but not in your profile: FastAPI, SQL" in text

    def test_optional_fields_absent(self):
        record = _record(
            location="",
            description="",
            must_have_skills_json="[]",
            nice_to_have_skills_json="[]",
            title="Backend Engineer",
        )
        text = render_vacancy_card(build_vacancy_card(record, set()))

        assert "📍 Ort nicht angegeben" in text
        assert "Deutsch" not in text
        assert "💰" not in text
        assert text.startswith("💼 Backend Engineer")
        assert "No technologies could be extracted" in text

    def test_salary_rendered_only_when_present(self):
        with_salary = VacancyCard(
            title="Dev", company="X", source="s", url="https://e.com", score=80, salary="45–52k"
        )
        without = VacancyCard(title="Dev", company="X", source="s", url="https://e.com", score=80)

        assert "💰 45–52k" in render_vacancy_card(with_salary)
        assert "💰" not in render_vacancy_card(without)

    def test_high_german_level_and_low_confidence_warnings(self):
        record = _record(
            description="Deutsch C1 zwingend erforderlich.",
            data_confidence=0.3,
            posting_type="SELBSTAENDIGKEIT",
        )
        text = render_vacancy_card(build_vacancy_card(record, {"python"}))

        assert "🇩🇪 Deutsch: C1" in text
        assert "High German level required: C1" in text
        assert "Low data confidence (0.30)" in text
        assert "Posting type: SELBSTAENDIGKEIT" in text

    def test_long_fields_are_truncated_and_card_stays_under_limit(self):
        many_skills = [f"Skill{i}" * 10 for i in range(200)]
        record = _record(
            title="T" * 5000,
            company="C" * 5000,
            location="L" * 5000,
            must_have_skills_json=json.dumps(many_skills),
            nice_to_have_skills_json=json.dumps(many_skills[:50]),
            url="https://example.com/" + "u" * 5000,
        )
        text = render_vacancy_card(build_vacancy_card(record, set()))

        assert len(text) <= CARD_SOFT_LIMIT
        first_line = text.splitlines()[0]
        assert len(first_line) <= 210 and first_line.endswith("…")
        assert "(+188 more - see Details)" in text
        assert "(link too long - see Details)" in text

    def test_oversized_sections_are_dropped_before_url(self):
        card = VacancyCard(
            title="Dev",
            company="X",
            source="s",
            url="https://e.com/job",
            score=80,
            reasons=["r" * 199] * 5,
            warnings=["w" * 199] * 5,
            technologies=[],
        )
        # Force the card over the limit by shrinking the limit for this test.
        original = feed.CARD_SOFT_LIMIT
        try:
            feed.CARD_SOFT_LIMIT = 1200
            text = feed.render_vacancy_card(card)
        finally:
            feed.CARD_SOFT_LIMIT = original

        assert len(text) <= 1200
        assert "Watch out:" not in text
        assert text.endswith("https://e.com/job")

    def test_details_view_is_bounded(self):
        record = _record(description="D" * 20000)
        text = render_vacancy_details(record)

        assert len(text) <= DETAILS_SOFT_LIMIT
        assert "Required: Python, FastAPI, SQL" in text
        assert text.endswith("https://example.com/jobs/1")


class TestCallbackData:
    def test_keyboard_uses_only_opaque_token_and_fits_telegram_limit(self):
        keyboard = build_card_keyboard(TOKEN)
        buttons = [b for row in keyboard["inline_keyboard"] for b in row]

        assert [b["text"] for b in buttons] == [
            "✅ Bewerbung erstellen",
            "📄 Details",
            "⭐ Speichern",
            "❌ Überspringen",
        ]
        for button in buttons:
            assert len(button["callback_data"].encode()) <= 64
            assert button["callback_data"].endswith(TOKEN)

    def test_parse_round_trip(self):
        assert parse_callback_data(build_callback_data("s", TOKEN)) == ("s", TOKEN)

    @pytest.mark.parametrize(
        "data",
        [None, "", "vf", "vf:s", "xx:s:" + TOKEN, "vf:z:" + TOKEN, "vf:s:" + TOKEN + ":1"],
    )
    def test_malformed_callback_data_rejected(self, data):
        assert parse_callback_data(data) is None


class TestDelivery:
    @pytest.mark.asyncio
    async def test_queued_card_is_sent_once_with_keyboard(self, db, sender):
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)

        stats = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)
        again = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        assert stats.sent == 1 and again.sent == 0
        assert len(sender.calls) == 1
        review = get_review_for_job(db, job.id)
        assert review.state == "TELEGRAM_SENT"
        assert review.telegram_message_id == 1001
        assert sender.calls[0]["reply_markup"] == build_card_keyboard(review.callback_token)

    @pytest.mark.asyncio
    async def test_failed_send_is_retried_on_next_sweep_without_new_rows(self, db, sender):
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)
        sender.outcomes = [TelegramSendOutcome.FAILED]

        first = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)
        review = get_review_for_job(db, job.id)
        assert first.failed == 1
        assert review.state == "QUEUED_FOR_REVIEW"

        second = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)
        db.refresh(review)
        assert second.sent == 1
        assert review.state == "TELEGRAM_SENT"
        assert review.attempt_count == 2
        assert db.scalar(select(func.count(TelegramVacancyReviewRecord.id))) == 1

    @pytest.mark.asyncio
    async def test_permanently_failing_send_stops_after_max_attempts(self, db, sender):
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)
        sender.outcomes = [TelegramSendOutcome.FAILED] * (MAX_DELIVERY_ATTEMPTS + 3)

        for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
            await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        assert len(sender.calls) == MAX_DELIVERY_ATTEMPTS
        assert get_review_for_job(db, job.id).state == "DELIVERY_FAILED"

    @pytest.mark.asyncio
    async def test_uncertain_send_is_never_retried(self, db, sender):
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)
        sender.outcomes = [TelegramSendOutcome.UNCERTAIN]

        await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)
        await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        assert len(sender.calls) == 1
        assert get_review_for_job(db, job.id).state == "DELIVERY_UNCERTAIN"

    @pytest.mark.asyncio
    async def test_card_claimed_by_another_sweep_is_not_sent(self, db, sender):
        job = _persist_job(db)
        review = ensure_review(db, job.id, eligible=True)
        claim_for_sending(db, review)  # a concurrent sweep holds the claim

        await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_job_no_longer_eligible_is_dequeued_not_sent(self, db, sender):
        job = _persist_job(db)
        review = ensure_review(db, job.id, eligible=True)
        job.status = "APPLIED"
        db.commit()

        stats = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        assert sender.calls == []
        assert stats.dequeued == 1
        db.refresh(review)
        assert review.state == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_per_run_cap_leaves_rest_queued(self, db, sender):
        for i in range(3):
            ensure_review(db, _persist_job(db, suffix=str(i)).id, eligible=True)

        await deliver_queued_vacancy_cards(
            db, _settings(telegram_vacancy_feed_max_per_run=2), sleep=_no_sleep
        )

        assert len(sender.calls) == 2
        states = db.scalars(select(TelegramVacancyReviewRecord.state)).all()
        assert sorted(states) == ["QUEUED_FOR_REVIEW", "TELEGRAM_SENT", "TELEGRAM_SENT"]

    @pytest.mark.asyncio
    async def test_lease_lost_stops_further_sends(self, db, sender):
        for i in range(3):
            ensure_review(db, _persist_job(db, suffix=str(i)).id, eligible=True)
        checks = {"n": 0}

        def is_lease_lost():
            checks["n"] += 1
            return checks["n"] > 1

        await deliver_queued_vacancy_cards(
            db, _settings(), is_lease_lost=is_lease_lost, sleep=_no_sleep
        )

        assert len(sender.calls) == 1

    @pytest.mark.asyncio
    async def test_unconfigured_telegram_sends_nothing_and_keeps_queue(self, db, sender):
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)

        await deliver_queued_vacancy_cards(db, _settings(telegram_chat_id=""), sleep=_no_sleep)

        assert sender.calls == []
        assert get_review_for_job(db, job.id).state == "QUEUED_FOR_REVIEW"


class FakeScorer:
    def __init__(self, profile_skills):
        pass

    def score(self, job: Job) -> JobScore:
        return JobScore(
            score=90,
            recommendation="APPLY",
            data_confidence=0.9,
            matched_must_have=["python", "fastapi"],
        )


class FakeCollector:
    def __init__(self, jobs):
        self._jobs = jobs
        self.skipped_invalid_count = 0

    async def fetch(self, since=None):
        return self._jobs

    async def fetch_detail(self, referenznummer):
        return None


class ExplodingNotifier:
    """The legacy notifier must never be used while the feed is enabled."""

    def __init__(self, *args, **kwargs):
        pass

    async def send_job(self, job, score):
        raise AssertionError("legacy send_job called while vacancy feed enabled")


class TestCollectorIntegration:
    def _patch(self, monkeypatch, jobs):
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeCollector(jobs),
        )
        monkeypatch.setattr("app.agents.job_score_evaluator.JobScorer", FakeScorer)
        monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", ExplodingNotifier)

    def _job(self):
        return Job(
            source="bundesagentur",
            title="Python Developer",
            company="First Company",
            url="https://www.arbeitsagentur.de/jobsuche/jobdetail/first",
            description="",
            skills=["python"],
        )

    @pytest.mark.asyncio
    async def test_same_vacancy_collected_twice_is_carded_once(self, db, sender, monkeypatch):
        self._patch(monkeypatch, [self._job()])

        first = await run_bundesagentur(db, _settings())
        second = await run_bundesagentur(db, _settings())

        assert first["created"] == 1 and second["updated"] == 1
        assert len(sender.calls) == 1
        assert db.scalar(select(func.count(TelegramVacancyReviewRecord.id))) == 1

    @pytest.mark.asyncio
    async def test_failed_send_is_retried_by_next_collector_run(self, db, sender, monkeypatch):
        self._patch(monkeypatch, [self._job()])
        sender.outcomes = [TelegramSendOutcome.FAILED]

        first = await run_bundesagentur(db, _settings())
        # The job is persisted even though Telegram failed.
        assert first["created"] == 1
        assert db.scalar(select(func.count(JobRecord.id))) == 1

        await run_bundesagentur(db, _settings())

        assert len(sender.calls) == 2
        review = db.scalars(select(TelegramVacancyReviewRecord)).one()
        assert review.state == "TELEGRAM_SENT"

    @pytest.mark.asyncio
    async def test_delivery_exception_never_fails_the_run(self, db, sender, monkeypatch):
        self._patch(monkeypatch, [self._job()])

        async def boom(*args, **kwargs):
            raise RuntimeError("telegram exploded")

        monkeypatch.setattr("app.services.collector_runner.deliver_queued_vacancy_cards", boom)

        stats = await run_bundesagentur(db, _settings())

        assert stats["created"] == 1 and stats["failed"] == 0
        review = db.scalars(select(TelegramVacancyReviewRecord)).one()
        assert review.state == "QUEUED_FOR_REVIEW"

    @pytest.mark.asyncio
    async def test_feed_disabled_keeps_legacy_path_and_writes_no_review_rows(
        self, db, sender, monkeypatch
    ):
        self._patch(monkeypatch, [self._job()])
        calls = []

        class LegacyNotifier:
            def __init__(self, *args, **kwargs):
                pass

            async def send_job(self, job, score):
                calls.append(job.title)
                return True

        monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", LegacyNotifier)

        await run_bundesagentur(db, _settings(telegram_vacancy_feed_enabled=False))

        assert calls == ["Python Developer"]
        assert sender.calls == []
        assert db.scalar(select(func.count(TelegramVacancyReviewRecord.id))) == 0


class _FakeAsyncClient:
    def __init__(self, response, **_kwargs):
        self.response = response
        self.payloads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        self.payloads.append(json)
        return self.response


class TestSendTelegramMessage:
    @pytest.mark.asyncio
    async def test_returns_message_id_and_sends_reply_markup(self, monkeypatch):
        response = httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42}},
            request=httpx.Request("POST", "https://api.telegram.org/x"),
        )
        client = _FakeAsyncClient(response)
        monkeypatch.setattr("app.services.telegram.httpx.AsyncClient", lambda **kw: client)

        result = await send_telegram_message("t", "1", "hi", reply_markup={"inline_keyboard": []})

        assert result == TelegramSendResult(TelegramSendOutcome.SENT, 42)
        assert client.payloads[0]["reply_markup"] == {"inline_keyboard": []}
        assert "parse_mode" not in client.payloads[0]

    @pytest.mark.asyncio
    async def test_unparseable_success_body_is_still_sent(self, monkeypatch):
        response = httpx.Response(200, request=httpx.Request("POST", "https://api.telegram.org/x"))
        monkeypatch.setattr(
            "app.services.telegram.httpx.AsyncClient", lambda **kw: _FakeAsyncClient(response)
        )

        result = await send_telegram_message("t", "1", "hi")

        assert result == TelegramSendResult(TelegramSendOutcome.SENT, None)


class _AcceptingClient:
    """Stands in for httpx.AsyncClient inside the REAL
    `send_telegram_message`: every POST is recorded as externally accepted
    (the card reached the chat) and then answered with the next scripted
    HTTP status -- simulating a gateway that forwards the POST to Telegram
    but returns an error to us (Codex S9A-CODEX-001)."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.accepted: list[dict] = []

    def __call__(self, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        self.accepted.append(json)
        status = self.statuses.pop(0) if self.statuses else 200
        body = (
            {"ok": True, "result": {"message_id": 77}}
            if status == 200
            else {"ok": False, "error_code": status, "description": "error"}
        )
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


@pytest.fixture()
def real_sender_client(monkeypatch):
    """Uses the REAL sender; only the HTTP client is replaced."""

    def install(statuses):
        client = _AcceptingClient(statuses)
        monkeypatch.setattr("app.services.telegram.httpx.AsyncClient", client)
        monkeypatch.setattr(
            feed, "get_candidate_skills_for_scoring", lambda db: frozenset({"python", "sql"})
        )
        return client

    return install


class TestRealSenderHttpClassification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [500, 502, 503, 504])
    async def test_gateway_error_after_acceptance_is_uncertain_and_never_resent(
        self, db, real_sender_client, status_code
    ):
        client = real_sender_client([status_code])
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)

        first = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        review = get_review_for_job(db, job.id)
        assert review.state == "DELIVERY_UNCERTAIN"
        assert first.uncertain == 1 and first.failed == 0
        assert len(client.accepted) == 1

        second = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        db.refresh(review)
        assert len(client.accepted) == 1  # zero new sendMessage calls
        assert second.sent == 0 and second.failed == 0 and second.uncertain == 0
        assert review.state == "DELIVERY_UNCERTAIN"
        assert review.attempt_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429])
    async def test_definitive_rejection_is_retried_on_next_sweep(
        self, db, real_sender_client, status_code
    ):
        client = real_sender_client([status_code, 200])
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)

        first = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)
        review = get_review_for_job(db, job.id)
        assert first.failed == 1
        assert review.state == "QUEUED_FOR_REVIEW"

        second = await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)
        db.refresh(review)
        assert second.sent == 1
        assert review.state == "TELEGRAM_SENT"
        assert review.telegram_message_id == 77
        assert len(client.accepted) == 2

    @pytest.mark.asyncio
    async def test_repeated_definitive_rejection_stops_at_max_attempts(
        self, db, real_sender_client
    ):
        client = real_sender_client([400] * (MAX_DELIVERY_ATTEMPTS + 3))
        job = _persist_job(db)
        ensure_review(db, job.id, eligible=True)

        for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
            await deliver_queued_vacancy_cards(db, _settings(), sleep=_no_sleep)

        assert len(client.accepted) == MAX_DELIVERY_ATTEMPTS
        assert get_review_for_job(db, job.id).state == "DELIVERY_FAILED"
