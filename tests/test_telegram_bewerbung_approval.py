"""Stage 9C: Telegram human review / approval of the EXACT Stage 9B package
through Stage 6E. Exercises the REAL 6B/6C/6D/6E services on file-backed
SQLite with the deterministic provider, a fake Telegram transport, and a
network guard that fails on any non-Telegram outbound call.

Concurrency note: SQLite ignores `FOR UPDATE` and serializes writers, so
the "interleaving" tests below inject a competing committed write at a
deterministic point (a hook) and verify that the CAS / lock-time checks
reject it. They are interleavings, NOT concurrency evidence; real
overlapping transactions are exercised only by
tests/integration/test_telegram_bewerbung_approval_postgres.py."""

import ast
import asyncio
import concurrent.futures
import imaplib
import json
import logging
import smtplib
import socket
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.services.telegram_bewerbung as tb
import app.services.telegram_bewerbung_approval as ta
import app.services.telegram_bot as bot
from app.db.base import Base
from app.db.models import (
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
    BewerbungDraftRecord,
    JobRecord,
    TelegramBewerbungApprovalRecord,
    TelegramVacancyReviewRecord,
)
from app.db.models import TelegramBewerbungPreparationRecord as Prep
from app.db.telegram_bewerbung_approval_repository import insert_link
from app.models.candidate_profile import CandidateSkill
from app.models.review_package import BewerbungContentPatch
from app.providers.bewerbung.deterministic import DeterministicBewerbungProvider
from app.services.bewerbung import BewerbungService
from app.services.review_package import ReviewPackageService
from app.services.telegram import TelegramSendOutcome
from tests.test_review_package_builder import _bewerbung_draft, _cv_draft
from tests.test_telegram_bewerbung import (
    CHAT_ID,
    STRANGER_CHAT_ID,
    FakeSender,
    _job,
    _profile,
    _review,
    _update,
    _update_db,
)
from tests.test_telegram_bewerbung import _settings as _settings_9b
from tests.test_telegram_bewerbung_preview import _match

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides):
    data = {"telegram_bewerbung_approval_enabled": True}
    data.update(overrides)
    return _settings_9b(**data)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'approval_flow.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    """Stage 9C may only call the (faked) Telegram sender. Any real socket,
    SMTP, IMAP, HTTP client or company-research fetch fails the test."""

    def forbidden(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"forbidden outbound/side-effect call: {name}")

        return _raise

    real_connect = socket.socket.connect

    def loopback_only_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"forbidden outbound/side-effect call: connect {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only_connect)
    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))
    monkeypatch.setattr(smtplib, "SMTP", forbidden("smtplib.SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden("smtplib.SMTP_SSL"))
    monkeypatch.setattr(imaplib, "IMAP4", forbidden("imaplib.IMAP4"))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", forbidden("imaplib.IMAP4_SSL"))
    monkeypatch.setattr(httpx, "AsyncClient", forbidden("httpx.AsyncClient"))
    monkeypatch.setattr(httpx, "Client", forbidden("httpx.Client"))
    monkeypatch.setattr(
        "app.services.company_research.CompanyResearchService.get_or_run",
        forbidden("CompanyResearchService.get_or_run"),
    )
    monkeypatch.setattr(
        "app.db.review_package_repository.get_latest_approved_review_for_job",
        forbidden("get_latest_approved_review_for_job"),
    )


@pytest.fixture()
def sender():
    return FakeSender()


@pytest.fixture()
def seeded(session_factory):
    db = session_factory()
    try:
        _profile(db)
        job = _job(db)
        review = _review(db, job.id)
        return {"job_id": job.id, "review_id": review.id}
    finally:
        db.close()


async def _prepare(session_factory, seeded, sender) -> str:
    """Stage 9B: prepare + publish a package; returns its package token."""
    outcome = await tb.handle_apply(session_factory, _settings(), seeded["review_id"], send=sender)
    assert outcome.code in ("PREVIEW_SENT", "ALREADY_SHOWN"), outcome.code
    return _prep(session_factory).package_token


async def _request(session_factory, token, sender, **settings):
    return await ta.handle_request_review(
        session_factory, _settings(**settings), token, send=sender
    )


async def _page(session_factory, capability, page, sender):
    return await ta.handle_review_page(session_factory, _settings(), capability, page, send=sender)


async def _decide(session_factory, capability, action):
    return await ta.handle_decision(session_factory, _settings(), capability, action)


@pytest_asyncio.fixture()
async def requested(session_factory, seeded, sender):
    """A prepared package with its Telegram-linked review displayed."""
    token = await _prepare(session_factory, seeded, sender)
    outcome = await _request(session_factory, token, sender)
    assert outcome.code == "REVIEW_SHOWN", outcome
    link = _links(session_factory)[0]
    return {**seeded, "token": token, "capability": link.approval_capability, "link": link}


def _rows(session_factory, model, *order):
    db = session_factory()
    try:
        stmt = select(model).order_by(*(order or (model.id,)))
        return list(db.scalars(stmt))
    finally:
        db.close()


def _links(session_factory):
    return _rows(session_factory, TelegramBewerbungApprovalRecord)


def _reviews(session_factory):
    return _rows(session_factory, ApplicationPackageReviewRecord)


def _revisions(session_factory):
    return _rows(session_factory, ApplicationPackageReviewRevisionRecord)


def _prep(session_factory) -> Prep:
    db = session_factory()
    try:
        return db.scalar(select(Prep))
    finally:
        db.close()


def _prep_snapshot(session_factory) -> dict:
    prep = _prep(session_factory)
    return {column.name: getattr(prep, column.name) for column in Prep.__table__.columns}


def _review_row(session_factory, review_id) -> ApplicationPackageReviewRecord:
    db = session_factory()
    try:
        return db.get(ApplicationPackageReviewRecord, review_id)
    finally:
        db.close()


def _assert_isolated(session_factory, seeded, prep_before=None, job_status="NEW"):
    db = session_factory()
    try:
        assert db.get(JobRecord, seeded["job_id"]).status == job_status
        assert db.get(TelegramVacancyReviewRecord, seeded["review_id"]).state == "TELEGRAM_SENT"
    finally:
        db.close()
    if prep_before is not None:
        assert _prep_snapshot(session_factory) == prep_before


def _letter_json(session_factory, letter_id) -> dict:
    db = session_factory()
    try:
        return json.loads(db.get(BewerbungDraftRecord, letter_id).draft_json)
    finally:
        db.close()


def _change_job(session_factory, seeded, **values):
    _update_db(
        session_factory,
        update(JobRecord).where(JobRecord.id == seeded["job_id"]).values(**values),
    )


def _bump_profile(session_factory):
    db = session_factory()
    try:
        _profile(db, skills=[CandidateSkill(name="Python"), CandidateSkill(name="SQL")])
    finally:
        db.close()


# Unpatched Stage 6E service methods: interleaving hooks patch the class, and
# the competing "other worker" must still use the real implementation.
_REAL_6E = {
    name: getattr(ReviewPackageService, name) for name in ("create", "patch", "approve", "reject")
}


def _api(session_factory, method, *args, **kwargs):
    db = session_factory()
    try:
        return _REAL_6E[method](ReviewPackageService(), db, *args, **kwargs)
    finally:
        db.close()


def _run_in_thread(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _regenerate(session_factory, seeded, company="Replacement Company") -> str:
    """A legitimate new Stage 9B generation (changed input, Apply again)."""
    _change_job(session_factory, seeded, company=company)
    outcome = await tb.handle_apply(
        session_factory, _settings(), seeded["review_id"], send=FakeSender()
    )
    assert outcome.code == "PREVIEW_SENT", outcome
    return _prep(session_factory).package_token


# --- callback parsing --------------------------------------------------------


class TestCallbackParsing:
    CAP = "AbCdEfGh_-123456"
    TOKEN = "PkgTokenAbc_-789"

    def test_valid_round_trips_fit_telegram_limit(self):
        request = ta.build_request_callback_data(self.TOKEN)
        page = ta.build_page_callback_data(self.CAP, 30)
        approve = ta.build_decision_callback_data(self.CAP, "f")
        reject = ta.build_decision_callback_data(self.CAP, "x")
        assert ta.parse_request_callback_data(request) == self.TOKEN
        assert ta.parse_page_callback_data(page) == (self.CAP, 30)
        assert ta.parse_decision_callback_data(approve) == (self.CAP, "f")
        assert ta.parse_decision_callback_data(reject) == (self.CAP, "x")
        for data in (request, page, approve, reject):
            assert len(data.encode("utf-8")) < 64

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "",
            "ba",
            "ba:",
            "ba:123",
            "ba:42:1",
            "bp:PkgTokenAbc_-789",
            "ba:PkgTokenAbc_-78",
            "ba:PkgTokenAbc_-7890",
            "ba:PkgTokenAbc_-78=",
            "ba:PkgTokenAbc_-78 ",
            "BA:PkgTokenAbc_-789",
        ],
    )
    def test_request_parser_is_strict(self, data):
        assert ta.parse_request_callback_data(data) is None

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "bv:AbCdEfGh_-123456",
            "bv:AbCdEfGh_-123456:0",
            "bv:AbCdEfGh_-123456:31",
            "bv:AbCdEfGh_-123456:01",
            "bv:AbCdEfGh_-123456:-1",
            "bv:AbCdEfGh_-123456:1:2",
            "bv:AbCdEfGh_-123456:١",
            "bv:12:1",
            "bv:AbCdEfGh_-12345!:1",
            "bz:AbCdEfGh_-123456:1",
        ],
    )
    def test_page_parser_is_strict(self, data):
        assert ta.parse_page_callback_data(data) is None

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "bz:AbCdEfGh_-123456",
            "bz:AbCdEfGh_-123456:a",
            "bz:AbCdEfGh_-123456:F",
            "bz:AbCdEfGh_-123456:fx",
            "bz:AbCdEfGh_-123456:f:",
            "bz:7:f",
            "bz:1234567890123456789:f",
            "bz:AbCdEfGh_-12345=:f",
            "bv:AbCdEfGh_-123456:f",
        ],
    )
    def test_decision_parser_is_strict(self, data):
        assert ta.parse_decision_callback_data(data) is None

    def test_decision_buttons_only_on_final_page(self):
        first = ta.review_page_keyboard(self.CAP, 1, 3, ta.MODE_DECIDE)
        middle = ta.review_page_keyboard(self.CAP, 2, 3, ta.MODE_DECIDE)
        last = ta.review_page_keyboard(self.CAP, 3, 3, ta.MODE_DECIDE)

        def datas(markup):
            return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]

        assert all(not d.startswith("bz:") for d in datas(first) + datas(middle))
        assert f"bz:{self.CAP}:f" in datas(last) and f"bz:{self.CAP}:x" in datas(last)
        reject_only = datas(ta.review_page_keyboard(self.CAP, 1, 1, ta.MODE_REJECT_ONLY))
        assert reject_only == [f"bz:{self.CAP}:x"]
        assert ta.review_page_keyboard(self.CAP, 1, 1, None) is None


# --- complete review renderer -----------------------------------------------


def _snapshot(cv=None, letter=None, match=None, **kw) -> ta.ReviewSnapshot:
    from app.agents.review_package_builder import (
        build_initial_reviewed_bewerbung,
        build_initial_reviewed_cv,
    )

    cv = cv or _cv_draft()
    letter = letter or _bewerbung_draft()
    return ta.ReviewSnapshot(
        reviewed_cv=kw.pop("reviewed_cv", build_initial_reviewed_cv(cv)),
        reviewed_bewerbung=kw.pop("reviewed_bewerbung", build_initial_reviewed_bewerbung(letter)),
        cv=cv,
        match=match or _match(),
        job_title=kw.pop("job_title", "Junior Python Developer"),
        job_company=kw.pop("job_company", "Example GmbH"),
        letter_warnings=kw.pop("letter_warnings", ()),
    )


def _full_cv(**overrides):
    from datetime import date

    from app.models.cv_draft import (
        CVCertificationItem,
        CVEducationItem,
        CVExperienceItem,
        CVLanguageItem,
        CVProjectItem,
        CVSkillItem,
    )

    data = dict(
        section_order=["HEADER", "SUMMARY", "SKILLS"],  # 5 sections NOT named
        skills=[
            CVSkillItem(
                text=f"Skill-{i}",
                category="Backend",
                proficiency="ADVANCED",
                years_experience=2.5,
                source_id=i,
                match_requirement="Python",
                importance="REQUIRED",
            )
            for i in range(3)
        ],
        experience=[
            CVExperienceItem(
                source_id=1,
                company="Exp-Company",
                job_title="Exp-Title",
                start_date=date(2020, 1, 1),
                end_date=None,
                is_current=True,
                location="Berlin",
                description="Exp-Description line one\nline two",
                responsibilities=["Resp-A", "Resp-B"],
                achievements=["Ach-A"],
                technologies=["Tech-A"],
                matched_skills=["Python"],
            )
        ],
        projects=[
            CVProjectItem(
                source_id=1,
                name="Proj-Name",
                description="Proj-Description",
                role="Proj-Role",
                technologies=["Proj-Tech"],
                repository_url="https://example.com/repo",
                demo_url="https://example.com/demo",
                start_date=None,
                end_date=None,
                highlights=["Proj-Highlight"],
                matched_skills=["SQL"],
            )
        ],
        education=[
            CVEducationItem(
                source_id=1,
                institution="Edu-Inst",
                program="Edu-Program",
                degree="Edu-Degree",
                field_of_study="Edu-Field",
                start_date=None,
                end_date=None,
                completed=True,
                location="Edu-City",
            )
        ],
        certifications=[
            CVCertificationItem(
                source_id=1,
                name="Cert-Name",
                issuer="Cert-Issuer",
                issued_date=date(2021, 5, 1),
                expires_date=None,
                status="ACTIVE",
            )
        ],
        languages=[
            CVLanguageItem(source_id=1, language="Lang-DE", level="C1", certificate="Lang-Cert")
        ],
        warnings=["CV_WARNING_CODE"],
    )
    data.update(overrides)
    return _cv_draft(**data)


def _all_text(document: ta.ReviewDocument) -> str:
    return "\n".join(
        ta.review_page_text(document, page, stale=False) for page in range(1, document.total + 1)
    )


class TestReviewRenderer:
    def test_every_letter_paragraph_and_cv_item_and_trailing_section(self):
        from app.models.bewerbung import BewerbungParagraph

        paragraphs = [
            BewerbungParagraph(text=f"Paragraph-{i}", source_claim_ids=[]) for i in range(7)
        ]
        snapshot = _snapshot(
            cv=_full_cv(),
            letter=_bewerbung_draft(body_paragraphs=paragraphs),
            letter_warnings=("LETTER_WARNING_CODE",),
        )
        document = ta.render_review_document(snapshot)
        text = _all_text(document)

        assert document.decidable
        for i in range(7):
            assert f"Paragraph-{i}" in text
        for needle in (
            "Skill-0",
            "Skill-2",
            "Exp-Title",
            "Exp-Company",
            "line two",
            "Resp-B",
            "Ach-A",
            "Tech-A",
            "Proj-Name",
            "Proj-Highlight",
            "https://example.com/repo",
            "Edu-Inst",
            "Edu-Field",
            "Cert-Name",
            "Cert-Issuer",
            "Lang-DE",
            "Lang-Cert",
            "CV_WARNING_CODE",
            "LETTER_WARNING_CODE",
            "Kubernetes",  # missing requirement shown as a labeled gap
            "Nicht belegt (wird nicht behauptet):",
            "Junior Python Developer",
            "Backend-focused developer.",
            ta.NOTHING_SENT_FOOTER,
        ):
            assert needle in text, needle
        # Sections the revision's section_order does not name still render,
        # in the fixed trailing order.
        order = [text.index(h) for h in ("Skills:", "Berufserfahrung:", "Projekte:", "Ausbildung:")]
        assert order == sorted(order)

    def test_revision_section_order_is_respected(self):
        assert ta.cv_section_sequence(["LANGUAGES", "SKILLS", "BOGUS", "SKILLS"]) == [
            "LANGUAGES",
            "SKILLS",
            "HEADER",
            "SUMMARY",
            "EXPERIENCE",
            "PROJECTS",
            "EDUCATION",
            "CERTIFICATIONS",
        ]

    def test_pages_are_bounded_in_utf16_units_with_emoji_and_long_values(self):
        from app.models.bewerbung import BewerbungParagraph
        from app.services.telegram_bewerbung_preview import utf16_units

        emoji = "👩‍💻🚀" * 900  # non-BMP + ZWJ
        unbroken = "X" * 9000
        paragraphs = [
            BewerbungParagraph(text=emoji, source_claim_ids=[]),
            BewerbungParagraph(text=unbroken, source_claim_ids=[]),
            BewerbungParagraph(text="Wort " * 1500, source_claim_ids=[]),
        ]
        document = ta.render_review_document(
            _snapshot(letter=_bewerbung_draft(body_paragraphs=paragraphs))
        )
        assert document.decidable and document.total > 3
        for page in range(1, document.total + 1):
            for stale in (False, True):
                text = ta.review_page_text(document, page, stale=stale)
                assert utf16_units(text) <= 3500
                assert "\ud83d" not in text.encode("utf-16-le", "surrogatepass").decode(
                    "utf-16-le", "replace"
                )
        joined = "".join("".join(body.split()) for body in document.bodies)
        assert "X" * 9000 in joined  # hard-split, never dropped
        assert emoji in joined

    def test_bidi_and_control_characters_are_normalized(self):
        snapshot = _snapshot(job_company="Evil‮Corp\x07​", job_title="Dev\nInjected: line")
        text = _all_text(ta.render_review_document(snapshot))
        assert "‮" not in text and "\x07" not in text and "​" not in text
        assert "Firma: EvilCorp" in text
        assert "Position: Dev Injected: line" in text

    def test_pagination_is_deterministic(self):
        snapshot = _snapshot(cv=_full_cv())
        assert ta.render_review_document(snapshot) == ta.render_review_document(snapshot)

    def test_more_than_max_pages_is_too_large_never_truncated(self, monkeypatch):
        from app.models.bewerbung import BewerbungParagraph

        big = [BewerbungParagraph(text="Wort " * 700, source_claim_ids=[])] * 40
        document = ta.render_review_document(
            _snapshot(letter=_bewerbung_draft(body_paragraphs=big))
        )
        assert document.too_large and not document.decidable and document.bodies == ()

    def test_completeness_failure_is_not_decidable(self, monkeypatch):
        real = ta._paginate_lines

        def dropping(lines, budget):
            return real([line for line in lines if "Ich bringe" not in line], budget)

        monkeypatch.setattr(ta, "_paginate_lines", dropping)
        document = ta.render_review_document(_snapshot())
        assert not document.complete and not document.decidable


# --- bot boundary ------------------------------------------------------------


@pytest.fixture()
def bot_env(monkeypatch, session_factory, sender):
    state = {"settings": _settings()}
    monkeypatch.setattr(bot, "SessionLocal", session_factory)
    monkeypatch.setattr(bot, "get_settings", lambda: state["settings"])
    monkeypatch.setattr(bot, "send_telegram_message", sender)
    return state


def _no_review_writes(session_factory):
    assert _links(session_factory) == []
    assert _reviews(session_factory) == []


class TestBotBoundary:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "flags",
        [
            {
                "telegram_bewerbung_draft_enabled": False,
                "telegram_bewerbung_approval_enabled": False,
            },
            {"telegram_bewerbung_draft_enabled": False},
            {"telegram_bewerbung_approval_enabled": False},
        ],
    )
    async def test_any_flag_off_writes_and_shows_nothing(
        self, bot_env, session_factory, seeded, sender, flags
    ):
        token = await _prepare(session_factory, seeded, sender)
        calls = len(sender.calls)
        bot_env["settings"] = _settings(**flags)
        for handler, data in (
            (bot.on_review_request_callback, f"ba:{token}"),
            (bot.on_review_page_callback, "bv:AbCdEfGh_-123456:1"),
            (bot.on_review_decision_callback, "bz:AbCdEfGh_-123456:f"),
        ):
            update_ = _update(data)
            await handler(update_, MagicMockContext())
            update_.callback_query.answer.assert_awaited_once_with(bot._APPROVAL_DISABLED)
        _no_review_writes(session_factory)
        assert len(sender.calls) == calls

    @pytest.mark.asyncio
    async def test_private_happy_path_creates_and_shows_review(
        self, bot_env, session_factory, seeded, sender
    ):
        token = await _prepare(session_factory, seeded, sender)
        await bot.on_review_request_callback(_update(f"ba:{token}"), MagicMockContext())

        assert len(_links(session_factory)) == 1 and len(_reviews(session_factory)) == 1
        page = sender.calls[-1]
        assert page["chat_id"] == str(CHAT_ID)
        assert ta.REVIEW_STATUS_LINE in page["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
    async def test_non_private_chats_fail_closed_before_any_write(
        self, bot_env, session_factory, seeded, sender, chat_type
    ):
        token = await _prepare(session_factory, seeded, sender)
        calls = len(sender.calls)
        for handler, data in (
            (bot.on_review_request_callback, f"ba:{token}"),
            (bot.on_review_page_callback, "bv:AbCdEfGh_-123456:1"),
            (bot.on_review_decision_callback, "bz:AbCdEfGh_-123456:f"),
        ):
            update_ = _update(data, chat_type=chat_type)
            await handler(update_, MagicMockContext())
            update_.callback_query.answer.assert_awaited_once_with(
                bot._PRIVATE_CHAT_ONLY, show_alert=True
            )
        _no_review_writes(session_factory)
        assert len(sender.calls) == calls

    @pytest.mark.asyncio
    async def test_unauthorized_or_missing_chat_gets_silence(
        self, bot_env, session_factory, seeded, sender
    ):
        token = await _prepare(session_factory, seeded, sender)
        calls = len(sender.calls)
        stranger = _update(f"ba:{token}", chat_id=STRANGER_CHAT_ID)
        missing = _update(f"ba:{token}")
        missing.effective_chat = None
        for update_ in (stranger, missing):
            await bot.on_review_request_callback(update_, MagicMockContext())
            update_.callback_query.answer.assert_not_awaited()
        _no_review_writes(session_factory)
        assert len(sender.calls) == calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "data"),
        [
            ("on_review_request_callback", "ba:1"),
            ("on_review_request_callback", "ba:42"),
            ("on_review_page_callback", "bv:12:1"),
            ("on_review_page_callback", "bv:AbCdEfGh_-123456:99"),
            ("on_review_decision_callback", "bz:5:f"),
            ("on_review_decision_callback", "bz:AbCdEfGh_-123456:approve"),
        ],
    )
    async def test_malformed_or_numeric_callbacks_touch_nothing(
        self, bot_env, session_factory, seeded, sender, handler_name, data
    ):
        update_ = _update(data)
        await getattr(bot, handler_name)(update_, MagicMockContext())
        update_.callback_query.answer.assert_awaited_once_with("Unbekannte Aktion.")
        _no_review_writes(session_factory)

    @pytest.mark.asyncio
    async def test_unknown_capability_is_rejected(self, session_factory, seeded, sender):
        outcome = await _decide(session_factory, "AbCdEfGh_-123456", "f")
        page = await _page(session_factory, "AbCdEfGh_-123456", 1, sender)
        assert outcome.code == page.code == "UNKNOWN_CAPABILITY"

    @pytest.mark.asyncio
    async def test_9b_keyboards_offer_zur_pruefung_only_when_9c_is_on(
        self, session_factory, seeded, sender
    ):
        await tb.handle_apply(
            session_factory,
            _settings(telegram_bewerbung_approval_enabled=False),
            seeded["review_id"],
            send=sender,
        )
        token = _prep(session_factory).package_token
        off_markup = json.dumps(sender.calls[-1]["reply_markup"])
        assert "ba:" not in off_markup
        await tb.handle_preview_page(session_factory, _settings(), token, 1, send=sender)
        on_markup = sender.calls[-1]["reply_markup"]
        assert {"text": "✅ Zur Prüfung", "callback_data": f"ba:{token}"} in [
            b for row in on_markup["inline_keyboard"] for b in row
        ]

    @pytest.mark.asyncio
    async def test_logs_never_contain_capabilities_tokens_or_content(
        self, bot_env, session_factory, seeded, sender, caplog
    ):
        caplog.set_level(logging.DEBUG)
        token = await _prepare(session_factory, seeded, sender)
        await bot.on_review_request_callback(_update(f"ba:{token}"), MagicMockContext())
        capability = _links(session_factory)[0].approval_capability
        await bot.on_review_page_callback(_update(f"bv:{capability}:1"), MagicMockContext())
        await bot.on_review_decision_callback(_update(f"bz:{capability}:f"), MagicMockContext())

        letter = _letter_json(session_factory, _prep(session_factory).bewerbung_draft_id)
        assert _reviews(session_factory)[0].status == "APPROVED"
        for secret in (token, capability, letter["opening"], "test-token"):
            assert secret not in caplog.text


class MagicMockContext:
    """A minimal stand-in for telegram's CallbackContext (unused here)."""

    bot = None


# --- creation ----------------------------------------------------------------


class TestCreation:
    @pytest.mark.asyncio
    async def test_first_request_creates_exactly_one_review_revision_and_link(
        self, session_factory, seeded, sender
    ):
        token = await _prepare(session_factory, seeded, sender)
        prep_before = _prep_snapshot(session_factory)

        outcome = await _request(session_factory, token, sender)

        assert outcome.code == "REVIEW_SHOWN"
        (link,) = _links(session_factory)
        (review,) = _reviews(session_factory)
        (revision,) = _revisions(session_factory)
        prep = _prep(session_factory)
        assert (link.preparation_id, link.generation, link.package_token) == (
            prep.id,
            prep.generation,
            token,
        )
        assert link.input_identity == prep.input_identity
        assert (link.review_id, link.bound_review_version) == (review.id, 1)
        assert (review.match_id, review.cv_draft_id, review.bewerbung_draft_id) == (
            prep.match_id,
            prep.cv_draft_id,
            prep.bewerbung_draft_id,
        )
        assert (review.status, review.review_version) == ("PENDING_REVIEW", 1)
        assert (revision.review_id, revision.revision_number) == (review.id, 1)
        reviewed = json.loads(revision.reviewed_bewerbung_json)
        letter = _letter_json(session_factory, prep.bewerbung_draft_id)
        assert reviewed["opening"]["value"] == letter["opening"]
        assert all(p["origin"] == "MACHINE" for p in reviewed["body_paragraphs"])
        _assert_isolated(session_factory, seeded, prep_before)

    @pytest.mark.asyncio
    async def test_sequential_repeat_reuses_link_review_revision_and_capability(
        self, session_factory, requested, sender
    ):
        before = len(sender.calls)
        again = await _request(session_factory, requested["token"], sender)

        assert again.code == "REVIEW_SHOWN"
        assert len(sender.calls) == before + 1  # re-display only
        assert [link.approval_capability for link in _links(session_factory)] == [
            requested["capability"]
        ]
        assert len(_reviews(session_factory)) == 1 and len(_revisions(session_factory)) == 1

    @pytest.mark.asyncio
    async def test_unlinked_api_review_for_the_same_pair_is_never_adopted(
        self, session_factory, seeded, sender
    ):
        token = await _prepare(session_factory, seeded, sender)
        prep = _prep(session_factory)
        db = session_factory()
        try:
            job = db.get(JobRecord, seeded["job_id"])
            api_review = ReviewPackageService().create(
                db, job, prep.cv_draft_id, prep.bewerbung_draft_id
            )
        finally:
            db.close()

        await _request(session_factory, token, sender)

        (link,) = _links(session_factory)
        assert link.review_id != api_review.id
        assert len(_reviews(session_factory)) == 2

    @pytest.mark.asyncio
    async def test_new_generation_gets_a_new_link_old_token_expires(
        self, session_factory, requested, sender
    ):
        new_token = await _regenerate(session_factory, requested)

        old = await _request(session_factory, requested["token"], sender)
        new = await _request(session_factory, new_token, sender)

        assert old.code == "PACKAGE_EXPIRED"
        assert new.code == "REVIEW_SHOWN"
        first, second = _links(session_factory)
        assert (first.generation, second.generation) == (1, 2)
        assert first.review_id != second.review_id
        assert first.approval_capability == requested["capability"]  # never rebound

    @pytest.mark.asyncio
    async def test_failure_before_link_insert_rolls_back_review_and_revision(
        self, session_factory, seeded, sender, monkeypatch
    ):
        token = await _prepare(session_factory, seeded, sender)

        def boom(*args, **kwargs):
            raise RuntimeError("crash before link")

        monkeypatch.setattr(ta, "insert_link", boom)
        with pytest.raises(RuntimeError):
            await _request(session_factory, token, sender)

        assert _reviews(session_factory) == [] and _revisions(session_factory) == []
        assert _links(session_factory) == []

    @pytest.mark.asyncio
    async def test_failure_at_commit_rolls_back_review_revision_and_link(
        self, session_factory, seeded, sender
    ):
        token = await _prepare(session_factory, seeded, sender)

        def failing_factory():
            db = session_factory()

            def commit():
                raise RuntimeError("crash at commit")

            db.commit = commit
            return db

        with pytest.raises(RuntimeError):
            await _request(failing_factory, token, sender)

        assert _reviews(session_factory) == [] and _revisions(session_factory) == []
        assert _links(session_factory) == []

    @pytest.mark.asyncio
    async def test_lost_link_race_rolls_back_the_losers_review_and_reuses_the_winner(
        self, session_factory, requested, sender, monkeypatch
    ):
        """Force a second provisional creation past the link lookup: its link
        INSERT hits UNIQUE(preparation_id, generation), the whole
        transaction (review + revision + link) rolls back, and the complete
        retry reuses the winner."""
        real_lookup = ta.get_link_for_generation
        misses = {"left": 1}

        def lookup(db, preparation_id, generation):
            if misses["left"]:
                misses["left"] -= 1
                return None
            return real_lookup(db, preparation_id, generation)

        monkeypatch.setattr(ta, "get_link_for_generation", lookup)

        outcome = await _request(session_factory, requested["token"], sender)

        assert outcome.code == "REVIEW_SHOWN"
        assert len(_reviews(session_factory)) == 1 and len(_revisions(session_factory)) == 1
        assert [link.approval_capability for link in _links(session_factory)] == [
            requested["capability"]
        ]

    @pytest.mark.asyncio
    async def test_capability_collision_rolls_back_and_retries_with_a_new_token(
        self, session_factory, seeded, sender, monkeypatch
    ):
        token = await _prepare(session_factory, seeded, sender)
        taken = "TakenTakenTaken_"
        db = session_factory()
        try:
            insert_link(
                db,
                preparation_id=999,
                generation=1,
                package_token="X" * 16,
                input_identity="0" * 64,
                review_id=999,
                approval_capability=taken,
            )
            db.commit()
        finally:
            db.close()
        real_new = ta.new_approval_capability
        tokens = iter([taken])
        monkeypatch.setattr(ta, "new_approval_capability", lambda: next(tokens, None) or real_new())

        outcome = await _request(session_factory, token, sender)

        assert outcome.code == "REVIEW_SHOWN"
        assert len(_reviews(session_factory)) == 1 and len(_revisions(session_factory)) == 1
        links = _links(session_factory)
        assert len(links) == 2 and links[1].approval_capability != taken

    @pytest.mark.asyncio
    async def test_unexpected_integrity_error_is_reraised_not_taken_as_a_winner(
        self, session_factory, seeded, sender, monkeypatch
    ):
        token = await _prepare(session_factory, seeded, sender)

        def unexpected(*args, **kwargs):
            raise IntegrityError("stmt", {}, Exception("CHECK constraint failed: something"))

        monkeypatch.setattr(ta, "insert_link", unexpected)
        with pytest.raises(IntegrityError):
            await _request(session_factory, token, sender)
        assert _reviews(session_factory) == [] and _links(session_factory) == []

    @pytest.mark.asyncio
    async def test_regeneration_between_resolution_and_lock_creates_nothing(
        self, session_factory, seeded, sender, monkeypatch
    ):
        token = await _prepare(session_factory, seeded, sender)
        real_lock = ta.lock_profile_fresh
        fired = {}

        def lock(db):
            if not fired:
                fired["x"] = True
                _run_in_thread(_regenerate(session_factory, seeded))
            return real_lock(db)

        monkeypatch.setattr(ta, "lock_profile_fresh", lock)

        outcome = await _request(session_factory, token, sender)

        assert outcome.code == "PACKAGE_EXPIRED"
        assert _reviews(session_factory) == [] and _links(session_factory) == []


# --- display ------------------------------------------------------------------


class TestDisplay:
    @pytest.mark.asyncio
    async def test_display_is_the_exact_bound_revision_not_newer_artifacts(
        self, session_factory, requested, sender
    ):
        db = session_factory()
        try:
            job = db.get(JobRecord, requested["job_id"])
            prep = db.scalar(select(Prep))
            # Newer unrelated artifacts: another letter, and an unlinked API
            # review whose revision 2 carries different content.
            await BewerbungService(DeterministicBewerbungProvider()).generate(
                db, job, prep.cv_draft_id
            )
            other = ReviewPackageService().create(
                db, job, prep.cv_draft_id, prep.bewerbung_draft_id
            )
            ReviewPackageService().patch(
                db, other.id, 1, None, BewerbungContentPatch(subject="NEWER-SUBJECT"), None
            )
        finally:
            db.close()

        outcome = await _page(session_factory, requested["capability"], 1, sender)

        assert outcome.code == "REVIEW_SHOWN"
        text = sender.calls[-1]["text"]
        letter = _letter_json(session_factory, _prep(session_factory).bewerbung_draft_id)
        assert letter["opening"] in text and letter["subject"] in text
        assert "NEWER-SUBJECT" not in text

    @pytest.mark.asyncio
    async def test_single_page_offers_both_decisions(self, session_factory, requested, sender):
        markup = sender.calls[-1]["reply_markup"]
        datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        cap = requested["capability"]
        assert f"bz:{cap}:f" in datas and f"bz:{cap}:x" in datas

    @pytest.mark.asyncio
    async def test_stale_package_offers_only_rejection_with_label(
        self, session_factory, requested, sender
    ):
        _bump_profile(session_factory)

        outcome = await _page(session_factory, requested["capability"], 1, sender)

        assert outcome.code == "REVIEW_SHOWN"
        call = sender.calls[-1]
        assert ta.STALE_PACKAGE_LABEL in call["text"]
        datas = [b["callback_data"] for row in call["reply_markup"]["inline_keyboard"] for b in row]
        assert datas == [f"bz:{requested['capability']}:x"]

    @pytest.mark.asyncio
    async def test_too_large_shows_no_content_and_no_decision(
        self, session_factory, seeded, sender, monkeypatch
    ):
        monkeypatch.setattr(ta, "MAX_REVIEW_PAGES", 0)
        token = await _prepare(session_factory, seeded, sender)
        calls = len(sender.calls)

        outcome = await _request(session_factory, token, sender)
        capability = _links(session_factory)[0].approval_capability
        approve = await _decide(session_factory, capability, "f")
        reject = await _decide(session_factory, capability, "x")

        assert outcome.code == approve.code == reject.code == "REVIEW_TOO_LARGE"
        assert "API" in outcome.notice and "nichts freigegeben" in outcome.notice
        assert len(sender.calls) == calls  # nothing displayed
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    async def test_incomplete_document_offers_no_decision(
        self, session_factory, requested, sender, monkeypatch
    ):
        monkeypatch.setattr(
            ta,
            "render_review_document",
            lambda snapshot: ta.ReviewDocument(("partial",), too_large=False, complete=False),
        )
        page = await _page(session_factory, requested["capability"], 1, sender)
        approve = await _decide(session_factory, requested["capability"], "f")
        assert page.code == approve.code == "REVIEW_INCOMPLETE"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    async def test_invalid_page(self, session_factory, requested, sender):
        outcome = await _page(session_factory, requested["capability"], 2, sender)
        assert outcome.code == "INVALID_PAGE"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "code"),
        [
            (TelegramSendOutcome.FAILED, "DISPLAY_FAILED"),
            (TelegramSendOutcome.UNCERTAIN, "DISPLAY_UNCERTAIN"),
            (RuntimeError("boom"), "DISPLAY_UNCERTAIN"),
        ],
    )
    async def test_display_failure_keeps_business_state_and_retry_reuses_it(
        self, session_factory, seeded, sender, result, code
    ):
        token = await _prepare(session_factory, seeded, sender)
        sender.outcomes = [result]

        failed = await _request(session_factory, token, sender)
        capability = _links(session_factory)[0].approval_capability
        retried = await _request(session_factory, token, sender)

        assert failed.code == code and retried.code == "REVIEW_SHOWN"
        assert [link.approval_capability for link in _links(session_factory)] == [capability]
        assert len(_reviews(session_factory)) == 1

    @pytest.mark.asyncio
    async def test_no_transaction_is_open_while_telegram_is_called(
        self, session_factory, seeded, sender
    ):
        token = await _prepare(session_factory, seeded, sender)
        sessions = []

        def tracking_factory():
            db = session_factory()
            sessions.append(db)
            return db

        async def hook():
            assert sessions and all(not db.in_transaction() for db in sessions)

        sender.hook = hook
        outcome = await _request(tracking_factory, token, sender)
        assert outcome.code == "REVIEW_SHOWN"


# --- revision binding ----------------------------------------------------------


class TestRevisionBinding:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("patch", [None, BewerbungContentPatch(subject="API EDIT")])
    async def test_api_patch_invalidates_the_telegram_capability(
        self, session_factory, requested, sender, patch
    ):
        review_id = requested["link"].review_id
        _api(session_factory, "patch", review_id, 1, None, patch, None)  # incl. no-op PATCH

        page = await _page(session_factory, requested["capability"], 1, sender)
        approve = await _decide(session_factory, requested["capability"], "f")
        reject = await _decide(session_factory, requested["capability"], "x")
        again = await _request(session_factory, requested["token"], sender)

        assert page.code == approve.code == reject.code == again.code == "REVIEW_CHANGED"
        row = _review_row(session_factory, review_id)
        assert (row.status, row.review_version) == ("PENDING_REVIEW", 2)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["approve", "reject"])
    async def test_patch_between_validation_and_cas_never_decides_revision_2(
        self, session_factory, requested, monkeypatch, method
    ):
        """Interleaving: the PATCH commits after all Telegram checks passed,
        right before the Stage 6E call; the expected-version CAS refuses."""
        review_id = requested["link"].review_id
        real = getattr(ReviewPackageService, method)

        def racing(self, db, *args, **kwargs):
            _api(session_factory, "patch", review_id, 1, None, None, None)
            return real(self, db, *args, **kwargs)

        monkeypatch.setattr(ReviewPackageService, method, racing)

        action = "f" if method == "approve" else "x"
        outcome = await _decide(session_factory, requested["capability"], action)

        assert outcome.code == "REVIEW_CHANGED"
        row = _review_row(session_factory, review_id)
        assert (row.status, row.review_version) == ("PENDING_REVIEW", 2)

    @pytest.mark.asyncio
    async def test_api_approval_of_revision_2_is_approved_elsewhere(
        self, session_factory, requested, sender
    ):
        review_id = requested["link"].review_id
        _api(
            session_factory, "patch", review_id, 1, None, BewerbungContentPatch(subject="v2"), None
        )
        _api(session_factory, "approve", review_id, 2, True, None)

        decision = await _decide(session_factory, requested["capability"], "f")
        page = await _page(session_factory, requested["capability"], 1, sender)
        again = await _request(session_factory, requested["token"], sender)

        assert decision.code == page.code == again.code == "APPROVED_ELSEWHERE"
        assert "NICHT freigegeben" in decision.notice
        db = session_factory()
        try:
            assert ta.get_approved_handoff(db, requested["link"].id) is None
        finally:
            db.close()


# --- approval -------------------------------------------------------------------


class TestApproval:
    @pytest.mark.asyncio
    async def test_approval_pins_the_exact_bound_revision_and_sends_nothing(
        self, session_factory, requested, sender
    ):
        prep_before = _prep_snapshot(session_factory)
        calls = len(sender.calls)

        outcome = await _decide(session_factory, requested["capability"], "f")

        assert outcome.code == "APPROVED"
        assert "FREIGEGEBEN — NOCH NICHT GESENDET" in outcome.notice
        (review,) = _reviews(session_factory)
        (revision,) = _revisions(session_factory)
        assert review.status == "APPROVED" and review.approved_revision_id == revision.id
        assert revision.revision_number == requested["link"].bound_review_version == 1
        assert len(sender.calls) == calls  # APPROVED != SENT: no message, no send
        _assert_isolated(session_factory, requested, prep_before)

    @pytest.mark.asyncio
    async def test_double_click_is_one_transition(self, session_factory, requested):
        first = await _decide(session_factory, requested["capability"], "f")
        decided_at = _reviews(session_factory)[0].decided_at
        second = await _decide(session_factory, requested["capability"], "f")

        assert (first.code, second.code) == ("APPROVED", "ALREADY_APPROVED")
        assert _reviews(session_factory)[0].decided_at == decided_at

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change",
        [
            {"description": "Python, SQL, Kubernetes und Docker."},  # fingerprint
            {"title": "junior python developer"},  # exact title case only
            {"company": "Example Holding GmbH"},  # company only
        ],
    )
    async def test_job_changes_block_approval(self, session_factory, requested, change):
        _change_job(session_factory, requested, **change)
        outcome = await _decide(session_factory, requested["capability"], "f")
        assert outcome.code == "PACKAGE_STALE"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    async def test_profile_change_blocks_approval(self, session_factory, requested):
        _bump_profile(session_factory)
        outcome = await _decide(session_factory, requested["capability"], "f")
        assert outcome.code == "PACKAGE_STALE"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "value"),
        [
            ("app.services.telegram_bewerbung.ALGORITHM_VERSION", "matcher-vNEXT"),
            ("app.services.telegram_bewerbung.CV_ADAPTER_VERSION", "adapter-vNEXT"),
            ("app.services.telegram_bewerbung.PREVIEW_RENDERER_VERSION", "renderer-vNEXT"),
            ("app.services.bewerbung_reuse.BEWERBUNG_GENERATOR_VERSION", "gen-vNEXT"),
            ("app.services.bewerbung_reuse.DETERMINISTIC_BEWERBUNG_PROVIDER", "other"),
        ],
    )
    async def test_runtime_version_changes_block_approval(
        self, session_factory, requested, monkeypatch, target, value
    ):
        monkeypatch.setattr(target, value)
        outcome = await _decide(session_factory, requested["capability"], "f")
        assert outcome.code == "PACKAGE_STALE"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    async def test_ineligible_job_blocks_approval(self, session_factory, requested):
        _change_job(session_factory, requested, status="APPLIED")
        outcome = await _decide(session_factory, requested["capability"], "f")
        assert outcome.code == "JOB_NOT_ELIGIBLE"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    async def test_dangling_letter_blocks_approval(self, session_factory, requested):
        _update_db(session_factory, BewerbungDraftRecord.__table__.delete())
        outcome = await _decide(session_factory, requested["capability"], "f")
        assert outcome.code == "PACKAGE_UNAVAILABLE"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    async def test_replaced_generation_cannot_be_approved_but_can_be_rejected(
        self, session_factory, requested
    ):
        await _regenerate(session_factory, requested)

        approve = await _decide(session_factory, requested["capability"], "f")
        assert approve.code == "PACKAGE_REPLACED"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"  # stale stays PENDING

        reject = await _decide(session_factory, requested["capability"], "x")
        assert reject.code == "REJECTED"

    @pytest.mark.asyncio
    async def test_missing_current_preparation_fails_closed_but_link_is_retained(
        self, session_factory, requested
    ):
        _update_db(
            session_factory,
            TelegramVacancyReviewRecord.__table__.delete(),  # cascades to the 9B ledger
        )
        assert _prep(session_factory) is None

        outcome = await _decide(session_factory, requested["capability"], "f")

        assert outcome.code == "PACKAGE_REPLACED"
        assert [link.id for link in _links(session_factory)] == [requested["link"].id]

    @pytest.mark.asyncio
    async def test_stale_identity_map_values_are_refreshed_under_the_lock(
        self, session_factory, requested
    ):
        from app.db.candidate_profile_repository import get_candidate_profile

        held = session_factory()
        get_candidate_profile(held)  # stale profile in the identity map
        held.get(JobRecord, requested["job_id"])
        held.rollback()
        _bump_profile(session_factory)  # committed elsewhere

        outcome = await ta.handle_decision(lambda: held, _settings(), requested["capability"], "f")

        assert outcome.code == "PACKAGE_STALE"
        assert _reviews(session_factory)[0].status == "PENDING_REVIEW"

    @pytest.mark.asyncio
    async def test_approve_vs_approve_interleaving_is_one_transition(
        self, session_factory, requested, monkeypatch
    ):
        review_id = requested["link"].review_id
        real = ReviewPackageService.approve

        def racing(self, db, *args, **kwargs):
            _api(session_factory, "approve", review_id, 1, False, "other worker")
            return real(self, db, *args, **kwargs)

        monkeypatch.setattr(ReviewPackageService, "approve", racing)
        outcome = await _decide(session_factory, requested["capability"], "f")

        assert outcome.code == "ALREADY_APPROVED"
        row = _review_row(session_factory, review_id)
        assert row.status == "APPROVED" and row.decision_note == "other worker"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("ours", "theirs"), [("f", "reject"), ("x", "approve")])
    async def test_approve_reject_interleavings_have_one_winner(
        self, session_factory, requested, monkeypatch, ours, theirs
    ):
        review_id = requested["link"].review_id
        method = "approve" if ours == "f" else "reject"
        real = getattr(ReviewPackageService, method)

        def racing(self, db, *args, **kwargs):
            if theirs == "approve":
                _api(session_factory, "approve", review_id, 1, False, None)
            else:
                _api(session_factory, "reject", review_id, 1, None)
            return real(self, db, *args, **kwargs)

        monkeypatch.setattr(ReviewPackageService, method, racing)
        outcome = await _decide(session_factory, requested["capability"], ours)

        expected = "ALREADY_REJECTED" if theirs == "reject" else "ALREADY_APPROVED"
        assert outcome.code == expected
        status = _review_row(session_factory, review_id).status
        assert status == ("REJECTED" if theirs == "reject" else "APPROVED")


# --- rejection & policies ---------------------------------------------------------


class TestRejectionAndPolicy:
    @pytest.mark.asyncio
    async def test_rejection_and_terminal_replies(self, session_factory, requested, sender):
        prep_before = _prep_snapshot(session_factory)
        reject = await _decide(session_factory, requested["capability"], "x")
        again = await _decide(session_factory, requested["capability"], "x")
        approve = await _decide(session_factory, requested["capability"], "f")

        assert reject.code == "REJECTED" and "ABGELEHNT — NICHT GESENDET" in reject.notice
        assert again.code == approve.code == "ALREADY_REJECTED"
        assert _reviews(session_factory)[0].status == "REJECTED"
        _assert_isolated(session_factory, requested, prep_before)

    @pytest.mark.asyncio
    async def test_reject_after_approve_reports_approval(self, session_factory, requested):
        await _decide(session_factory, requested["capability"], "f")
        outcome = await _decide(session_factory, requested["capability"], "x")
        assert outcome.code == "ALREADY_APPROVED"
        assert _reviews(session_factory)[0].status == "APPROVED"

    @pytest.mark.asyncio
    async def test_stale_package_can_still_be_rejected(self, session_factory, requested):
        _bump_profile(session_factory)
        _change_job(session_factory, requested, company="Changed")
        outcome = await _decide(session_factory, requested["capability"], "x")
        assert outcome.code == "REJECTED"

    @pytest.mark.asyncio
    async def test_rejected_generation_cannot_be_reviewed_again(
        self, session_factory, requested, sender
    ):
        await _decide(session_factory, requested["capability"], "x")

        again = await _request(session_factory, requested["token"], sender)

        assert again.code == "ALREADY_REJECTED"
        assert len(_reviews(session_factory)) == 1 and len(_links(session_factory)) == 1

    @pytest.mark.asyncio
    async def test_new_generation_after_rejection_gets_a_new_review(
        self, session_factory, requested, sender
    ):
        await _decide(session_factory, requested["capability"], "x")
        new_token = await _regenerate(session_factory, requested)

        outcome = await _request(session_factory, new_token, sender)

        assert outcome.code == "REVIEW_SHOWN"
        assert [r.status for r in _reviews(session_factory)] == ["REJECTED", "PENDING_REVIEW"]

    @pytest.mark.asyncio
    async def test_historical_approval_never_authorizes_the_replacement(
        self, session_factory, requested, sender
    ):
        await _decide(session_factory, requested["capability"], "f")
        new_token = await _regenerate(session_factory, requested)

        old_report = await _decide(session_factory, requested["capability"], "f")
        await _request(session_factory, new_token, sender)
        new_review = _reviews(session_factory)[1]

        assert old_report.code == "ALREADY_APPROVED"
        assert "gilt nicht für das neue Paket" in old_report.notice
        assert new_review.status == "PENDING_REVIEW"


# --- handoff & static isolation -------------------------------------------------


class TestHandoff:
    @pytest.mark.asyncio
    async def test_exact_handoff_per_link_despite_multiple_historical_approvals(
        self, session_factory, requested, sender
    ):
        await _decide(session_factory, requested["capability"], "f")
        new_token = await _regenerate(session_factory, requested)
        await _request(session_factory, new_token, sender)
        second_link = _links(session_factory)[1]
        await _decide(session_factory, second_link.approval_capability, "f")

        db = session_factory()
        try:
            first = ta.get_approved_handoff(db, requested["link"].id)
            second = ta.get_approved_handoff(db, second_link.id)
        finally:
            db.close()

        reviews = _reviews(session_factory)
        revisions = _revisions(session_factory)
        assert (first.review_id, first.generation) == (reviews[0].id, 1)
        assert (second.review_id, second.generation) == (reviews[1].id, 2)
        assert first.approved_revision_id == revisions[0].id
        assert second.approved_revision_id == revisions[1].id
        assert first.bewerbung_draft_id != second.bewerbung_draft_id
        assert first.bound_review_version == 1

    @pytest.mark.asyncio
    async def test_pending_or_rejected_has_no_handoff(self, session_factory, requested):
        db = session_factory()
        try:
            assert ta.get_approved_handoff(db, requested["link"].id) is None
        finally:
            db.close()
        await _decide(session_factory, requested["capability"], "x")
        db = session_factory()
        try:
            assert ta.get_approved_handoff(db, requested["link"].id) is None
            assert ta.get_approved_handoff(db, 424242) is None
        finally:
            db.close()


_FORBIDDEN_IMPORT_PREFIXES = (
    "smtplib",
    "imaplib",
    "httpx",
    "app.providers.email",
    "app.services.gmail",
    "app.services.automation",
    "app.services.follow_up",
    "app.services.response_draft",
    "app.services.company_research",
    "app.services.scheduler",
    "app.domain.status_transitions",
)


def test_stage_9c_modules_import_no_outbound_or_status_code():
    for relative in (
        "app/services/telegram_bewerbung_approval.py",
        "app/db/telegram_bewerbung_approval_repository.py",
    ):
        tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
                names.extend(f"{node.module}.{alias.name}" for alias in node.names)
        for name in names:
            assert not name.startswith(_FORBIDDEN_IMPORT_PREFIXES), (relative, name)
            assert "update_job_status" not in name and "record_decision" not in name


def test_9b_modules_still_import_no_stage_6e_or_9c_code():
    for relative in (
        "app/services/telegram_bewerbung.py",
        "app/services/telegram_bewerbung_preview.py",
    ):
        tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
        modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        for module in modules:
            assert "review_package" not in module and "approval" not in module, module


def test_lock_profile_fresh_refreshes_a_stale_identity_map_profile(session_factory, seeded):
    """`get_candidate_profile(for_update=True)` alone returns the stale
    identity-map object; the lock helper must refresh it under the lock."""
    from app.db.candidate_profile_repository import get_candidate_profile
    from app.db.telegram_bewerbung_approval_repository import lock_profile_fresh

    db = session_factory()
    try:
        stale = get_candidate_profile(db)
        version = stale.profile_version
        _bump_profile(session_factory)  # committed by another session
        assert get_candidate_profile(db, for_update=True).profile_version == version

        locked = lock_profile_fresh(db)

        assert locked is stale and locked.profile_version == version + 1
    finally:
        db.close()
