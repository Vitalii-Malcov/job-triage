"""Stage 9D: Gmail draft handoff of the EXACT Stage 9C-approved package.

Exercises the REAL Stage 9B/9C/6E services on file-backed SQLite (the 9C
test infrastructure, including its network guard) with a FAKE draft
provider that records every call -- no IMAP, no Gmail, nothing sent.

Concurrency note: SQLite serializes writers; the "interleaving" tests here
inject a competing write at a deterministic point. They are NOT
concurrency evidence -- see
tests/integration/test_gmail_application_draft_postgres.py."""

import ast
import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

import app.services.gmail_application_draft as gd
from app.db import gmail_application_draft_repository as repo
from app.db.models import (
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
    GmailApplicationDraftRecord,
    JobRecord,
    TelegramBewerbungApprovalRecord,
    TelegramVacancyReviewRecord,
)
from app.db.models import TelegramBewerbungPreparationRecord as Prep
from app.models.review_package import (
    BewerbungContentPatch,
    ReviewedBewerbungContent,
    ReviewedBewerbungParagraph,
    ReviewTextField,
)
from app.providers.email import draft_base
from app.providers.email.draft_base import (
    DraftBudgetExhaustedError,
    DraftCreateOutcomeUnknownError,
    DraftCreateRejectedError,
    DraftCreateResult,
    DraftLookupError,
    DraftLookupResult,
    DraftsMailboxInvalidError,
    build_draft_mime,
)
from tests import test_telegram_bewerbung_approval as s9c
from tests.test_telegram_bewerbung_approval import (
    _api,
    _bump_profile,
    _change_job,
    _decide,
    _regenerate,
    _run_in_thread,
)
from tests.test_telegram_bewerbung_approval import _settings as _settings_9c

# The Stage 9C fixtures (real 9B/9C/6E flow on SQLite, network guard).
network_guard = s9c.network_guard
requested = s9c.requested
seeded = s9c.seeded
sender = s9c.sender
session_factory = s9c.session_factory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "me@example.com"
PASSWORD = "app-pw-secret-value"
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _settings(**overrides):
    data = dict(
        telegram_gmail_draft_enabled=True,
        gmail_username=ACCOUNT,
        gmail_app_password=PASSWORD,
        gmail_drafts_mailbox="[Gmail]/Drafts",
        gmail_draft_attempt_budget_seconds=60,
        gmail_draft_reconcile_min_age_seconds=120,
    )
    data.update(overrides)
    return _settings_9c(**data)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(gd, "_now", value)
    return value


class FakeProvider:
    """Records every provider call. `create_draft` builds the real MIME so
    every created draft is checked against the full MIME contract."""

    def __init__(self):
        self.creates: list[tuple] = []
        self.lookups: list[tuple] = []
        self.mime: list[bytes] = []
        self.create_result = DraftCreateResult(7, 42)
        self.create_exc: BaseException | None = None
        self.lookup_result = DraftLookupResult(7, (42,))
        self.lookup_exc: BaseException | None = None
        self.on_create = None

    def create_draft(self, message, target, deadline_at):
        self.creates.append((message, target, deadline_at))
        self.mime.append(build_draft_mime(message, date=T0))
        if self.on_create is not None:
            self.on_create()
        if self.create_exc is not None:
            raise self.create_exc
        return self.create_result

    def find_by_message_id(self, reconcile_target, deadline_at):
        self.lookups.append((reconcile_target, deadline_at))
        if self.lookup_exc is not None:
            raise self.lookup_exc
        return self.lookup_result


@pytest.fixture()
def provider():
    return FakeProvider()


@pytest_asyncio.fixture()
async def approved(session_factory, requested):
    outcome = await _decide(session_factory, requested["capability"], "f")
    assert outcome.code == "APPROVED", outcome
    return requested


async def _create(session_factory, capability, provider, **settings):
    return await gd.handle_create(
        session_factory, _settings(**settings), capability, provider=provider
    )


async def _recheck(session_factory, capability, provider, **settings):
    return await gd.handle_reconcile(
        session_factory, _settings(**settings), capability, provider=provider
    )


def _ledgers(session_factory) -> list[GmailApplicationDraftRecord]:
    db = session_factory()
    try:
        return list(db.scalars(select(GmailApplicationDraftRecord)))
    finally:
        db.close()


def _ledger(session_factory) -> GmailApplicationDraftRecord:
    (row,) = _ledgers(session_factory)
    return row


def _update(session_factory, stmt):
    db = session_factory()
    try:
        db.execute(stmt)
        db.commit()
    finally:
        db.close()


def _set_revision_letter(session_factory, approved, **fields):
    """Simulate a LEGALLY approved Stage 6E revision whose nullable letter
    fields hold the given values (6E does not guarantee non-blank ones)."""
    db = session_factory()
    try:
        review = db.get(ApplicationPackageReviewRecord, approved["link"].review_id)
        revision = db.get(ApplicationPackageReviewRevisionRecord, review.approved_revision_id)
        letter = json.loads(revision.reviewed_bewerbung_json)
        for name, value in fields.items():
            if name == "body_paragraphs":
                letter[name] = [{"text": text} for text in value]
            else:
                letter[name] = {"value": value, "origin": "MACHINE"}
        revision.reviewed_bewerbung_json = json.dumps(letter)
        db.commit()
    finally:
        db.close()


_ISOLATED_MODELS = (
    JobRecord,
    TelegramVacancyReviewRecord,
    Prep,
    TelegramBewerbungApprovalRecord,
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
)


def _state(session_factory) -> dict:
    """Every Stage 9A/9B/9C/6E row and JobRecord, column by column."""
    db = session_factory()
    try:
        return {
            model.__tablename__: [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in db.scalars(select(model).order_by(model.id))
            ]
            for model in _ISOLATED_MODELS
        }
    finally:
        db.close()


def _parsed(mime: bytes):
    return BytesParser(policy=policy.SMTP).parsebytes(mime)


# --- pure: content rendering ------------------------------------------------------


def _letter(
    subject="Bewerbung als Junior Python Developer",
    salutation="Sehr geehrte Damen und Herren,",
    opening="ich bewerbe mich.",
    paragraphs=("Absatz eins.", "Absatz zwei."),
    closing="Mit freundlichen Grüßen",
    signature="Anna Muster",
) -> ReviewedBewerbungContent:
    return ReviewedBewerbungContent(
        subject=ReviewTextField(value=subject),
        salutation=ReviewTextField(value=salutation),
        opening=ReviewTextField(value=opening),
        body_paragraphs=[ReviewedBewerbungParagraph(text=text) for text in paragraphs],
        closing=ReviewTextField(value=closing),
        signature_name=ReviewTextField(value=signature),
    )


class TestRenderer:
    def test_full_letter_exact_bytes(self):
        rendered = gd.render_draft_content(_letter())
        assert rendered.subject == "Bewerbung als Junior Python Developer"
        assert rendered.body_lf == (
            "Sehr geehrte Damen und Herren,\n\nich bewerbe mich.\n\nAbsatz eins.\n\n"
            "Absatz zwei.\n\nMit freundlichen Grüßen\nAnna Muster\n"
        )

    @pytest.mark.parametrize(
        ("overrides", "body"),
        [
            ({"salutation": None}, "ich bewerbe mich.\n\nP.\n\nGruß\nAnna\n"),
            ({"opening": ""}, "Hallo,\n\nP.\n\nGruß\nAnna\n"),
            ({"closing": "  \n\t "}, "Hallo,\n\nich bewerbe mich.\n\nP.\n\nAnna\n"),
            ({"paragraphs": ()}, "Hallo,\n\nich bewerbe mich.\n\nGruß\nAnna\n"),
            ({"closing": None}, "Hallo,\n\nich bewerbe mich.\n\nP.\n\nAnna\n"),
            ({"signature": None}, "Hallo,\n\nich bewerbe mich.\n\nP.\n\nGruß\n"),
            ({"signature": "   "}, "Hallo,\n\nich bewerbe mich.\n\nP.\n\nGruß\n"),
            (
                {"opening": None, "closing": None, "salutation": None, "signature": None},
                "P.\n",
            ),
        ],
    )
    def test_nullable_components_are_omitted_without_placeholders(self, overrides, body):
        values = dict(salutation="Hallo,", paragraphs=("P.",), closing="Gruß", signature="Anna")
        values.update(overrides)
        assert gd.render_draft_content(_letter(**values)).body_lf == body

    def test_blank_paragraphs_omitted_order_preserved(self):
        rendered = gd.render_draft_content(
            _letter(
                salutation=None,
                opening=None,
                closing=None,
                signature=None,
                paragraphs=("Erstens.", "", "   ", "\r\n", "Zweitens.", "Drittens."),
            )
        )
        assert rendered.body_lf == "Erstens.\n\nZweitens.\n\nDrittens.\n"

    def test_line_endings_strip_and_unicode_preserved(self):
        decomposed = "Café   mit  zwei   Leerzeichen"
        rendered = gd.render_draft_content(
            _letter(
                salutation=None,
                closing=None,
                signature=None,
                paragraphs=(),
                opening="  \r\n Zeile 1\r\nZeile 2\rZeile 3\n\n" + decomposed + " \n ",
            )
        )
        assert rendered.body_lf == f"Zeile 1\nZeile 2\nZeile 3\n\n{decomposed}\n"
        assert rendered.body_lf.endswith("\n") and not rendered.body_lf.endswith("\n\n")
        assert "́" in rendered.body_lf  # no NFC normalization

    def test_long_content_is_never_truncated(self):
        long = "x" * 20000
        rendered = gd.render_draft_content(_letter(paragraphs=(long,), subject="S" * 900))
        assert long in rendered.body_lf and rendered.subject == "S" * 900

    @pytest.mark.parametrize(
        "overrides",
        [
            {"opening": None, "paragraphs": (), "closing": None},  # salutation + signature
            {"opening": None, "paragraphs": (" ",), "closing": "  "},
            {
                "salutation": None,
                "signature": None,
                "opening": None,
                "paragraphs": (),
                "closing": None,
            },
            {"subject": None},
            {"subject": ""},
            {"subject": "   \t "},
        ],
    )
    def test_incomplete_for_stage_9d(self, overrides):
        assert gd.render_draft_content(_letter(**overrides)) == "CONTENT_INCOMPLETE"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"subject": "Betreff\r\nBcc: x@example.com"},
            {"subject": "Betreff\nzweite"},
            {"subject": "Betreff\r"},
            {"subject": "Bet\x00reff"},
            {"opening": "a\x00b"},
            {"paragraphs": ("ok", "x\x00")},
            {"signature": "\x00"},
            {"opening": "lone \ud800 surrogate"},
        ],
    )
    def test_invalid_content(self, overrides):
        assert gd.render_draft_content(_letter(**overrides)) == "CONTENT_INVALID"

    def test_subject_is_normalized_after_the_raw_check(self):
        rendered = gd.render_draft_content(_letter(subject="  Bewerbung \t als   Dev  "))
        assert rendered.subject == "Bewerbung als Dev"


class TestHashMarkerFrom:
    def test_exact_canonical_vector(self):
        payload = '["9d-v1","me@example.com",null,null,null,"Betreff – ä","Text\\n"]'
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert gd.content_hash("9d-v1", ACCOUNT, "Betreff – ä", "Text\n") == expected

    def test_from_included_transport_metadata_excluded(self):
        base = gd.content_hash("9d-v1", ACCOUNT, "S", "B\n")
        assert gd.content_hash("9d-v1", "other@example.com", "S", "B\n") != base
        assert gd.content_hash("9d-v1", ACCOUNT, "S", "B\n") == base  # stable
        assert "date" not in gd.content_hash.__code__.co_varnames
        assert "message_id" not in gd.content_hash.__code__.co_varnames

    def test_marker_shape_and_entropy(self):
        markers = {gd.new_marker() for _ in range(200)}
        assert len(markers) == 200
        for marker in markers:
            assert draft_base.MESSAGE_ID_PATTERN.fullmatch(marker)
            assert len(marker) == 60

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "me@example.com\r\nBcc: x@example.com",
            "me@example.com\n",
            "me@exa\x00mple.com",
            "Anna <me@example.com>",
            "me@example.com, you@example.com",
            "me@example.com; you@example.com",
            " me@example.com",
            "not-an-address",
            "me@localhost",
            "mé@example.com",
            "a" * 310 + "@example.com",
            "(comment) me@example.com",
        ],
    )
    def test_from_injection_and_ambiguity_rejected(self, raw):
        assert gd.validate_from_address(raw) is None

    def test_from_accepts_one_bare_mailbox(self):
        assert gd.validate_from_address(ACCOUNT) == ACCOUNT


class TestCallbackParsing:
    CAP = "AbCdEfGh_-123456"

    def test_round_trip_and_limit(self):
        for action in ("c", "r"):
            data = gd.build_callback_data(self.CAP, action)
            assert gd.parse_callback_data(data) == (self.CAP, action)
            assert len(data.encode()) < 64

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "",
            "bm",
            "bm:AbCdEfGh_-123456",
            "bm:AbCdEfGh_-123456:x",
            "bm:AbCdEfGh_-123456:c:1",
            "bm:AbCdEfGh_-12345:c",
            "bm:AbCdEfGh_-1234567:c",
            "bm:AbCdEfGh_-12345=:c",
            "bz:AbCdEfGh_-123456:f",
            "BM:AbCdEfGh_-123456:c",
            "bm:42:c",
        ],
    )
    def test_strict(self, data):
        assert gd.parse_callback_data(data) is None


# --- end-to-end create ------------------------------------------------------------------


class TestCreate:
    @pytest.mark.asyncio
    async def test_first_create_is_one_exact_append_and_nothing_else(
        self, session_factory, approved, provider
    ):
        before = _state(session_factory)
        outcome = await _create(session_factory, approved["capability"], provider)

        assert outcome.code == "CREATED" and outcome.action is None
        assert "NOCH NICHT GESENDET" in outcome.notice and "Empfänger" in outcome.notice
        assert len(provider.creates) == 1
        message, target, deadline_at = provider.creates[0]
        row = _ledger(session_factory)
        assert (row.state, row.attempt_count, row.reconciled) == ("CREATED", 1, False)
        assert (row.uid_validity, row.draft_uid) == (7, 42)
        assert row.claim_token is None and row.claim_started_at is None
        assert row.link_id == approved["link"].id and row.account_key == ACCOUNT
        assert row.marker_message_id == message.message_id
        assert (row.drafts_mailbox, row.drafts_mailbox_wire) == (
            "[Gmail]/Drafts",
            '"[Gmail]/Drafts"',
        )
        assert (target.mailbox, target.mailbox_wire, target.account_key) == (
            "[Gmail]/Drafts",
            '"[Gmail]/Drafts"',
            ACCOUNT,
        )
        assert row.renderer_version == "9d-v1" and row.attempt_budget_seconds == 60
        assert deadline_at == T0 + timedelta(seconds=60)
        assert row.content_sha256 == gd.content_hash(
            "9d-v1", ACCOUNT, message.subject, message.body_lf
        )
        review = session_factory().get(ApplicationPackageReviewRecord, approved["link"].review_id)
        assert (row.review_id, row.approved_revision_id) == (review.id, review.approved_revision_id)
        assert _state(session_factory) == before  # no Stage 9A/9B/9C/6E/job write

    @pytest.mark.asyncio
    async def test_mime_has_no_recipient_attachment_or_html(
        self, session_factory, approved, provider
    ):
        await _create(session_factory, approved["capability"], provider)
        parsed = _parsed(provider.mime[0])
        for name in ("To", "Cc", "Bcc", "Reply-To", "In-Reply-To", "References"):
            assert parsed[name] is None
        assert str(parsed["From"]) == ACCOUNT
        assert parsed.get_content_type() == "text/plain" and not parsed.is_multipart()
        assert b"<html" not in provider.mime[0].lower()
        assert not list(parsed.iter_attachments())
        assert not hasattr(provider.creates[0][0], "to")

    @pytest.mark.asyncio
    async def test_content_comes_only_from_the_exact_approved_revision(
        self, session_factory, approved, provider
    ):
        _set_revision_letter(session_factory, approved, opening="REVISION-ONLY-OPENING")
        await _create(session_factory, approved["capability"], provider)
        message = provider.creates[0][0]
        assert "REVISION-ONLY-OPENING" in message.body_lf

    @pytest.mark.asyncio
    async def test_salutation_and_signature_only_is_refused_before_any_claim(
        self, session_factory, approved, provider
    ):
        _set_revision_letter(
            session_factory, approved, opening=None, closing="  ", body_paragraphs=[]
        )
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "CONTENT_INCOMPLETE"
        assert _ledgers(session_factory) == [] and provider.creates == []
        assert _decided_status(session_factory, approved) == "APPROVED"  # 6E untouched

    @pytest.mark.asyncio
    async def test_single_real_paragraph_is_a_valid_draft(
        self, session_factory, approved, provider
    ):
        _set_revision_letter(
            session_factory,
            approved,
            salutation=None,
            opening="",
            closing=None,
            signature_name=None,
            body_paragraphs=["", "Nur dieser Absatz."],
        )
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "CREATED"
        assert provider.creates[0][0].body_lf == "Nur dieser Absatz.\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fields", "code"),
        [
            ({"subject": "a\r\nBcc: x@example.com"}, "CONTENT_INVALID"),
            ({"subject": None}, "CONTENT_INCOMPLETE"),
            ({"subject": "   "}, "CONTENT_INCOMPLETE"),
        ],
    )
    async def test_subject_problems_refused_before_any_claim(
        self, session_factory, approved, provider, fields, code
    ):
        _set_revision_letter(session_factory, approved, **fields)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == code
        assert _ledgers(session_factory) == [] and provider.creates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"gmail_username": "Anna <me@example.com>"}, "FROM_INVALID"),
            ({"gmail_username": "me@example.com, x@example.com"}, "FROM_INVALID"),
            ({"gmail_username": ""}, "FROM_INVALID"),
            ({"gmail_app_password": ""}, "CONFIG_INVALID"),
            ({"gmail_drafts_mailbox": "INBOX"}, "DRAFTS_MAILBOX_INVALID"),
            ({"gmail_drafts_mailbox": "Dra\r\nfts"}, "DRAFTS_MAILBOX_INVALID"),
            ({"gmail_drafts_mailbox": "Dra*"}, "DRAFTS_MAILBOX_INVALID"),
        ],
    )
    async def test_config_refused_before_any_claim_or_network(
        self, session_factory, approved, provider, overrides, code
    ):
        outcome = await _create(session_factory, approved["capability"], provider, **overrides)
        assert outcome.code == code and "Konfiguration" in outcome.notice
        assert _ledgers(session_factory) == [] and provider.creates == []

    @pytest.mark.asyncio
    async def test_wire_over_the_bound_fails_closed_before_any_claim(
        self, session_factory, approved, provider, monkeypatch
    ):
        monkeypatch.setattr(draft_base, "MAX_DRAFTS_MAILBOX_WIRE_LENGTH", 30)
        outcome = await _create(
            session_factory,
            approved["capability"],
            provider,
            gmail_drafts_mailbox="[Gmail]/Entwürfe-Entwürfe-Entwürfe",
        )
        assert outcome.code == "DRAFTS_MAILBOX_UNENCODABLE"
        assert _ledgers(session_factory) == [] and provider.creates == []

    @pytest.mark.asyncio
    async def test_localized_mailbox_is_frozen_with_its_exact_wire_form(
        self, session_factory, approved, provider
    ):
        await _create(
            session_factory,
            approved["capability"],
            provider,
            gmail_drafts_mailbox="[Gmail]/Entwürfe",
        )
        row = _ledger(session_factory)
        assert (row.drafts_mailbox, row.drafts_mailbox_wire) == (
            "[Gmail]/Entwürfe",
            '"[Gmail]/Entw&APw-rfe"',
        )
        assert provider.creates[0][1].mailbox_wire == '"[Gmail]/Entw&APw-rfe"'

    @pytest.mark.asyncio
    async def test_sequential_duplicate_is_one_append_with_historical_wording(
        self, session_factory, approved, provider
    ):
        first = await _create(session_factory, approved["capability"], provider)
        second = await _create(session_factory, approved["capability"], provider)
        assert (first.code, second.code) == ("CREATED", "ALREADY_CREATED")
        assert len(provider.creates) == 1 and len(_ledgers(session_factory)) == 1
        assert "bereits" in second.notice and "Dieser Schritt hat nichts gesendet" in second.notice
        assert second.action is None

    @pytest.mark.asyncio
    async def test_created_replay_after_any_change_stays_historical(
        self, session_factory, approved, provider
    ):
        await _create(session_factory, approved["capability"], provider)
        await _regenerate(session_factory, approved)
        outcome = await _create(
            session_factory, approved["capability"], provider, gmail_username="new@example.com"
        )
        assert outcome.code == "ALREADY_CREATED" and len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_in_flight_duplicate_press_reports_progress_without_a_second_append(
        self, session_factory, approved, provider
    ):
        inner = []

        def hook():
            inner.append(asyncio.run(_create(session_factory, approved["capability"], provider)))

        provider.on_create = hook
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "CREATED" and inner[0].code == "IN_PROGRESS"
        assert len(provider.creates) == 1


# --- S9D-ASTRA-001: the approved subject reaches the MIME literally -------------------


ENCODED_WORD_TITLE = "Junior Python Developer =?utf-8?q?Unapproved_Subject_Text?="


def _approved_revision_subject(session_factory, review_id) -> str:
    db = session_factory()
    try:
        review = db.get(ApplicationPackageReviewRecord, review_id)
        revision = db.get(ApplicationPackageReviewRevisionRecord, review.approved_revision_id)
        return json.loads(revision.reviewed_bewerbung_json)["subject"]["value"]
    finally:
        db.close()


def _transmitted(provider) -> tuple[str, str]:
    """(decoded Subject, LF body) of the MIME actually handed to APPEND."""
    (mime,) = provider.mime
    parsed = _parsed(mime)
    (subject,) = parsed.get_all("Subject")
    return str(subject), parsed.get_content().replace("\r\n", "\n")


class TestSubjectIntegrity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "subject",
        [
            "Bewerbung als Junior Python Developer =?utf-8?q?Unapproved_Subject_Text?=",
            "Bewerbung als =?UTF-8?B?VW5hcHByb3ZlZCBTdWJqZWN0?= Developer",
            "Bewerbung =?utf-8?q?=0D?= Developer",
            "Bewerbung =?utf-8?q?=0A?=Bcc: x@example.com",
            "Bewerbung =?utf-8?q?=00?= Developer",
        ],
        ids=["q-encoded-word", "b-encoded-word", "encoded-cr", "encoded-lf", "encoded-nul"],
    )
    async def test_encoded_word_looking_subject_is_drafted_literally(
        self, session_factory, approved, provider, subject
    ):
        _set_revision_letter(session_factory, approved, subject=subject)
        outcome = await _create(session_factory, approved["capability"], provider)

        assert outcome.code == "CREATED" and len(provider.creates) == 1
        decoded, body = _transmitted(provider)
        assert decoded == subject == provider.creates[0][0].subject
        assert not any(char in decoded for char in ("\r", "\n", "\x00"))
        assert _ledger(session_factory).content_sha256 == gd.content_hash(
            "9d-v1", ACCOUNT, decoded, body
        )

    @pytest.mark.asyncio
    async def test_subject_that_would_not_round_trip_is_refused_before_any_claim(
        self, session_factory, approved, provider, monkeypatch
    ):
        # Re-introduce the S9D-ASTRA-001 encoding defect: the final decoded
        # subject check must refuse it in the pre-claim dry run.
        monkeypatch.setattr(draft_base, "_subject_header_value", lambda subject: subject)
        _set_revision_letter(
            session_factory, approved, subject=f"Bewerbung als {ENCODED_WORD_TITLE}"
        )
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "CONTENT_INVALID"
        assert _ledgers(session_factory) == [] and provider.creates == []

    @pytest.mark.asyncio
    async def test_full_9b_9c_9d_flow_keeps_the_approved_subject_exact(
        self, session_factory, seeded, sender, provider
    ):
        _change_job(session_factory, seeded, title=ENCODED_WORD_TITLE)
        token = await s9c._prepare(session_factory, seeded, sender)  # real Stage 9B
        shown = await s9c._request(session_factory, token, sender)  # real Stage 9C review
        assert shown.code == "REVIEW_SHOWN", shown
        link = s9c._links(session_factory)[0]
        decided = await _decide(session_factory, link.approval_capability, "f")
        assert decided.code == "APPROVED", decided
        approved_subject = _approved_revision_subject(session_factory, link.review_id)
        assert approved_subject == f"Bewerbung als {ENCODED_WORD_TITLE}"

        outcome = await _create(session_factory, link.approval_capability, provider)

        assert outcome.code == "CREATED" and len(provider.creates) == 1
        decoded, body = _transmitted(provider)
        assert decoded == approved_subject
        parsed = _parsed(provider.mime[0])
        for name in ("To", "Cc", "Bcc", "Reply-To", "In-Reply-To", "References"):
            assert parsed[name] is None
        row = _ledger(session_factory)
        assert row.content_sha256 == gd.content_hash("9d-v1", ACCOUNT, decoded, body)
        review = session_factory().get(ApplicationPackageReviewRecord, link.review_id)
        assert row.approved_revision_id == review.approved_revision_id  # exact revision


def _decided_status(session_factory, approved) -> str:
    db = session_factory()
    try:
        return db.get(ApplicationPackageReviewRecord, approved["link"].review_id).status
    finally:
        db.close()


# --- exact binding --------------------------------------------------------------------


class TestExactBinding:
    @pytest.mark.asyncio
    async def test_unknown_capability(self, session_factory, approved, provider):
        outcome = await _create(session_factory, "AbCdEfGh_-123456", provider)
        assert outcome.code == "UNKNOWN_CAPABILITY" and _ledgers(session_factory) == []

    @pytest.mark.asyncio
    async def test_pending_review_is_not_approved(self, session_factory, requested, provider):
        outcome = await _create(session_factory, requested["capability"], provider)
        assert outcome.code == "NOT_APPROVED" and provider.creates == []

    @pytest.mark.asyncio
    async def test_rejected_review(self, session_factory, requested, provider):
        await _decide(session_factory, requested["capability"], "x")
        outcome = await _create(session_factory, requested["capability"], provider)
        assert outcome.code == "ALREADY_REJECTED" and provider.creates == []

    @pytest.mark.asyncio
    async def test_api_approval_of_another_revision_is_refused(
        self, session_factory, requested, provider
    ):
        review_id = requested["link"].review_id
        _api(
            session_factory, "patch", review_id, 1, None, BewerbungContentPatch(subject="v2"), None
        )
        _api(session_factory, "approve", review_id, 2, True, None)
        outcome = await _create(session_factory, requested["capability"], provider)
        assert outcome.code == "APPROVED_ELSEWHERE"
        assert _ledgers(session_factory) == [] and provider.creates == []

    @pytest.mark.asyncio
    async def test_replaced_generation(self, session_factory, approved, provider):
        await _regenerate(session_factory, approved)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "PACKAGE_REPLACED" and provider.creates == []

    @pytest.mark.asyncio
    async def test_profile_change_is_stale(self, session_factory, approved, provider):
        _bump_profile(session_factory)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "PACKAGE_STALE" and provider.creates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", [{"title": "Senior Python Developer"}, {"company": "Other"}])
    async def test_job_change_is_stale(self, session_factory, approved, provider, change):
        _change_job(session_factory, approved, **change)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "PACKAGE_STALE" and provider.creates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["APPLIED", "REJECTED", "WITHDRAWN"])
    async def test_ineligible_job(self, session_factory, approved, provider, status):
        _change_job(session_factory, approved, status=status)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "JOB_NOT_ELIGIBLE" and provider.creates == []

    @pytest.mark.asyncio
    async def test_saved_job_is_eligible(self, session_factory, approved, provider):
        _change_job(session_factory, approved, status="SAVED")
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "CREATED"

    @pytest.mark.asyncio
    async def test_stale_input_identity_on_the_link(self, session_factory, approved, provider):
        _update(
            session_factory,
            update(TelegramBewerbungApprovalRecord).values(input_identity="f" * 64),
        )
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "PACKAGE_STALE" and provider.creates == []

    @pytest.mark.asyncio
    async def test_no_latest_lookup_is_ever_used(
        self, session_factory, approved, provider, monkeypatch
    ):
        def forbidden(*args, **kwargs):
            raise AssertionError("latest lookup")

        monkeypatch.setattr("app.db.review_package_repository.get_latest_revision", forbidden)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "CREATED"

    @pytest.mark.asyncio
    async def test_frozen_handoff_mismatch_on_retry_is_replaced(
        self, session_factory, approved, provider
    ):
        provider.create_exc = DraftCreateRejectedError("x")
        assert (await _create(session_factory, approved["capability"], provider)).code == "FAILED"
        _update(session_factory, update(GmailApplicationDraftRecord).values(generation=99))
        provider.create_exc = None
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "PACKAGE_REPLACED" and len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_package_replaced_between_claim_and_arm_releases_the_claim(
        self, session_factory, approved, provider, monkeypatch
    ):
        real = gd.revalidate_approved_link_locked
        calls = []

        def racing(db, link_id):
            calls.append(link_id)
            if len(calls) == 2:  # the arm phase: a concurrent replacement committed
                _run_in_thread(_regenerate(session_factory, approved))
            return real(db, link_id)

        monkeypatch.setattr(gd, "revalidate_approved_link_locked", racing)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "PACKAGE_REPLACED" and provider.creates == []
        row = _ledger(session_factory)
        assert row.state == "FAILED" and row.append_started_at is None


# --- provider outcomes, FAILED retry --------------------------------------------------------


class TestProviderOutcomes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            DraftCreateRejectedError("x"),
            DraftBudgetExhaustedError("x"),
            DraftsMailboxInvalidError("x"),
            draft_base.DraftsDisabledError("x"),
            draft_base.DraftAuthError("x"),
        ],
    )
    async def test_definite_failures_are_failed_with_retry_button(
        self, session_factory, approved, provider, exc
    ):
        provider.create_exc = exc
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "FAILED" and outcome.action == "c"
        keyboard = gd.outcome_keyboard(outcome, approved["capability"])
        assert keyboard["inline_keyboard"][0][0]["text"] == gd.RETRY_BUTTON
        row = _ledger(session_factory)
        assert (row.state, row.last_error) == ("FAILED", exc.code)
        assert row.claim_token is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc", [DraftCreateOutcomeUnknownError("x"), TimeoutError("t"), RuntimeError("r")]
    )
    async def test_ambiguous_failures_are_uncertain_with_only_a_recheck_button(
        self, session_factory, approved, provider, exc
    ):
        provider.create_exc = exc
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "UNCERTAIN" and outcome.action == "r"
        assert _ledger(session_factory).state == "UNCERTAIN"

    @pytest.mark.asyncio
    async def test_created_without_uid_pair(self, session_factory, approved, provider):
        provider.create_result = DraftCreateResult(None, None)
        outcome = await _create(session_factory, approved["capability"], provider)
        row = _ledger(session_factory)
        assert outcome.code == "CREATED"
        assert (row.uid_validity, row.draft_uid, row.reconciled) == (None, None, False)

    @pytest.mark.asyncio
    async def test_failed_retry_is_a_new_attempt_with_new_marker_and_new_target(
        self, session_factory, approved, provider
    ):
        provider.create_exc = DraftCreateRejectedError("x")
        await _create(session_factory, approved["capability"], provider)
        first = _ledger(session_factory)
        provider.create_exc = None
        outcome = await _create(
            session_factory,
            approved["capability"],
            provider,
            gmail_drafts_mailbox="[Gmail]/Entwürfe",
        )
        row = _ledger(session_factory)
        assert outcome.code == "CREATED" and row.attempt_count == 2
        assert row.marker_message_id != first.marker_message_id
        assert row.drafts_mailbox == "[Gmail]/Entwürfe"
        assert [create[0].message_id for create in provider.creates] == [
            first.marker_message_id,
            row.marker_message_id,
        ]

    @pytest.mark.asyncio
    async def test_failed_retry_requires_full_freshness(self, session_factory, approved, provider):
        provider.create_exc = DraftCreateRejectedError("x")
        await _create(session_factory, approved["capability"], provider)
        _bump_profile(session_factory)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "PACKAGE_STALE" and len(provider.creates) == 1
        assert _ledger(session_factory).state == "FAILED"

    @pytest.mark.asyncio
    async def test_no_retry_from_created_or_uncertain(self, session_factory, approved, provider):
        provider.create_exc = TimeoutError("t")
        await _create(session_factory, approved["capability"], provider)
        provider.create_exc = None
        for _ in range(3):
            outcome = await _create(session_factory, approved["capability"], provider)
            assert outcome.code == "UNCERTAIN" and outcome.action == "r"
        assert len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_cancellation_leaves_the_attempt_uncertain(
        self, session_factory, approved, provider
    ):
        provider.create_exc = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await _create(session_factory, approved["capability"], provider)
        assert _ledger(session_factory).state == "UNCERTAIN"
        assert (await _create(session_factory, approved["capability"], provider)).code == (
            "UNCERTAIN"
        )
        assert len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_frozen_budget_and_deadline_are_not_extended_by_config(
        self, session_factory, approved, provider, clock
    ):
        provider.on_create = lambda: clock.advance(1)
        await _create(
            session_factory,
            approved["capability"],
            provider,
            gmail_draft_attempt_budget_seconds=30,
        )
        row = _ledger(session_factory)
        assert row.attempt_budget_seconds == 30
        assert provider.creates[0][2] == T0 + timedelta(seconds=30)


# --- stale claims and stale armed attempts ---------------------------------------------------


class TestStaleWorkers:
    def _crash_after_claim(self, session_factory, approved, monkeypatch):
        """A worker whose process died right after its acknowledged claim."""

        def die(*args, **kwargs):
            raise SystemExit("crash")

        monkeypatch.setattr(gd, "_phase_arm", die)
        with pytest.raises(SystemExit):
            asyncio.run(_create(session_factory, approved["capability"], FakeProvider()))
        monkeypatch.undo()

    def test_pre_fence_takeover_only_after_the_lease(
        self, session_factory, approved, provider, monkeypatch, clock
    ):
        self._crash_after_claim(session_factory, approved, monkeypatch)
        monkeypatch.setattr(gd, "_now", clock)
        old = _ledger(session_factory)
        assert (old.state, old.append_started_at) == ("CREATING", None)

        early = asyncio.run(_create(session_factory, approved["capability"], provider))
        assert early.code == "IN_PROGRESS" and provider.creates == []

        clock.advance(gd.CLAIM_LEASE_SECONDS + 1)
        later = asyncio.run(_create(session_factory, approved["capability"], provider))
        row = _ledger(session_factory)
        assert later.code == "CREATED" and row.attempt_count == 2
        assert len(provider.creates) == 1

    def test_pre_fence_takeover_requires_full_freshness(
        self, session_factory, approved, provider, monkeypatch, clock
    ):
        self._crash_after_claim(session_factory, approved, monkeypatch)
        monkeypatch.setattr(gd, "_now", clock)
        clock.advance(gd.CLAIM_LEASE_SECONDS + 1)
        _change_job(session_factory, approved, status="APPLIED")
        outcome = asyncio.run(_create(session_factory, approved["capability"], provider))
        assert outcome.code == "JOB_NOT_ELIGIBLE" and provider.creates == []
        row = _ledger(session_factory)
        assert (row.state, row.attempt_count) == ("CREATING", 1)

    @pytest.mark.asyncio
    async def test_wrong_claim_token_cannot_finalize(self, session_factory, approved, provider):
        def steal():
            _update(
                session_factory,
                update(GmailApplicationDraftRecord).values(claim_token="Z" * 22),
            )

        provider.on_create = steal
        outcome = await _create(session_factory, approved["capability"], provider)
        row = _ledger(session_factory)
        assert row.state == "CREATING" and row.claim_token == "Z" * 22  # not ours: respected
        assert outcome.code == "IN_PROGRESS"

    @pytest.mark.asyncio
    async def test_aged_armed_attempt_becomes_uncertain_never_new_authority(
        self, session_factory, approved, provider, clock
    ):
        def hang():
            clock.advance(60 + gd.STALE_MARGIN_SECONDS + 1)
            inner.append(asyncio.run(_create(session_factory, approved["capability"], provider)))
            raise TimeoutError("worker died")

        inner = []
        provider.on_create = hang
        await _create(session_factory, approved["capability"], provider)
        assert inner[0].code == "UNCERTAIN"
        assert _ledger(session_factory).state == "UNCERTAIN"
        assert len(provider.creates) == 1


# --- S9D-ARCH-001: sticky UNCERTAIN and positive-only reconciliation --------------------------


class TestStickyUncertain:
    @pytest_asyncio.fixture()
    async def uncertain(self, session_factory, approved, provider):
        provider.create_exc = DraftCreateOutcomeUnknownError("x")
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "UNCERTAIN"
        provider.create_exc = None
        return approved

    @pytest.mark.asyncio
    async def test_zero_matches_at_any_age_stays_uncertain(
        self, session_factory, uncertain, provider, clock
    ):
        provider.lookup_result = DraftLookupResult(7, ())
        cap = uncertain["capability"]
        assert (await _recheck(session_factory, cap, provider)).code == "TOO_EARLY"  # 0 s
        assert provider.lookups == []  # pacing only, never evidence
        for seconds in (120, 3600, 86400):
            clock.now = T0 + timedelta(seconds=seconds + 1)
            outcome = await _recheck(session_factory, cap, provider)
            assert outcome.code == "STILL_UNCERTAIN" and outcome.action == "r"
            assert _ledger(session_factory).state == "UNCERTAIN"
            create = await _create(session_factory, cap, provider)
            assert create.code == "UNCERTAIN" and create.action == "r"
        assert len(provider.creates) == 1 and len(provider.lookups) == 3

    @pytest.mark.asyncio
    async def test_zero_immediately_with_zero_min_age_stays_uncertain(
        self, session_factory, uncertain, provider, clock
    ):
        provider.lookup_result = DraftLookupResult(7, ())
        clock.advance(61)  # past the frozen deadline; min age 0
        outcome = await _recheck(
            session_factory,
            uncertain["capability"],
            provider,
            gmail_draft_reconcile_min_age_seconds=0,
        )
        assert outcome.code == "STILL_UNCERTAIN" and len(provider.lookups) == 1

    @pytest.mark.asyncio
    async def test_recheck_waits_for_the_frozen_deadline(
        self, session_factory, uncertain, provider, clock
    ):
        clock.advance(30)
        outcome = await _recheck(
            session_factory,
            uncertain["capability"],
            provider,
            gmail_draft_reconcile_min_age_seconds=0,
        )
        assert outcome.code == "TOO_EARLY" and provider.lookups == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "exc"),
        [
            (DraftLookupResult(7, (3, 9)), None),  # >1
            (None, DraftLookupError("error")),
            (None, DraftLookupError("partial/malformed SEARCH")),
            (None, DraftLookupError("original mailbox gone")),
            (None, OSError("network")),
            (DraftLookupResult(7, ()), None),  # moved / deleted / sent manually
        ],
    )
    async def test_non_positive_results_stay_uncertain(
        self, session_factory, uncertain, provider, clock, result, exc
    ):
        provider.lookup_result, provider.lookup_exc = result, exc
        clock.advance(200)
        for _ in range(2):
            outcome = await _recheck(session_factory, uncertain["capability"], provider)
            assert outcome.code == "STILL_UNCERTAIN" and outcome.action == "r"
        row = _ledger(session_factory)
        assert row.state == "UNCERTAIN" and row.attempt_count == 1
        assert len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_one_match_is_created_and_reconciled(
        self, session_factory, uncertain, provider, clock
    ):
        clock.advance(200)
        before = _ledger(session_factory)
        outcome = await _recheck(session_factory, uncertain["capability"], provider)
        row = _ledger(session_factory)
        assert outcome.code == "RECONCILED"
        assert (row.state, row.reconciled, row.uid_validity, row.draft_uid) == (
            "CREATED",
            True,
            7,
            42,
        )
        assert row.created_in_gmail_at is not None
        assert row.marker_message_id == before.marker_message_id  # bundle preserved
        reconcile_target, _ = provider.lookups[0]
        assert reconcile_target.message_id == before.marker_message_id
        assert len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_reconciliation_uses_the_original_frozen_target(
        self, session_factory, uncertain, provider, clock
    ):
        clock.advance(200)
        await _recheck(
            session_factory,
            uncertain["capability"],
            provider,
            gmail_drafts_mailbox="[Gmail]/Entwürfe",
        )
        target = provider.lookups[0][0].target
        assert (target.mailbox, target.mailbox_wire, target.account_key) == (
            "[Gmail]/Drafts",
            '"[Gmail]/Drafts"',
            ACCOUNT,
        )

    @pytest.mark.asyncio
    async def test_account_change_blocks_reconciliation_and_creation(
        self, session_factory, uncertain, provider, clock
    ):
        clock.advance(200)
        outcome = await _recheck(
            session_factory, uncertain["capability"], provider, gmail_username="new@example.com"
        )
        assert outcome.code == "ACCOUNT_CHANGED" and provider.lookups == []
        create = await _create(
            session_factory, uncertain["capability"], provider, gmail_username="new@example.com"
        )
        assert create.code == "UNCERTAIN" and len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_recheck_of_other_states(self, session_factory, approved, provider):
        assert (await _recheck(session_factory, approved["capability"], provider)).code == (
            "NO_DRAFT_STATUS"
        )
        await _create(session_factory, approved["capability"], provider)
        outcome = await _recheck(session_factory, approved["capability"], provider)
        assert outcome.code == "ALREADY_CREATED" and provider.lookups == []


# --- S9D-ARCH-002: commit acknowledgment lost AFTER a real commit ---------------------------


class AckLoss:
    """Session factory whose `commit()` REALLY commits and THEN raises a
    client-facing OperationalError when `predicate(rows)` holds -- the
    acknowledgment-lost case, not a pre-commit failure. Optionally the DB
    then becomes unavailable for every later session."""

    def __init__(self, factory, predicate, *, times=1, then_unavailable=False, really_commit=True):
        self.factory = factory
        self.predicate = predicate
        self.remaining = times
        self.fired = 0
        self.then_unavailable = then_unavailable
        self.really_commit = really_commit

    def __call__(self):
        db = self.factory()
        if self.fired and self.then_unavailable:

            def down(*args, **kwargs):
                raise OperationalError("SELECT", {}, Exception("database unavailable"))

            for name in ("execute", "scalar", "scalars", "get"):
                setattr(db, name, down)
            return db
        real_commit = db.commit

        def commit():
            if not self.really_commit and self.remaining:
                db.flush()
                pending = db.scalars(select(GmailApplicationDraftRecord)).all()
                if self.predicate(pending):
                    self.remaining -= 1
                    self.fired += 1
                    db.rollback()  # the COMMIT never happened
                    raise OperationalError("COMMIT", {}, Exception("lost before commit"))
            real_commit()
            if self.remaining and self.really_commit:
                rows = self.factory().scalars(select(GmailApplicationDraftRecord)).all()
                if self.predicate(rows):
                    self.remaining -= 1
                    self.fired += 1
                    raise OperationalError("COMMIT", {}, Exception("connection lost after commit"))

        db.commit = commit
        return db


def _armed(rows):
    return any(r.state == "CREATING" and r.append_started_at is not None for r in rows)


def _state_is(state):
    return lambda rows: any(r.state == state for r in rows)


class TestCommitAcknowledgment:
    @pytest.mark.asyncio
    async def test_begin_append_ack_lost_never_appends(
        self, session_factory, approved, provider, clock
    ):
        lossy = AckLoss(session_factory, _armed)
        outcome = await _create(lossy, approved["capability"], provider)
        assert lossy.fired == 1
        assert outcome.code == "STATUS_UNSAVED" and "nichts an Gmail übertragen" in outcome.notice
        assert provider.creates == []  # APPEND count 0 for that operation
        row = _ledger(session_factory)
        assert row.state == "CREATING" and row.append_started_at is not None  # really committed

        # A replay never resumes the armed row into an APPEND ...
        again = await _create(session_factory, approved["capability"], provider)
        assert again.code == "IN_PROGRESS" and provider.creates == []
        # ... and once aged it becomes UNCERTAIN -- never re-armed.
        clock.advance(60 + gd.STALE_MARGIN_SECONDS + 1)
        aged = await _create(session_factory, approved["capability"], provider)
        assert aged.code == "UNCERTAIN" and provider.creates == []
        assert _ledger(session_factory).attempt_count == 1

    @pytest.mark.asyncio
    async def test_begin_append_really_rolled_back_allows_a_later_takeover(
        self, session_factory, approved, provider, clock, monkeypatch
    ):
        real = repo.begin_append

        def failing(db, *args, **kwargs):
            raise OperationalError("UPDATE", {}, Exception("definite failure before COMMIT"))

        monkeypatch.setattr(repo, "begin_append", failing)
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "STATUS_UNAVAILABLE" and provider.creates == []
        row = _ledger(session_factory)
        assert (row.state, row.append_started_at) == ("CREATING", None)
        monkeypatch.setattr(repo, "begin_append", real)
        clock.advance(gd.CLAIM_LEASE_SECONDS + 1)
        later = await _create(session_factory, approved["capability"], provider)
        assert later.code == "CREATED" and _ledger(session_factory).attempt_count == 2
        assert len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_claim_ack_lost_grants_no_authority(self, session_factory, approved, provider):
        lossy = AckLoss(session_factory, lambda rows: bool(rows))
        outcome = await _create(lossy, approved["capability"], provider)
        assert outcome.code == "STATUS_UNSAVED" and provider.creates == []
        assert _ledger(session_factory).state == "CREATING"

    @pytest.mark.asyncio
    async def test_created_finalize_ack_lost_is_recovered_db_only(
        self, session_factory, approved, provider
    ):
        lossy = AckLoss(session_factory, _state_is("CREATED"))
        outcome = await _create(lossy, approved["capability"], provider)
        assert lossy.fired == 1 and outcome.code == "CREATED"
        assert len(provider.creates) == 1
        assert _ledger(session_factory).state == "CREATED"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "state"),
        [
            (None, "CREATED"),
            (DraftCreateRejectedError("x"), "FAILED"),
            (TimeoutError("t"), "UNCERTAIN"),
        ],
    )
    async def test_finalize_that_never_committed_is_retried_db_only_same_attempt(
        self, session_factory, approved, provider, exc, state
    ):
        provider.create_exc = exc
        lossy = AckLoss(session_factory, _state_is(state), really_commit=False)
        outcome = await _create(lossy, approved["capability"], provider)
        assert lossy.fired == 1 and outcome.code == state
        assert len(provider.creates) == 1
        row = _ledger(session_factory)
        assert (row.state, row.attempt_count) == (state, 1)

    @pytest.mark.asyncio
    async def test_failed_finalize_ack_lost_is_recovered_db_only(
        self, session_factory, approved, provider
    ):
        provider.create_exc = DraftCreateRejectedError("x")
        lossy = AckLoss(session_factory, _state_is("FAILED"))
        outcome = await _create(lossy, approved["capability"], provider)
        assert lossy.fired == 1 and outcome.code == "FAILED"
        assert len(provider.creates) == 1 and _ledger(session_factory).state == "FAILED"

    @pytest.mark.asyncio
    async def test_uncertain_finalize_ack_lost_is_recovered_db_only(
        self, session_factory, approved, provider
    ):
        provider.create_exc = TimeoutError("t")
        lossy = AckLoss(session_factory, _state_is("UNCERTAIN"))
        outcome = await _create(lossy, approved["capability"], provider)
        assert lossy.fired == 1 and outcome.code == "UNCERTAIN"
        assert len(provider.creates) == 1 and _ledger(session_factory).state == "UNCERTAIN"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "predicate", "code", "durable"),
        [
            (None, "CREATED", "PERSISTENCE_UNCERTAIN", "CREATED"),
            (DraftCreateRejectedError("x"), "FAILED", "FINALIZE_UNSAVED", "FAILED"),
            (TimeoutError("t"), "UNCERTAIN", "FINALIZE_UNSAVED", "UNCERTAIN"),
        ],
    )
    async def test_db_unavailable_during_recovery_has_no_network_authority(
        self, session_factory, approved, provider, exc, predicate, code, durable
    ):
        provider.create_exc = exc
        lossy = AckLoss(session_factory, _state_is(predicate), then_unavailable=True)
        outcome = await _create(lossy, approved["capability"], provider)
        assert outcome.code == code and len(provider.creates) == 1
        assert "gesendet" in outcome.notice
        assert _ledger(session_factory).state == durable

    @pytest.mark.asyncio
    async def test_failed_evidence_never_downgrades_uncertain(
        self, session_factory, approved, provider, clock
    ):
        def stale_classifier_wins():
            clock.advance(60 + gd.STALE_MARGIN_SECONDS + 1)
            asyncio.run(_create(session_factory, approved["capability"], provider))
            raise DraftCreateRejectedError("tagged NO arrives late")

        provider.on_create = stale_classifier_wins
        outcome = await _create(session_factory, approved["capability"], provider)
        assert outcome.code == "UNCERTAIN" and _ledger(session_factory).state == "UNCERTAIN"
        assert len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_persisted_created_is_never_downgraded(
        self, session_factory, approved, provider, clock
    ):
        await _create(session_factory, approved["capability"], provider)
        clock.advance(10**6)
        for action in (_create, _recheck):
            await action(session_factory, approved["capability"], provider)
        row = _ledger(session_factory)
        assert row.state == "CREATED" and len(provider.creates) == 1


# --- retained tagged-OK finalization ---------------------------------------------------------


class TestRetainedTaggedOk:
    @pytest.mark.asyncio
    async def test_order_a_stale_classifier_wins_then_retained_ok_creates(
        self, session_factory, approved, provider, clock
    ):
        inner = []

        def stale_classifier_wins():
            clock.advance(60 + gd.STALE_MARGIN_SECONDS + 1)
            inner.append(asyncio.run(_create(session_factory, approved["capability"], provider)))
            assert _ledger(session_factory).state == "UNCERTAIN"
            assert _ledger(session_factory).claim_token is None

        provider.on_create = stale_classifier_wins
        outcome = await _create(session_factory, approved["capability"], provider)
        row = _ledger(session_factory)
        assert inner[0].code == "UNCERTAIN" and outcome.code == "CREATED"
        assert (row.state, row.reconciled, row.uid_validity, row.draft_uid) == (
            "CREATED",
            False,
            7,
            42,
        )
        assert row.claim_token is None and row.claim_started_at is None
        assert len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_order_b_retained_finalize_wins_before_the_stale_classifier(
        self, session_factory, approved, provider, clock
    ):
        outcome = await _create(session_factory, approved["capability"], provider)
        clock.advance(60 + gd.STALE_MARGIN_SECONDS + 1)
        later = await _create(session_factory, approved["capability"], provider)
        assert (outcome.code, later.code) == ("CREATED", "ALREADY_CREATED")
        assert _ledger(session_factory).state == "CREATED" and len(provider.creates) == 1

    @pytest.mark.asyncio
    async def test_retained_cas_binds_id_state_attempt_and_marker(
        self, session_factory, approved, provider, clock, monkeypatch
    ):
        seen = {}
        real = repo.retained_ok_created

        def spy(db, **kwargs):
            seen.update(kwargs)
            return real(db, **kwargs)

        monkeypatch.setattr(repo, "retained_ok_created", spy)

        def stale_classifier_wins():
            clock.advance(60 + gd.STALE_MARGIN_SECONDS + 1)
            asyncio.run(_create(session_factory, approved["capability"], provider))

        provider.on_create = stale_classifier_wins
        await _create(session_factory, approved["capability"], provider)
        row = _ledger(session_factory)
        assert seen["ledger_id"] == row.id and seen["attempt_count"] == 1
        assert seen["marker"] == row.marker_message_id
        assert "claim_token" not in seen

    @pytest.mark.asyncio
    async def test_evidence_cannot_be_reconstructed_by_a_replay(
        self, session_factory, approved, provider, clock
    ):
        provider.create_exc = DraftCreateOutcomeUnknownError("x")
        await _create(session_factory, approved["capability"], provider)
        provider.create_exc = None
        clock.advance(10**5)
        for _ in range(3):
            assert (await _create(session_factory, approved["capability"], provider)).code == (
                "UNCERTAIN"
            )
        assert _ledger(session_factory).state == "UNCERTAIN" and len(provider.creates) == 1
        assert not hasattr(GmailApplicationDraftRecord, "retained_claim_token")


# --- state isolation, privacy, outbound -----------------------------------------------------


class TestIsolationAndPrivacy:
    @pytest.mark.asyncio
    async def test_every_outcome_leaves_other_stages_untouched(
        self, session_factory, approved, provider, clock
    ):
        before = _state(session_factory)
        provider.create_exc = DraftCreateRejectedError("x")
        await _create(session_factory, approved["capability"], provider)  # FAILED
        provider.create_exc = TimeoutError("t")
        await _create(session_factory, approved["capability"], provider)  # retry -> UNCERTAIN
        await _create(session_factory, approved["capability"], provider)  # duplicate
        clock.advance(200)
        provider.lookup_result = DraftLookupResult(7, ())
        await _recheck(session_factory, approved["capability"], provider)  # zero
        provider.lookup_result = DraftLookupResult(7, (42,))
        await _recheck(session_factory, approved["capability"], provider)  # reconciled
        assert _ledger(session_factory).state == "CREATED"
        assert _state(session_factory) == before

    @pytest.mark.asyncio
    async def test_commit_ambiguity_leaves_other_stages_untouched(
        self, session_factory, approved, provider
    ):
        before = _state(session_factory)
        await _create(AckLoss(session_factory, _armed), approved["capability"], provider)
        assert _state(session_factory) == before

    @pytest.mark.asyncio
    async def test_logs_never_contain_sensitive_values(
        self, session_factory, approved, provider, clock, caplog
    ):
        caplog.set_level(logging.DEBUG)
        provider.create_exc = DraftCreateOutcomeUnknownError("x")
        await _create(session_factory, approved["capability"], provider)
        provider.create_exc = None
        clock.advance(200)
        provider.lookup_exc = OSError("secret raw IMAP text me@example.com")
        await _recheck(session_factory, approved["capability"], provider)
        provider.lookup_exc = None
        await _recheck(session_factory, approved["capability"], provider)
        await _create(AckLoss(session_factory, _armed), approved["capability"], provider)

        row = _ledger(session_factory)
        message = provider.creates[0][0]
        secrets_ = [
            ACCOUNT,
            PASSWORD,
            approved["capability"],
            f"bm:{approved['capability']}",
            message.subject,
            message.body_lf.strip().split("\n")[0],
            row.marker_message_id,
            row.content_sha256,
            "[Gmail]/Drafts",
            "secret raw IMAP text",
            "uid_validity=7",
            "draft_uid=42",
        ]
        for value in secrets_:
            assert value not in caplog.text, value
        assert "gmail_draft_create" in caplog.text  # ids/codes are logged

    def test_outcome_and_dto_reprs_hide_content(self):
        texts = repr(gd.RenderedDraft("SECRET SUBJECT", "SECRET BODY"))
        assert "SECRET" not in texts


_FORBIDDEN_IMPORT_PREFIXES = (
    "smtplib",
    "httpx",
    "requests",
    "urllib",
    "aiohttp",
    "app.providers.email.smtp",
    "app.providers.email.outbound_base",
    "app.services.response_draft",
    "app.services.follow_up",
    "app.services.company_research",
    "app.services.automation",
    "app.domain.status_transitions",
    "app.agents",
    "app.providers.bewerbung",
    "app.services.bewerbung",
)


@pytest.mark.parametrize(
    "relative",
    [
        "app/services/gmail_application_draft.py",
        "app/db/gmail_application_draft_repository.py",
        "app/providers/email/imap_draft.py",
        "app/providers/email/draft_base.py",
    ],
)
def test_stage_9d_modules_import_no_send_status_or_generation_code(relative):
    tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        for name in names:
            assert not name.startswith(_FORBIDDEN_IMPORT_PREFIXES), (relative, name)
            assert "update_job_status" not in name and "record_decision" not in name
            assert "ReviewPackageService" not in name


def test_stage_9d_repository_writes_only_its_own_table():
    tree = ast.parse(
        (PROJECT_ROOT / "app/db/gmail_application_draft_repository.py").read_text(encoding="utf-8")
    )
    updated = {
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "update"
        and node.args
        and isinstance(node.args[0], ast.Name)
    }
    assert updated == {"Ledger"}


# --- the Stage 9C read-only revalidation wrapper ------------------------------------------


@pytest.mark.asyncio
async def test_revalidation_wrapper_is_read_only_and_caller_owned(session_factory, approved):
    from app.services.telegram_bewerbung_approval import revalidate_approved_link_locked

    before = _state(session_factory)
    db = session_factory()
    calls = []
    db.commit = lambda: calls.append("commit")
    db.rollback_original = db.rollback
    db.rollback = lambda: calls.append("rollback")
    try:
        snapshot = revalidate_approved_link_locked(db, approved["link"].id)
        assert not db.new and not db.dirty and not db.deleted
        assert calls == []  # never commits or rolls back: the caller owns the txn
    finally:
        db.rollback_original()
        db.close()
    review = session_factory().get(ApplicationPackageReviewRecord, approved["link"].review_id)
    assert snapshot.approved_revision_id == review.approved_revision_id
    assert snapshot.link_id == approved["link"].id
    assert snapshot.reviewed_bewerbung().subject.value
    for secret in (snapshot.package_token, snapshot.input_identity):
        assert secret not in repr(snapshot)
    assert _state(session_factory) == before


@pytest.mark.asyncio
async def test_revalidation_wrapper_codes(session_factory, requested):
    from app.services.telegram_bewerbung_approval import revalidate_approved_link_locked

    db = session_factory()
    try:
        assert revalidate_approved_link_locked(db, requested["link"].id) == "NOT_APPROVED"
        assert revalidate_approved_link_locked(db, 999999) == "UNKNOWN_CAPABILITY"
    finally:
        db.rollback()
        db.close()
