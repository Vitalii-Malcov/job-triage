"""Night audit 2026-10-27: regressions from live operation (Stage 15 mail).

Anonymized from real messages (company/person/address tokens replaced):

1. A portal rejection ("... da wir uns anderweitig entschieden haben")
   was classified GENERAL_RECRUITER_SIGNAL and correlated LOW (company
   only), so it was IGNOREd. The rejection came from the same
   per-application reply address as the receipt already correlated to the
   application.
2. A JOIN receipt in the informal "Du" form ("vielen Dank für Deine
   Bewerbung als ...") was classified AUTOMATED_SIGNAL -> RECORD_ONLY
   instead of RECEIPT_CONFIRMATION -> RECEIVED.

Invariants kept: a rejection is never applied by e-mail (operator
confirms), a LOW/ambiguous correlation never writes, and a rejection is
never attributed to another vacancy of the same employer on company alone.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.agents.application_response_classifier import classify_application_response
from app.db.base import Base
from app.db.models import ApplicationEventRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.application_lifecycle import ResponseClassification as R
from app.services.application_correlation import InboundMessage
from app.services.application_response_processor import (
    NEEDS_OPERATOR_REVIEW,
    ApplicationResponseProcessor,
)
from app.services.application_tracker import ApplicationTracker

APPLICANT = "applicant@example.invalid"
SUBMITTED_AT = datetime(2026, 10, 26, 10, 12, 39, tzinfo=UTC)

PORTAL_SENDER = "examplecorp+applicantreply-token123@on-apply.example"
PORTAL_RECEIPT_SUBJECT = "ExampleCorp GmbH - Ihre Bewerbung"
PORTAL_RECEIPT_BODY = (
    "Hallo Max Muster,\n"
    "vielen Dank für Ihre Bewerbung auf die Stelle Versuchsingenieur (m/w/d) für "
    "System- und Integrationstests bei ExampleCorp GmbH."
)
PORTAL_REJECTION_SUBJECT = "Ihre Bewerbung bei ExampleCorp"
PORTAL_REJECTION_BODY = (
    "Hallo Max Muster,\n"
    "für Ihre Bewerbung und für das Interesse, das Sie damit an unserem Unternehmen "
    "zeigen, danken wir Ihnen herzlich. Wir bedauern es sehr, Ihnen keine positive "
    "Nachricht auf Ihre Bewerbung geben zu können, da wir uns anderweitig entschieden "
    "haben. Es tut uns sehr leid, Ihnen keine andere Antwort geben zu können.\n"
    "Freundliche Grüße\nExampleCorp GmbH"
)

JOIN_SENDER = "no-reply@msg.join.example"
JOIN_SUBJECT = "Deine Bewerbung bei Sample Soft GmbH als Softwaretester:in (m/w/d)"
JOIN_BODY = (
    "Hallo Max,\n\n*vielen Dank für Deine Bewerbung als Softwaretester:in (m/w/d)! *\n\n"
    "Bitte habe Verständnis, dass wir nicht die Ressourcen haben, um alle Bewerbungen "
    "zu beantworten. Wenn Du 14 Tage nach Eingang Deiner Bewerbung keine Antwort "
    "erhalten hast, passte ein*e andere*r Bewerber*in besser zum Anforderungsprofil.\n"
    "Mit freundlichen Grüßen\nTeam Sample Soft\nSample Soft GmbH"
)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'night_audit.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _app(db, *, company, role, ref, domains=(), status=S.SUBMITTED):
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
            occurred_at=SUBMITTED_AT,
            employer_domains=domains,
            correlation_key=f"portal-submission:{ref}",
        )
        .application
    )


def _msg(subject, body, *, sender, key, at, display=None):
    return InboundMessage(
        account_key=APPLICANT,
        message_key=f"mid:{key}",
        message_id_header=f"<{key}>",
        reply_to_ids=(),
        from_address=sender,
        from_display_name=display,
        to_addresses=(APPLICANT,),
        subject=subject,
        body=body,
        has_html=False,
        occurred_at=at,
    )


def _process(db, messages, apply=True):
    return ApplicationResponseProcessor().process(
        db, messages, apply=apply, applicant_addresses=(APPLICANT,)
    )


def _receipt(at_offset=timedelta(seconds=1)):
    return _msg(
        PORTAL_RECEIPT_SUBJECT,
        PORTAL_RECEIPT_BODY,
        sender=PORTAL_SENDER,
        display="ExampleCorp",
        key="receipt-1@ses.example",
        at=SUBMITTED_AT + at_offset,
    )


def _rejection(sender=PORTAL_SENDER):
    return _msg(
        PORTAL_REJECTION_SUBJECT,
        PORTAL_REJECTION_BODY,
        sender=sender,
        display="ExampleCorp",
        key="rejection-1@ses.example",
        at=SUBMITTED_AT + timedelta(days=1, hours=5),
    )


def _events(db, application_id):
    return db.scalar(
        select(func.count())
        .select_from(ApplicationEventRecord)
        .where(ApplicationEventRecord.application_id == application_id)
    )


# --- classifier ---------------------------------------------------------


def test_portal_rejection_anderweitig_entschieden_is_rejection():
    result = classify_application_response(
        PORTAL_REJECTION_SUBJECT, PORTAL_REJECTION_BODY, PORTAL_SENDER
    )
    assert result.classification == R.REJECTION
    assert result.confidence == "HIGH"


def test_join_du_form_receipt_is_receipt_confirmation():
    result = classify_application_response(JOIN_SUBJECT, JOIN_BODY, JOIN_SENDER)
    assert result.classification == R.RECEIPT_CONFIRMATION
    assert result.confidence == "HIGH"


@pytest.mark.parametrize(
    "body",
    [
        "Leider müssen wir Dir mitteilen, dass wir uns für eine*n andere*n Bewerber*in "
        "entschieden haben.",
        "Wir haben uns für einen anderen Bewerber entschieden.",
        "Wir haben uns für andere Bewerber entschieden, deren Profil besser passt.",
        "Nach sorgfältiger Prüfung haben wir uns gegen Ihre Bewerbung entschieden.",
        "Leider können wir Ihnen keine positive Rückmeldung geben.",
        "Wir können Ihre Bewerbung leider nicht berücksichtigen.",
        "Leider müssen wir Ihnen mitteilen, dass wir Sie nicht in die engere Auswahl "
        "einbeziehen konnten.",
        "We have decided to pursue other candidates.",
    ],
)
def test_common_rejection_wordings(body):
    result = classify_application_response("Ihre Bewerbung", body, "jobs@corp.example")
    assert result.classification == R.REJECTION


@pytest.mark.parametrize(
    "body",
    [
        # Conditional "silence means rejection" note inside a receipt.
        JOIN_BODY,
        # Polite receipt that mentions other applicants.
        "Vielen Dank für Ihre Bewerbung. Wir prüfen alle Bewerbungen sorgfältig und "
        "melden uns, sobald wir uns entschieden haben.",
        # Negated decision wording.
        "Wir haben uns noch nicht entschieden und melden uns bis Ende der Woche.",
    ],
)
def test_receipts_and_pending_notices_are_not_rejections(body):
    result = classify_application_response("Ihre Bewerbung", body, "jobs@corp.example")
    assert result.classification != R.REJECTION


@pytest.mark.parametrize(
    "body",
    [
        "Hallo Max, danke für Deine Bewerbung! Wir melden uns bald.",
        "Deine Bewerbung ist bei uns eingegangen.",
        "Wir haben Deine Bewerbung erhalten und schauen sie uns an.",
        "Vielen Dank für deine Bewerbung als Junior Developer.",
    ],
)
def test_informal_receipts(body):
    result = classify_application_response("Deine Bewerbung", body, "no-reply@ats.example")
    assert result.classification == R.RECEIPT_CONFIRMATION


def test_platform_marketing_mail_stays_non_lifecycle():
    body = (
        "Neue Jobs für dich: 12 passende Stellen in Frankfurt. "
        "Jetzt Bewerbung mit einem Klick starten!"
    )
    result = classify_application_response("Deine Jobempfehlungen", body, "no-reply@jobs.example")
    assert result.classification == R.OTHER_APPLICATION_MESSAGE


# --- end to end: receipt -> RECEIVED (JOIN) ------------------------------


def test_join_receipt_moves_submitted_to_received(db):
    app = _app(
        db,
        company="Sample Soft GmbH",
        role="Softwaretester:in (m/w/d)",
        ref="14671967",
    )
    message = _msg(
        JOIN_SUBJECT,
        JOIN_BODY,
        sender=JOIN_SENDER,
        key="join-1@join.example",
        at=SUBMITTED_AT + timedelta(minutes=1),
    )
    [dry] = _process(db, [message], apply=False)
    assert (dry.action, dry.new_status, dry.dry_run) == ("TRANSITION", "RECEIVED", True)
    assert _events(db, app.id) == 1

    [result] = _process(db, [message])
    assert result.transition_applied and result.new_status == "RECEIVED"
    assert result.correlation_confidence == "MEDIUM"
    [again] = _process(db, [message])
    assert again.duplicate and again.reason == "ALREADY_PROCESSED"
    assert _events(db, app.id) == 2


# --- end to end: portal rejection after a correlated receipt -------------


def test_portal_rejection_after_receipt_needs_operator_confirmation(db):
    app = _app(
        db,
        company="ExampleCorp GmbH",
        role="Versuchsingenieur (m/w/d) für System- und Integrationstests",
        ref="10001-1003396871-S",
        domains=("examplecorp.example",),
    )
    [receipt] = _process(db, [_receipt()])
    assert receipt.transition_applied and receipt.new_status == "RECEIVED"

    [dry] = _process(db, [_rejection()], apply=False)
    assert dry.classification == "REJECTION"
    assert dry.application_id == app.id
    assert "SENDER" in dry.correlation_signals
    assert dry.correlation_confidence == "MEDIUM"
    # SENDER is evidence only (Codex round 3): the application is named for
    # the operator, nothing is written.
    assert dry.action == "IGNORE" and dry.reason == "SENDER_ONLY_CORRELATION_NEEDS_OPERATOR"
    assert dry.proposed_status == "REJECTED"
    assert dry.operator_review == NEEDS_OPERATOR_REVIEW

    [result] = _process(db, [_rejection()])
    assert not result.event_recorded and not result.transition_applied
    assert ApplicationTracker().get_application(db, app.id).current_status == S.RECEIVED
    assert _events(db, app.id) == 2


def test_rejection_from_unknown_sender_is_not_upgraded(db):
    """Without the previously-seen sender the same text stays LOW: nothing
    is written, but it is surfaced for operator review instead of silently
    ignored."""
    app = _app(
        db,
        company="ExampleCorp GmbH",
        role="Versuchsingenieur (m/w/d) für System- und Integrationstests",
        ref="10001-1003396871-S",
    )
    _process(db, [_receipt()])
    [result] = _process(db, [_rejection(sender="someone@elsewhere.example")])
    assert result.correlation_confidence == "LOW"
    assert result.action == "IGNORE" and not result.event_recorded
    assert result.classification == "REJECTION"
    assert result.operator_review == NEEDS_OPERATOR_REVIEW
    assert _events(db, app.id) == 2


def test_rejection_is_not_attributed_to_other_vacancy_of_same_employer(db):
    """Two applications at the same employer; the receipt (from a
    per-application-looking sender) belongs to the first. A role-less
    rejection from that sender is NOT attributed by the sender (an ATS may
    reuse the address): AMBIGUOUS, nothing written, operator review."""
    first = _app(
        db,
        company="ExampleCorp GmbH",
        role="Versuchsingenieur (m/w/d) für System- und Integrationstests",
        ref="10001-1003396871-S",
    )
    second = _app(
        db,
        company="ExampleCorp GmbH",
        role="Entwicklungsingenieur Embedded Software (m/w/d)",
        ref="10001-1009999999-S",
    )
    _process(db, [_receipt()])
    [result] = _process(db, [_rejection()])
    assert result.correlation_status == "AMBIGUOUS" and not result.event_recorded
    assert result.operator_review == NEEDS_OPERATOR_REVIEW
    assert _events(db, first.id) == 2 and _events(db, second.id) == 1


def test_shared_plus_address_never_upgrades_role_less_invitation(db):
    """Codex P1 scenario: one plus-token ATS address used for two
    applications at an employer; a role-less interview invitation must not
    move the first application to INTERVIEW."""
    first = _app(db, company="ExampleCorp GmbH", role="Tester (m/w/d)", ref="REF-000001")
    _app(db, company="ExampleCorp GmbH", role="Entwickler (m/w/d)", ref="REF-000002")
    receipt = _msg(
        "Ihre Bewerbung als Tester (m/w/d)",
        "Vielen Dank für Ihre Bewerbung als Tester (m/w/d) bei ExampleCorp GmbH.",
        sender=PORTAL_SENDER,
        key="r-1@ats.example",
        at=SUBMITTED_AT + timedelta(minutes=1),
    )
    _process(db, [receipt])
    invitation = _msg(
        "Einladung",
        "Gerne laden wir Sie zu einem Vorstellungsgespräch ein. ExampleCorp GmbH",
        sender=PORTAL_SENDER,
        key="i-1@ats.example",
        at=SUBMITTED_AT + timedelta(days=2),
    )
    [result] = _process(db, [invitation])
    assert result.correlation_status == "AMBIGUOUS" and not result.transition_applied
    assert ApplicationTracker().get_application(db, first.id).current_status == S.RECEIVED


def test_shared_employer_sender_never_decides_between_two_vacancies(db):
    """The employer's shared HR address sent the receipt for the first
    application. A role-less rejection from that address may concern either
    vacancy: it stays AMBIGUOUS and is only surfaced for review."""
    shared = "karriere@examplecorp.example"
    first = _app(
        db,
        company="ExampleCorp GmbH",
        role="Versuchsingenieur (m/w/d) für System- und Integrationstests",
        ref="10001-1003396871-S",
    )
    second = _app(
        db,
        company="ExampleCorp GmbH",
        role="Entwicklungsingenieur Embedded Software (m/w/d)",
        ref="10001-1009999999-S",
    )
    receipt = _msg(
        PORTAL_RECEIPT_SUBJECT,
        PORTAL_RECEIPT_BODY,
        sender=shared,
        key="receipt-shared@examplecorp.example",
        at=SUBMITTED_AT + timedelta(seconds=1),
    )
    [first_receipt] = _process(db, [receipt])
    assert first_receipt.application_id == first.id
    [result] = _process(db, [_rejection(sender=shared)])
    assert result.correlation_status == "AMBIGUOUS"
    assert not result.event_recorded
    assert result.operator_review == NEEDS_OPERATOR_REVIEW
    assert _events(db, first.id) == 2 and _events(db, second.id) == 1


def test_same_employer_rejection_without_distinguishing_signal_is_ambiguous(db):
    first = _app(db, company="ExampleCorp GmbH", role="Tester (m/w/d)", ref="REF-000001")
    second = _app(db, company="ExampleCorp GmbH", role="Entwickler (m/w/d)", ref="REF-000002")
    [result] = _process(db, [_rejection(sender="hr@unknown.example")])
    assert result.application_id is None
    assert result.correlation_status == "AMBIGUOUS"
    assert result.action == "IGNORE" and not result.event_recorded
    assert result.classification == "REJECTION"
    assert result.operator_review == NEEDS_OPERATOR_REVIEW
    assert _events(db, first.id) == 1 and _events(db, second.id) == 1


def test_shared_platform_sender_alone_does_not_correlate(db):
    """A generic platform sender seen on one employer's receipt must not pull
    another employer's mail to that application."""
    app = _app(db, company="Sample Soft GmbH", role="Softwaretester:in (m/w/d)", ref="14671967")
    receipt = _msg(
        JOIN_SUBJECT,
        JOIN_BODY,
        sender=JOIN_SENDER,
        key="join-1@join.example",
        at=SUBMITTED_AT + timedelta(minutes=1),
    )
    _process(db, [receipt])
    other = _msg(
        "Deine Bewerbung bei Other AG als Data Analyst (m/w/d)",
        "Hallo Max, vielen Dank für Deine Bewerbung als Data Analyst (m/w/d)!",
        sender=JOIN_SENDER,
        key="join-2@join.example",
        at=SUBMITTED_AT + timedelta(days=1),
    )
    [result] = _process(db, [other])
    assert result.application_id is None or result.correlation_confidence == "LOW"
    assert not result.event_recorded
    assert _events(db, app.id) == 2


def test_sender_hash_is_stored_not_address(db):
    import json

    app = _app(
        db,
        company="ExampleCorp GmbH",
        role="Versuchsingenieur (m/w/d) für System- und Integrationstests",
        ref="10001-1003396871-S",
    )
    _process(db, [_receipt()])
    event = db.scalars(
        select(ApplicationEventRecord)
        .where(ApplicationEventRecord.application_id == app.id)
        .order_by(ApplicationEventRecord.sequence.desc())
    ).first()
    metadata = json.loads(event.metadata_json)
    assert len(metadata["sender_address_hash"]) == 64
    assert PORTAL_SENDER not in event.metadata_json
    assert "on-apply" not in event.metadata_json


# --- Codex night-audit review -------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Wir können Ihnen derzeit keine positive Nachricht geben; die Entscheidung fällt "
        "nächste Woche.",
        "Leider können wir Ihnen noch keine Rückmeldung geben, wir melden uns.",
        "Leider können wir Dir derzeit noch nichts anbieten, melden uns aber.",
        "Wir können Ihnen derzeit leider keine positive Nachricht geben; die Entscheidung "
        "fällt nächste Woche.",
        "Leider haben wir momentan noch keine positive Rückmeldung für Sie.",
    ],
)
def test_pending_decisions_are_not_rejections(body):
    result = classify_application_response("Ihre Bewerbung", body, "hr@corp.example")
    assert result.classification != R.REJECTION


@pytest.mark.parametrize(
    "body",
    [
        "Vielen Dank für Deine Bewerbung. Leider können wir Dir die Stelle nicht anbieten.",
        "Danke für Ihre Bewerbung. Leider können wir Ihnen die Position nicht anbieten.",
        "Vielen Dank für Deine Bewerbung! Leider können wir Dir keine Zusage geben.",
    ],
)
def test_informal_rejection_beats_receipt_wording(body):
    result = classify_application_response("Deine Bewerbung", body, "jobs@corp.example")
    assert result.classification == R.REJECTION


def test_sender_never_decides_against_a_differently_named_branch(db):
    """Codex round 2: the second application is recorded as "... Standort
    Berlin"; a role-less invitation naming only "ExampleCorp GmbH" must not
    be pinned to the first application through the shared sender."""
    first = _app(db, company="ExampleCorp GmbH", role="Tester (m/w/d)", ref="REF-000001")
    _app(
        db,
        company="ExampleCorp GmbH Standort Berlin",
        role="Entwickler (m/w/d)",
        ref="REF-000002",
    )
    receipt = _msg(
        "Ihre Bewerbung als Tester (m/w/d)",
        "Vielen Dank für Ihre Bewerbung als Tester (m/w/d) bei ExampleCorp GmbH.",
        sender=PORTAL_SENDER,
        key="r-2@ats.example",
        at=SUBMITTED_AT + timedelta(minutes=1),
    )
    _process(db, [receipt])
    invitation = _msg(
        "Einladung",
        "Gerne laden wir Sie zu einem Vorstellungsgespräch ein. ExampleCorp GmbH",
        sender=PORTAL_SENDER,
        key="i-2@ats.example",
        at=SUBMITTED_AT + timedelta(days=2),
    )
    [result] = _process(db, [invitation])
    assert not result.transition_applied and "SENDER" not in result.correlation_signals
    assert ApplicationTracker().get_application(db, first.id).current_status == S.RECEIVED


def test_sender_only_never_transitions_even_under_an_alias(db):
    """Codex round 3: "IBM Deutschland GmbH" and "International Business
    Machines Deutschland GmbH" are one employer under two names; a shared
    ATS sender + "IBM Deutschland" must not move the first application to
    INTERVIEW."""
    first = _app(db, company="IBM Deutschland GmbH", role="Tester (m/w/d)", ref="REF-000011")
    second = _app(
        db,
        company="International Business Machines Deutschland GmbH",
        role="Entwickler (m/w/d)",
        ref="REF-000012",
    )
    receipt = _msg(
        "Ihre Bewerbung als Tester (m/w/d)",
        "Vielen Dank für Ihre Bewerbung als Tester (m/w/d) bei IBM Deutschland GmbH.",
        sender="jobs@ats.example",
        key="r-ibm@ats.example",
        at=SUBMITTED_AT + timedelta(minutes=1),
    )
    _process(db, [receipt])
    invitation = _msg(
        "Einladung",
        "Gerne laden wir Sie zu einem Vorstellungsgespräch ein. IBM Deutschland GmbH",
        sender="jobs@ats.example",
        key="i-ibm@ats.example",
        at=SUBMITTED_AT + timedelta(days=2),
    )
    [result] = _process(db, [invitation])
    assert not result.transition_applied and not result.event_recorded
    assert result.operator_review == NEEDS_OPERATOR_REVIEW
    assert ApplicationTracker().get_application(db, first.id).current_status == S.RECEIVED
    assert _events(db, second.id) == 1


def test_definitive_rejection_after_a_suppressed_phrase_in_one_sentence():
    """Codex round 6: "derzeit" suppresses the open-ended first phrase, but
    the later "anderweitig entschieden" in the same sentence is definitive."""
    body = (
        "Derzeit erhalten Sie keine positive Nachricht, da wir uns anderweitig entschieden haben."
    )
    result = classify_application_response("Ihre Bewerbung", body, "jobs@corp.example")
    assert result.classification == R.REJECTION


@pytest.mark.parametrize(
    "body",
    [
        "Es liegt keine Absage vor.",
        # Codex round 7: pending first phrase + explicit "keine Absage".
        "Wir können Ihnen derzeit keine positive Nachricht geben, es liegt keine Absage vor.",
    ],
)
def test_explicit_no_rejection_is_never_a_rejection(body):
    result = classify_application_response("Ihre Bewerbung", body, "jobs@corp.example")
    assert result.classification != R.REJECTION
