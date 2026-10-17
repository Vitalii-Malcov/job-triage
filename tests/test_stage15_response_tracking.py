"""Stage 15: inbound response classification, correlation, processing, the
real ALD pilot flow, operator CLI/bootstrap, read-only API and the
no-outbound guarantees.

Inbound messages are synthetic or the sanitized real ALD receipt
(tests/stage15_fixtures.py). No test opens a network connection.
"""

import ast
import email
import hashlib
import smtplib
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.command import upgrade
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import scripts.stage15_application_tracker as cli
import scripts.stage15_import_ald_application as bootstrap
from app.agents.application_response_classifier import (
    classify_application_response,
    normalize_body,
    normalize_subject,
)
from app.agents.email_classifier import classify_email
from app.core.config import Settings
from app.db.base import Base
from app.db.models import ApplicationEventRecord, ApplicationRecord, GmailMessageRecord
from app.db.session import get_db
from app.domain.application_response_policy import decide_response
from app.main import app as fastapi_app
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationEventType as T
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.application_lifecycle import ResponseClassification as R
from app.providers.email import imap as imap_module
from app.security import rate_limit as rate_limit_module
from app.services.application_correlation import (
    InboundMessage,
    correlate_message,
    message_key_for,
    normalize_occurred_at,
)
from app.services.application_notifications import render_lifecycle_notification
from app.services.application_response_processor import (
    ApplicationResponseProcessor,
    inbound_from_parsed,
    inbound_from_record,
)
from app.services.application_tracker import ApplicationTracker
from tests import stage15_fixtures as fx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPLICANT = "applicant@example.invalid"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage15_resp.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _app(
    db,
    *,
    company="ALD Vacuum Technologies GmbH",
    role="Backend Developer (m/w/div.)",
    ref="20385-2347902-S",
    domains=("ald-vt.de", "ald-vt.com"),
    status=S.SUBMITTED,
    external="962",
):
    return (
        ApplicationTracker()
        .create_application(
            db,
            company_name=company,
            role_title=role,
            source="bundesagentur",
            source_reference=ref,
            application_channel=C.EMPLOYER_PORTAL,
            initial_status=status,
            origin=O.IMPORT,
            actor="test",
            occurred_at=fx.ALD_SUBMITTED_AT,
            employer_domains=domains,
            external_job_id=external,
            correlation_key=f"portal-submission:{ref}",
        )
        .application
    )


def _msg(
    subject=fx.ALD_RECEIPT_SUBJECT,
    body=fx.ALD_RECEIPT_TEXT,
    *,
    sender=fx.ALD_RECEIPT_FROM,
    display=fx.ALD_RECEIPT_DISPLAY_NAME,
    key="mid:ald-receipt@ald-vt.de",
    at=fx.ALD_RECEIPT_ARRIVAL,
    html=False,
    replies=(),
    message_id="<ald-receipt@ald-vt.de>",
    direction="INBOUND",
):
    return InboundMessage(
        account_key="applicant@example.invalid",
        message_key=key,
        message_id_header=message_id,
        reply_to_ids=tuple(replies),
        from_address=sender,
        from_display_name=display,
        to_addresses=(APPLICANT,),
        subject=subject,
        body=body,
        has_html=html,
        occurred_at=at,
        direction=direction,
    )


def _process(db, messages, apply=True, **kw):
    return ApplicationResponseProcessor().process(
        db,
        messages if isinstance(messages, list) else [messages],
        apply=apply,
        applicant_addresses=(APPLICANT,),
        **kw,
    )


def _events(db):
    return db.scalar(select(func.count()).select_from(ApplicationEventRecord))


# --- J. real ALD receipt classifier -------------------------------------------------


def test_real_ald_receipt_is_a_receipt_confirmation():
    result = classify_application_response(
        fx.ALD_RECEIPT_SUBJECT, fx.ALD_RECEIPT_TEXT, fx.ALD_RECEIPT_FROM
    )
    assert result.classification == R.RECEIPT_CONFIRMATION
    assert result.base_category == "APPLICATION_RECEIVED" and result.confidence == "HIGH"
    assert "Eingang" in result.evidence_fragment or "Eingangsbest" in result.evidence_fragment


def test_real_ald_receipt_body_alone_and_subject_alone_are_receipts():
    assert classify_application_response("", fx.ALD_RECEIPT_TEXT, None).classification == (
        R.RECEIPT_CONFIRMATION
    )
    assert classify_application_response(fx.ALD_RECEIPT_SUBJECT, "", None).classification == (
        R.RECEIPT_CONFIRMATION
    )


def test_real_ald_receipt_is_not_read_as_under_review():
    # "Sobald wir Ihre Bewerbungsunterlagen eingehend geprüft haben" is future tense.
    result = classify_application_response(fx.ALD_RECEIPT_SUBJECT, fx.ALD_RECEIPT_TEXT, None)
    assert result.classification != R.UNDER_REVIEW_NOTICE


def test_html_only_mime_message_yields_classifiable_text():
    message = email.message_from_bytes(fx.ald_receipt_mime())
    text = imap_module._extract_html_text(message)
    assert "deren Eingang wir hiermit bestätigen" in text
    assert "<p>" not in text and "margin" not in text
    body_plain, _truncated, has_html, _att = imap_module._extract_content(message)
    assert body_plain == "" and has_html is True  # Stage 7A meaning unchanged
    assert (
        classify_application_response(
            fx.ALD_RECEIPT_SUBJECT, fx.ALD_RECEIPT_HTML, None, has_html=True
        ).classification
        == R.RECEIPT_CONFIRMATION
    )


def test_stage7b_classifier_also_recognises_the_receipt_wording():
    assert classify_email("", fx.ALD_RECEIPT_TEXT, fx.ALD_RECEIPT_FROM).category == (
        "APPLICATION_RECEIVED"
    )


# --- classifier edge cases ----------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        (
            "",
            "VIELEN DANK FÜR IHRE BEWERBUNG. DEREN EINGANG WIR HIERMIT BESTÄTIGEN.",
            R.RECEIPT_CONFIRMATION,
        ),
        ("", "deren Eingang wir hiermit bestaetigen", R.RECEIPT_CONFIRMATION),
        ("", "Wir haben Ihre Bewerbung erhalten.", R.RECEIPT_CONFIRMATION),
        ("Application received", "Thank you for your application.", R.RECEIPT_CONFIRMATION),
        (
            "",
            "Gerne möchten wir Sie zu einem Vorstellungsgespräch einladen.",
            R.INTERVIEW_INVITATION,
        ),
        (
            "Einladung zum Vorstellungsgespräch",
            "Bitte bestätigen Sie den Termin.",
            R.INTERVIEW_INVITATION,
        ),
        (
            "",
            "Wir möchten Sie zu einem Interview einladen. Es handelt sich um ein technisches "
            "Interview mit Live-Coding.",
            R.TECHNICAL_INTERVIEW_INVITATION,
        ),
        ("Einladung zum technischen Interview", "", R.TECHNICAL_INTERVIEW_INVITATION),
        (
            "",
            "Leider müssen wir Ihre Bewerbung absagen, wir haben uns für einen anderen "
            "Kandidaten entschieden.",
            R.REJECTION,
        ),
        ("", "We regret to inform you that we will not proceed.", R.REJECTION),
        ("", "Wir freuen uns, Ihnen die Stelle anzubieten. Anbei Ihr Vertragsangebot.", R.OFFER),
        ("", "Ihre Bewerbung befindet sich derzeit in der Prüfung.", R.UNDER_REVIEW_NOTICE),
        ("", "Bitte senden Sie uns noch Ihre Zeugnisse.", R.REQUEST_FOR_INFORMATION),
        ("Newsletter Oktober", "Unsere neuesten Produkte im Überblick.", R.UNKNOWN),
        ("", "", R.UNKNOWN),
    ],
)
def test_classifier_cases(subject, body, expected):
    assert classify_application_response(subject, body, None).classification == expected


def test_conflicting_rejection_and_invitation_never_classifies_terminal():
    result = classify_application_response(
        "",
        "Leider müssen wir Ihre Bewerbung absagen. Wir möchten Sie zu einem Gespräch einladen.",
        None,
    )
    assert result.classification == R.OTHER_APPLICATION_MESSAGE


def test_negated_rejection_is_not_a_rejection():
    result = classify_application_response("", "Dies ist keine Absage.", None)
    assert result.classification != R.REJECTION


def test_quoted_reply_history_does_not_classify_the_new_message():
    body = (
        "Danke, wir melden uns.\n\nAm 17.10.2026 um 16:00 schrieb Bewerber <a@b.invalid>:\n"
        "> Leider müssen wir Ihre Bewerbung absagen\nEinladung zum Vorstellungsgespräch"
    )
    assert normalize_body(body) == "Danke, wir melden uns."
    assert classify_application_response("Re: Frage", body, None).classification == R.UNKNOWN
    outlook = "Kurze Info.\n-----Original Message-----\nWir freuen uns, Ihnen die Stelle anzubieten"
    assert classify_application_response("", outlook, None).classification == R.UNKNOWN


def test_forward_and_reply_prefixes_are_removed_from_subjects():
    assert normalize_subject("WG: AW: Re: Ihre Bewerbung - Eingangsbestätigung") == (
        fx.ALD_RECEIPT_SUBJECT
    )
    assert normalize_subject(None) == ""


# --- K. correlation -----------------------------------------------------------------


def test_ald_receipt_correlates_to_the_ald_application(db):
    ald = _app(db)
    result = correlate_message(_msg(), [ald], applicant_addresses=(APPLICANT,))
    assert result.status == "MATCHED" and result.application_id == ald.id
    assert result.confidence == "MEDIUM" and result.signals == ("DOMAIN", "COMPANY")


def test_reference_or_role_raises_correlation_to_high(db):
    ald = _app(db)
    with_ref = _msg(body=fx.ALD_RECEIPT_TEXT + "\nReferenznummer 20385-2347902-S")
    assert correlate_message(with_ref, [ald]).confidence == "HIGH"
    with_role = _msg(subject="Ihre Bewerbung als Backend Developer (m/w/d)")
    assert correlate_message(with_role, [ald]).signals == ("DOMAIN", "COMPANY", "ROLE")
    labelled = _msg(body="Stellen-ID: 962. Vielen Dank für Ihre Bewerbung.")
    assert correlate_message(labelled, [ald]).signals == ("DOMAIN", "COMPANY", "REFERENCE")
    bare = _msg(body="Wir sind seit 962 Tagen ... Vielen Dank für Ihre Bewerbung.")
    assert "REFERENCE" not in correlate_message(bare, [ald]).signals


def test_same_employer_two_applications_needs_the_role_to_decide(db):
    backend = _app(db)
    data = _app(db, role="Data Engineer (m/w/d)", ref="20385-0000001-S", external="963")
    generic = correlate_message(_msg(), [backend, data])
    assert generic.status == "AMBIGUOUS" and generic.application_id is None
    specific = correlate_message(_msg(subject="Ihre Bewerbung als Data Engineer"), [backend, data])
    assert specific.status == "MATCHED" and specific.application_id == data.id


def test_same_title_at_different_companies_is_decided_by_sender(db):
    ald = _app(db)
    other = _app(
        db,
        company="Beispiel Software AG",
        ref="10000-1111111-S",
        domains=("beispiel.example",),
        external=None,
    )
    message = _msg(
        subject="Ihre Bewerbung als Backend Developer",
        sender="jobs@beispiel.example",
        display="Beispiel Software AG",
        body="Vielen Dank für Ihre Bewerbung bei Beispiel Software AG.",
    )
    result = correlate_message(message, [ald, other])
    assert result.status == "MATCHED" and result.application_id == other.id


def test_role_only_or_company_only_is_low_and_never_recorded(db):
    ald = _app(db)
    stranger = _msg(
        sender="info@jobportal.example",
        display="Jobportal",
        body="Neue Jobs: Backend Developer bei vielen Firmen.",
    )
    result = correlate_message(stranger, [ald])
    assert result.confidence in ("LOW", "NONE")
    assert decide_response(
        S.SUBMITTED,
        R.RECEIPT_CONFIRMATION,
        correlation_confidence="LOW",
        classifier_confidence="HIGH",
    ).action == ("IGNORE")


def test_own_and_outbound_messages_are_never_correlated(db):
    ald = _app(db)
    assert correlate_message(
        _msg(sender=APPLICANT), [ald], applicant_addresses=(APPLICANT,)
    ).reason == ("OWN_MESSAGE")
    assert correlate_message(_msg(direction="OUTBOUND"), [ald]).reason == "OUTBOUND_MESSAGE"


def test_message_outside_the_response_window_is_unmatched(db):
    ald = _app(db)
    before = _msg(at=fx.ALD_SUBMITTED_AT - timedelta(days=2))
    assert correlate_message(before, [ald]).status == "UNMATCHED"
    later = _msg(at=fx.ALD_SUBMITTED_AT + timedelta(days=400))
    assert correlate_message(later, [ald]).status == "UNMATCHED"


def test_missing_or_malformed_timestamp_never_correlates(db):
    ald = _app(db)
    assert correlate_message(_msg(at=None), [ald]).reason == "NO_TRUSTED_TIMESTAMP"
    assert normalize_occurred_at(None, "not-a-date") is None


def test_timezone_normalization_to_utc():
    from datetime import timezone

    berlin = datetime(2026, 10, 17, 16, 2, 50, tzinfo=timezone(timedelta(hours=2)))
    assert normalize_occurred_at(berlin) == fx.ALD_SUBMITTED_AT
    assert normalize_occurred_at(datetime(2026, 10, 17, 14, 2, 50)).tzinfo == UTC


def test_thread_reply_supports_correlation(db):
    ald = _app(db)
    _process(db, _msg())  # records the receipt with its message_id_hash
    reply = _msg(
        subject="AW: Termin",
        body="Bitte senden Sie uns Ihre Zeugnisse.",
        display="Recruiting",
        key="mid:reply",
        message_id="<reply@ald-vt.de>",
        replies=("<ald-receipt@ald-vt.de>",),
        at=fx.ALD_RECEIPT_ARRIVAL + timedelta(days=2),
    )
    from app.services.application_response_processor import _thread_hashes

    result = correlate_message(
        reply,
        [ApplicationTracker().get_application(db, ald.id)],
        thread_hashes_by_application=_thread_hashes(db, reply),
    )
    assert result.signals == ("DOMAIN", "THREAD") and result.confidence == "MEDIUM"


def test_message_keys_are_deterministic_and_fall_back_to_imap_identity():
    assert message_key_for("<A@B>", "INBOX", 1, 2) == message_key_for(" <a@b> ", "x", 9, 9)
    assert message_key_for(None, "INBOX", 7, 42) == "imap:INBOX:7:42"
    assert _msg().correlation_key == _msg().correlation_key
    assert _msg().correlation_key != _msg(key="mid:other").correlation_key


# --- L/M/I/V/U. processor -----------------------------------------------------------


def test_ald_receipt_moves_submitted_to_received_exactly_once(db):
    ald = _app(db)
    first = _process(db, _msg())[0]
    assert (first.previous_status, first.new_status) == ("SUBMITTED", "RECEIVED")
    assert first.transition_applied and first.event_recorded and not first.duplicate
    assert first.classification == "RECEIPT_CONFIRMATION"
    for _ in range(2):
        again = _process(db, _msg())[0]
        assert again.duplicate and not again.transition_applied and not again.event_recorded
        assert again.new_status == "RECEIVED"
    history = ApplicationTracker().get_application_history(db, ald.id)
    assert [(e.event_type, e.new_status) for e in history] == [
        (T.CREATED, S.SUBMITTED),
        (T.STATUS_CHANGED, S.RECEIVED),
    ]
    receipt = history[1]
    assert receipt.origin == O.GMAIL and receipt.correlation_key == _msg().correlation_key
    assert receipt.occurred_at == fx.ALD_RECEIPT_ARRIVAL
    assert "Mustermann" not in (receipt.evidence_summary or "")


def test_dry_run_writes_nothing(tmp_path):
    db_path = tmp_path / "dry.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    _app(session)
    session.close()
    engine.dispose()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    engine = create_engine(f"sqlite:///{db_path}")
    session = sessionmaker(engine)()
    result = _process(session, _msg(), apply=False)[0]
    assert result.dry_run and result.new_status == "RECEIVED" and not result.event_recorded
    assert not session.new and not session.dirty and not session.deleted
    session.close()
    engine.dispose()
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_unrelated_message_does_nothing(db):
    ald = _app(db)
    news = _msg(
        subject="Ihr Paket ist unterwegs",
        sender="versand@shop.example",
        display="Shop",
        body="Ihre Bestellung wurde versandt.",
        key="mid:shop",
    )
    result = _process(db, news)[0]
    assert result.correlation_status == "UNMATCHED" and result.application_id is None
    assert _events(db) == 1
    assert ApplicationTracker().get_application(db, ald.id).current_status == S.SUBMITTED


def test_ambiguous_messages_never_change_status(db):
    ald = _app(db)
    vague = _msg(
        subject="Info",
        body="Hallo, kurze Rückmeldung von ALD Vacuum Technologies GmbH.",
        key="mid:vague",
    )
    result = _process(db, vague)[0]
    assert result.classification == "UNKNOWN" and not result.transition_applied
    assert result.event_recorded  # visible in history, status untouched
    assert ApplicationTracker().get_application(db, ald.id).current_status == S.SUBMITTED
    second = _app(db, role="Data Engineer (m/w/d)", ref="20385-0000001-S", external="963")
    tie = _process(db, _msg(key="mid:tie"))[0]
    assert tie.correlation_status == "AMBIGUOUS" and not tie.event_recorded
    assert ApplicationTracker().get_application(db, second.id).current_status == S.SUBMITTED


def test_rejection_needs_high_correlation(db):
    ald = _app(db)
    rejection = "Leider müssen wir Ihre Bewerbung absagen."
    weak = _process(
        db,
        _msg(
            subject="Ihre Bewerbung",
            body=rejection + " ALD Vacuum Technologies GmbH",
            key="mid:rej-weak",
        ),
    )[0]
    assert (
        weak.classification == "REJECTION" and weak.reason == "INSUFFICIENT_EVIDENCE_FOR_TRANSITION"
    )
    assert ApplicationTracker().get_application(db, ald.id).current_status == S.SUBMITTED
    strong = _process(
        db,
        _msg(subject="Ihre Bewerbung als Backend Developer", body=rejection, key="mid:rej-strong"),
    )[0]
    assert strong.transition_applied and strong.new_status == "REJECTED"
    late_invite = _process(
        db,
        _msg(
            subject="Einladung zum Vorstellungsgespräch Backend Developer", body="", key="mid:late"
        ),
    )[0]
    assert late_invite.reason == "APPLICATION_TERMINAL" and not late_invite.transition_applied


def test_interview_and_technical_interview_invitations(db):
    ald = _app(db)
    _process(db, _msg())
    invite = _process(
        db,
        _msg(
            subject="Einladung zum Vorstellungsgespräch",
            body="ALD Vacuum Technologies GmbH",
            key="mid:inv",
        ),
    )[0]
    assert (invite.previous_status, invite.new_status) == ("RECEIVED", "INTERVIEW")
    tech = _process(
        db,
        _msg(
            subject="Einladung zum technischen Interview",
            body="ALD Vacuum Technologies GmbH",
            key="mid:tech",
        ),
    )[0]
    assert tech.new_status == "TECHNICAL_INTERVIEW" and tech.transition_applied
    stale_receipt = _process(db, _msg(key="mid:receipt-copy"))[0]
    assert stale_receipt.reason == "NOT_ALLOWED_FROM_CURRENT_STATUS"
    assert ApplicationTracker().get_application(db, ald.id).current_status == S.TECHNICAL_INTERVIEW


def test_offer_email(db):
    ald = _app(db)
    offer = _process(
        db,
        _msg(
            subject="Ihr Vertragsangebot Backend Developer",
            body="Wir freuen uns, Ihnen die Stelle anzubieten.",
            key="mid:offer",
        ),
    )[0]
    assert offer.classification == "OFFER" and offer.new_status == "OFFER"
    assert ApplicationTracker().get_application(db, ald.id).current_status == S.OFFER


def test_request_for_information_sets_followup_without_status_change(db):
    ald = _app(db)
    result = _process(
        db,
        _msg(
            subject="Unterlagen",
            body="Bitte senden Sie uns Ihre Zeugnisse. ALD Vacuum Technologies GmbH",
            key="mid:rfi",
        ),
    )[0]
    assert result.classification == "REQUEST_FOR_INFORMATION" and result.event_recorded
    app = ApplicationTracker().get_application(db, ald.id)
    assert app.current_status == S.SUBMITTED and app.followup_required is True


def test_empty_subject_and_missing_reference_still_work(db):
    _app(db)
    result = _process(db, _msg(subject="", key="mid:nosubj"))[0]
    assert result.transition_applied and result.new_status == "RECEIVED"


def test_only_application_ids_limits_the_blast_radius(db):
    ald = _app(db)
    other = _app(
        db,
        company="Beispiel Software AG",
        ref="10000-1111111-S",
        domains=("beispiel.example",),
        external=None,
    )
    msg = _msg(
        sender="jobs@beispiel.example",
        display="Beispiel Software AG",
        body="Vielen Dank für Ihre Bewerbung bei Beispiel Software AG.",
        key="mid:b",
    )
    assert _process(db, msg, only_application_ids=frozenset({ald.id}))[0].application_id is None
    assert ApplicationTracker().get_application(db, other.id).current_status == S.SUBMITTED


def test_results_never_carry_bodies_or_addresses(db):
    _app(db)
    payload = repr(_process(db, _msg(), apply=False)[0].as_dict())
    for secret in ("Mustermann", fx.ALD_RECEIPT_FROM, APPLICANT, "ald-receipt@"):
        assert secret not in payload


def test_inbound_adapters_use_trusted_arrival_and_html_text():
    from app.providers.email.base import ParsedGmailMessage

    parsed = ParsedGmailMessage(
        account_key="acc",
        mailbox="INBOX",
        uid=5,
        uid_validity=1,
        message_id_header=fx.ALD_RECEIPT_MESSAGE_ID,
        in_reply_to=None,
        references=(),
        from_address=fx.ALD_RECEIPT_FROM,
        from_display_name=fx.ALD_RECEIPT_DISPLAY_NAME,
        to_addresses=(APPLICANT,),
        cc_addresses=(),
        subject=fx.ALD_RECEIPT_SUBJECT,
        sent_at=fx.ALD_RECEIPT_DATE_HEADER,
        direction="INBOUND",
        body_plain="",
        body_truncated=False,
        has_html=True,
        provider_arrival_at=fx.ALD_RECEIPT_ARRIVAL,
        body_html_text=fx.ALD_RECEIPT_TEXT,
    )
    message = inbound_from_parsed(parsed)
    assert message.occurred_at == fx.ALD_RECEIPT_ARRIVAL and "Eingang" in message.body
    assert message.message_key == "mid:stage15-fixture-ald-receipt@ald-vt.de"
    record = GmailMessageRecord(
        account_key="acc",
        mailbox="INBOX",
        uid=5,
        uid_validity=1,
        message_id_header=None,
        references_json="[]",
        to_addresses_json="[]",
        subject="s",
        body_plain="b",
        has_html=False,
        direction="INBOUND",
        sent_at=fx.ALD_RECEIPT_DATE_HEADER,
        provider_arrival_at=fx.ALD_RECEIPT_ARRIVAL,
        provider_arrival_is_trusted=False,
    )
    stored = inbound_from_record(record)
    assert stored.message_key == "imap:INBOX:1:5"
    assert stored.occurred_at == fx.ALD_RECEIPT_DATE_HEADER  # untrusted arrival is skipped


def test_notification_text_is_rendered_locally_only():
    text = render_lifecycle_notification(
        company="ALD",
        role="Backend Developer",
        previous=S.SUBMITTED,
        new=S.RECEIVED,
        needs_review=True,
    )
    assert text.startswith("Bewerbung bei ALD bestätigt")
    assert (
        render_lifecycle_notification(
            company="A", role="r", previous=S.RECEIVED, new=S.RECEIVED, needs_review=False
        )
        is None
    )


# --- CLI / bootstrap / pilot ----------------------------------------------------------


def _migrated_db(tmp_path) -> Path:
    path = tmp_path / "tracker.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    upgrade(cfg, "head")
    return path


def _store_receipt(path: Path) -> None:
    from app.db.gmail_repository import upsert_message
    from app.providers.email.base import ParsedGmailMessage

    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    upsert_message(
        session,
        ParsedGmailMessage(
            account_key=APPLICANT,
            mailbox="INBOX",
            uid=77,
            uid_validity=3,
            message_id_header=fx.ALD_RECEIPT_MESSAGE_ID,
            in_reply_to=None,
            references=(),
            from_address=fx.ALD_RECEIPT_FROM,
            from_display_name=fx.ALD_RECEIPT_DISPLAY_NAME,
            to_addresses=(APPLICANT,),
            cc_addresses=(),
            subject=fx.ALD_RECEIPT_SUBJECT,
            sent_at=fx.ALD_RECEIPT_DATE_HEADER,
            direction="INBOUND",
            body_plain=fx.ALD_RECEIPT_TEXT,
            body_truncated=False,
            has_html=False,
            provider_arrival_at=fx.ALD_RECEIPT_ARRIVAL,
        ),
    )
    session.commit()
    session.close()
    engine.dispose()


def _settings():
    return Settings(_env_file=None, gmail_username=APPLICANT, outbound_sending_enabled=False)


def test_ald_pilot_bootstrap_end_to_end_and_idempotent(tmp_path, capsys):
    path = _migrated_db(tmp_path)
    _store_receipt(path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "SUBMISSION_RECORD.txt").write_text(
        'Result page title: "Ihre Bewerbung ist auf dem Weg zu uns!"\n'
        "POST https://karriere.ald-vt.com/Backend-Developer-mwdiv-de-f962.html => 200\n",
        encoding="utf-8",
    )
    args = ["--database", str(path), "--receipt-source", "stored"]
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert bootstrap.run(args, settings=_settings()) == 0  # dry run
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    for _ in range(2):
        assert (
            bootstrap.run(
                args + ["--submission-evidence", str(evidence), "--apply"], settings=_settings()
            )
            == 0
        )
    out = capsys.readouterr().out
    assert "final status: RECEIVED; events: 2" in out
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    app = session.scalars(select(ApplicationRecord)).one()
    assert (app.company_name, app.role_title, app.source_reference, app.current_status) == (
        "ALD Vacuum Technologies GmbH",
        "Backend Developer (m/w/div.)",
        "20385-2347902-S",
        "RECEIVED",
    )
    history = ApplicationTracker().get_application_history(session, app.id)
    assert [(e.new_status, e.occurred_at) for e in history] == [
        (S.SUBMITTED, fx.ALD_SUBMITTED_AT),
        (S.RECEIVED, fx.ALD_RECEIPT_ARRIVAL),
    ]
    assert history[0].metadata["submission_evidence_record_sha256"]
    session.close()
    engine.dispose()
    assert (
        cli.run(
            [
                "--database",
                str(path),
                "process-gmail",
                "--source",
                "stored",
                "--since",
                "2026-10-17",
                "--apply",
            ],
            settings=_settings(),
        )
        == 0
    )
    assert "ALREADY_PROCESSED" in capsys.readouterr().out
    engine = create_engine(f"sqlite:///{path}")
    assert (
        sessionmaker(engine)().scalar(select(func.count()).select_from(ApplicationEventRecord)) == 2
    )
    engine.dispose()


def test_cli_read_commands_and_dry_run_transition(tmp_path, capsys):
    path = _migrated_db(tmp_path)
    assert bootstrap.run(["--database", str(path), "--receipt-source", "none", "--apply"]) == 0
    assert cli.run(["--database", str(path), "list"]) == 0
    assert "company=ALD Vacuum Technologies GmbH" in capsys.readouterr().out
    assert cli.run(["--database", str(path), "--json", "show", "1"]) == 0
    assert '"status": "SUBMITTED"' in capsys.readouterr().out
    assert cli.run(["--database", str(path), "history", "1"]) == 0
    assert "type=CREATED" in capsys.readouterr().out
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert (
        cli.run(
            [
                "--database",
                str(path),
                "transition",
                "1",
                "WITHDRAWN",
                "--actor",
                "op",
                "--reason",
                "test",
            ]
        )
        == 0
    )
    assert "DRY RUN" in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert (
        cli.run(
            [
                "--database",
                str(path),
                "transition",
                "1",
                "DISCOVERED",
                "--actor",
                "op",
                "--reason",
                "x",
                "--apply",
            ]
        )
        == 1
    )
    assert (
        cli.run(
            [
                "--database",
                str(path),
                "override",
                "1",
                "WITHDRAWN",
                "--actor",
                "op",
                "--reason",
                "operator correction",
                "--apply",
            ]
        )
        == 0
    )
    assert "APPLIED" in capsys.readouterr().out
    assert cli.run(["--database", str(path), "show", "99"]) == 1


def test_cli_refuses_unsafe_targets(tmp_path, monkeypatch, capsys):
    path = _migrated_db(tmp_path)
    monkeypatch.setattr(
        cli, "PROTECTED_DATABASE_SHA256", frozenset({hashlib.sha256(path.read_bytes()).hexdigest()})
    )
    assert cli.run(["--database", str(path), "list"]) == 2
    assert "protected" in capsys.readouterr().err
    monkeypatch.undo()
    stage13d = tmp_path / "stage13d_secure" / "x.db"
    stage13d.parent.mkdir()
    stage13d.write_bytes(path.read_bytes())
    assert cli.run(["--database", str(stage13d), "list"]) == 2
    behind = tmp_path / "behind.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{behind}")
    upgrade(cfg, "a7c3e9f1b2d4")
    assert cli.run(["--database", str(behind), "list"]) == 2
    assert "upgrade it explicitly" in capsys.readouterr().err
    assert cli.run(["--database", str(tmp_path / "missing.db"), "list"]) == 2
    assert cli.run(["--database-url", "sqlite:///x.db", "list"]) == 2
    assert cli.run(["--database-url", "postgresql+psycopg://u:p@ai_agent-db-1/x", "list"]) == 2


def test_canonical_stage14b_database_is_protected_by_digest():
    assert (
        "874d2cab6fc78efe3574dd1fb123c06e60662da48c32c240227313778f41e35d"
        in cli.PROTECTED_DATABASE_SHA256
    )


# --- API (read-only) ----------------------------------------------------------------


@pytest.fixture()
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = override
    settings = Settings(api_key="k", rate_limit_requests=1000, rate_limit_window_seconds=60)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)
    rate_limit_module._requests.clear()
    with TestClient(fastapi_app) as client:
        yield client, factory
    fastapi_app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


def test_application_api_is_read_only_and_authenticated(api):
    client, factory = api
    session = factory()
    ald = _app(session)
    _process(session, _msg())
    session.close()
    assert client.get("/api/v1/applications").status_code == 401
    headers = {"X-API-Key": "k"}
    listing = client.get("/api/v1/applications", headers=headers).json()
    assert [a["current_status"] for a in listing] == ["RECEIVED"]
    assert client.get("/api/v1/applications?status=REJECTED", headers=headers).json() == []
    detail = client.get(f"/api/v1/applications/{ald.id}", headers=headers).json()
    assert detail["source_reference"] == "20385-2347902-S"
    history = client.get(f"/api/v1/applications/{ald.id}/history", headers=headers).json()
    assert [e["new_status"] for e in history] == ["SUBMITTED", "RECEIVED"]
    assert client.get("/api/v1/applications/999", headers=headers).status_code == 404
    for method in ("post", "put", "patch", "delete"):
        assert (
            getattr(client, method)(f"/api/v1/applications/{ald.id}", headers=headers).status_code
            == 405
        )


# --- W/X/Y/Z. no outbound -------------------------------------------------------------

STAGE15_MODULES = [
    "app/models/application_lifecycle.py",
    "app/domain/application_lifecycle.py",
    "app/domain/application_response_policy.py",
    "app/db/application_repository.py",
    "app/services/application_tracker.py",
    "app/services/application_correlation.py",
    "app/services/application_response_processor.py",
    "app/services/application_notifications.py",
    "app/agents/application_response_classifier.py",
    "app/utils/html_text.py",
    "scripts/stage15_application_tracker.py",
    "scripts/stage15_import_ald_application.py",
]
FORBIDDEN_IMPORTS = (
    "smtplib",
    "httpx",
    "requests",
    "urllib.request",
    "aiohttp",
    "telegram",
    "app.providers.email.smtp",
    "app.providers.email.imap_draft",
    "app.services.response_draft_send",
    "app.services.follow_up_send",
    "app.services.gmail_application_draft",
    "app.services.telegram",
    "app.services.telegram_bot",
)


@pytest.mark.parametrize("module", STAGE15_MODULES)
def test_stage15_modules_import_no_outbound_capability(module):
    tree = ast.parse((PROJECT_ROOT / module).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.startswith(FORBIDDEN_IMPORTS)}
    source = (PROJECT_ROOT / module).read_text(encoding="utf-8")
    for call in ("send_message", "sendmail", "send_mail", "create_draft", ".append(", ".post("):
        if call == ".append(":
            continue  # list.append is fine; IMAP APPEND is excluded via imports above
        assert call not in source


def test_full_pipeline_makes_no_network_smtp_draft_or_telegram_call(tmp_path, monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args[:1])
        raise AssertionError("outbound attempt")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    import app.services.gmail_application_draft as draft_module
    import app.services.telegram as telegram_module

    monkeypatch.setattr(draft_module, "render_draft_content", refuse, raising=False)
    for name in dir(telegram_module):
        if name.startswith("send"):
            monkeypatch.setattr(telegram_module, name, refuse)
    path = _migrated_db(tmp_path)
    _store_receipt(path)
    assert (
        bootstrap.run(
            ["--database", str(path), "--receipt-source", "stored", "--apply"], settings=_settings()
        )
        == 0
    )
    assert (
        cli.run(
            [
                "--database",
                str(path),
                "process-gmail",
                "--source",
                "stored",
                "--since",
                "2026-10-17",
            ],
            settings=_settings(),
        )
        == 0
    )
    assert calls == []
