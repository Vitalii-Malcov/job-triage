"""Inbound application replies across the applicant's monitored mailboxes.

Applications have been sent from two of the applicant's own accounts: the
primary GMAIL_* mailbox and the XING_MAILBOX_* mailbox (which also receives
XING job alerts). These tests prove that replies in either mailbox are
tracked, that XING alerts and application replies never cross paths, and
that re-scans and the same Message-ID in two mailboxes stay idempotent.

Everything is synthetic; no test opens a network connection or sends mail.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.collectors.xing_email import XingEmailCollector
from app.core.config import Settings
from app.db.base import Base
from app.db.models import ApplicationEventRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.providers.email.base import GmailConnectionError, GmailFetchResult, ParsedGmailMessage
from app.services.application_correlation import InboundMessage, correlate_message
from app.services.application_response_processor import (
    ApplicationResponseProcessor,
    application_mailboxes,
    fetch_application_mailboxes,
    monitored_account_keys,
)
from app.services.application_tracker import ApplicationTracker
from tests import stage15_fixtures as fx
from tests.test_collectors_xing_email import FakeImapClient, _build_email

PRIMARY = "primary.applicant@example.invalid"
SECONDARY = "secondary.applicant@example.invalid"
SUBMITTED_AT = datetime(2026, 10, 20, 18, 58, 59, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        gmail_username=PRIMARY,
        gmail_app_password="primary-app-password",
        xing_mailbox_username=SECONDARY,
        xing_mailbox_app_password="secondary-app-password",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reply_mailboxes.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _application(db, *, company, role, ref, domains, submitted_at, channel=C.EMAIL):
    return (
        ApplicationTracker()
        .create_application(
            db,
            company_name=company,
            role_title=role,
            source="bundesagentur",
            source_reference=ref,
            application_channel=channel,
            initial_status=S.SUBMITTED,
            origin=O.OPERATOR,
            actor="test",
            occurred_at=submitted_at,
            employer_domains=domains,
            correlation_key=f"submission:{ref}",
        )
        .application
    )


def _hd24(db):
    return _application(
        db,
        company="Heizungsdiscount 24 GmbH",
        role="Softwareentwickler – Business Applications & Integrationen",
        ref="18088-k60163.1143-S",
        domains=("heizungsdiscount24.de",),
        submitted_at=SUBMITTED_AT,
    )


def _ald(db, role="Backend Developer (m/w/div.)", ref="20385-2347902-S"):
    return _application(
        db,
        company="ALD Vacuum Technologies GmbH",
        role=role,
        ref=ref,
        domains=("ald-vt.de", "ald-vt.com"),
        submitted_at=fx.ALD_SUBMITTED_AT,
        channel=C.EMPLOYER_PORTAL,
    )


def _msg(
    *,
    account,
    subject,
    body,
    sender,
    display="",
    message_id,
    at=SUBMITTED_AT + timedelta(hours=2),
):
    return InboundMessage(
        account_key=account,
        message_key="mid:" + message_id.strip("<>").casefold(),
        message_id_header=message_id,
        reply_to_ids=(),
        from_address=sender,
        from_display_name=display,
        to_addresses=(account,),
        subject=subject,
        body=body,
        has_html=False,
        occurred_at=at,
    )


def _process(db, messages, *, apply=True, settings=None):
    settings = settings or _settings()
    return ApplicationResponseProcessor().process(
        db,
        messages,
        apply=apply,
        applicant_addresses=(PRIMARY, SECONDARY),
        monitored_account_keys=monitored_account_keys(settings),
    )


def _events(db) -> int:
    return db.scalar(select(func.count()).select_from(ApplicationEventRecord))


HD24_RECEIPT = dict(
    subject="Eingangsbestätigung Ihrer Bewerbung als Softwareentwickler – Business "
    "Applications & Integrationen",
    body="Sehr geehrter Herr Muster,\n\nvielen Dank für Ihre Bewerbung. Hiermit bestätigen "
    "wir den Eingang Ihrer Unterlagen.\n\nMit freundlichen Grüßen\nHeizungsdiscount 24 GmbH",
    sender="bewerbung@heizungsdiscount24.de",
    display="Heizungsdiscount 24 GmbH",
)

XING_ALERT = dict(
    subject="Ein neues Stellenangebot für dich",
    body="Softwareentwickler – Business Applications & Integrationen\n"
    "Heizungsdiscount 24 GmbH\nGießen\n=> https://www.xing.com/m/notfollowed",
    sender="jobs@mail.xing.com",
    display="XING",
)


# --- scenarios 1-3, 8, 9: receipts and replies in either mailbox -----------------


def test_receipt_with_role_in_secondary_mailbox_moves_to_received(db):
    app = _hd24(db)
    result = _process(db, [_msg(account=SECONDARY, message_id="<r1@hd24>", **HD24_RECEIPT)])[0]
    assert result.application_id == app.id
    assert result.correlation_confidence == "HIGH"
    assert (result.previous_status, result.new_status) == ("SUBMITTED", "RECEIVED")
    assert result.transition_applied


def test_receipt_in_primary_mailbox_is_tracked_the_same_way(db):
    app = _hd24(db)
    result = _process(db, [_msg(account=PRIMARY, message_id="<r2@hd24>", **HD24_RECEIPT)])[0]
    assert result.application_id == app.id and result.transition_applied


def test_generic_ald_receipt_still_correlates_in_secondary_mailbox(db):
    ald = _ald(db)
    receipt = _msg(
        account=SECONDARY,
        subject=fx.ALD_RECEIPT_SUBJECT,
        body=fx.ALD_RECEIPT_TEXT,
        sender=fx.ALD_RECEIPT_FROM,
        display=fx.ALD_RECEIPT_DISPLAY_NAME,
        message_id="<ald@ald-vt.de>",
        at=fx.ALD_RECEIPT_ARRIVAL,
    )
    result = _process(db, [receipt])[0]
    assert result.application_id == ald.id and result.new_status == "RECEIVED"


def test_generic_receipt_for_two_same_employer_applications_stays_ambiguous(db):
    first = _ald(db)
    second = _ald(db, role="Software Engineer (m/w/div.)", ref="20385-2352935-S")
    receipt = _msg(
        account=SECONDARY,
        subject=fx.ALD_RECEIPT_SUBJECT,
        body=fx.ALD_RECEIPT_TEXT,
        sender=fx.ALD_RECEIPT_FROM,
        display=fx.ALD_RECEIPT_DISPLAY_NAME,
        message_id="<ald-generic@ald-vt.de>",
        at=fx.ALD_RECEIPT_ARRIVAL,
    )
    result = _process(db, [receipt])[0]
    assert result.correlation_status == "AMBIGUOUS" and not result.event_recorded
    tracker = ApplicationTracker()
    assert tracker.get_application(db, first.id).current_status == S.SUBMITTED
    assert tracker.get_application(db, second.id).current_status == S.SUBMITTED
    assert _events(db) == 2


def test_human_reply_is_recorded_without_a_status_change(db):
    app = _hd24(db)
    reply = _msg(
        account=SECONDARY,
        subject="Re: Bewerbung Softwareentwickler – Business Applications & Integrationen",
        body="Hallo Herr Muster,\n\nwann hätten Sie kommende Woche Zeit für ein kurzes "
        "Telefonat? Bitte senden Sie uns noch Ihre Zeugnisse.\n\nViele Grüße\nAnna",
        sender="anna.beispiel@heizungsdiscount24.de",
        display="Anna Beispiel",
        message_id="<human@hd24>",
    )
    result = _process(db, [reply])[0]
    assert result.application_id == app.id
    assert result.classification == "REQUEST_FOR_INFORMATION"
    assert result.action == "RECORD_ONLY" and result.event_recorded
    assert result.new_status == "SUBMITTED"
    assert ApplicationTracker().get_application(db, app.id).followup_required


# --- scenarios 4, 5: XING alerts and personal mail ---------------------------------


def test_xing_alert_naming_an_applied_role_is_never_an_employer_reply(db):
    app = _hd24(db)
    alert = _msg(account=SECONDARY, message_id="<alert@mail.xing.com>", **XING_ALERT)
    result = _process(db, [alert])[0]
    assert result.correlation_status == "UNMATCHED"
    assert result.reason == "JOB_PLATFORM_NOTIFICATION"
    assert _events(db) == 1
    assert ApplicationTracker().get_application(db, app.id).current_status == S.SUBMITTED


def test_any_xing_notification_subdomain_sender_is_excluded(db):
    app = _hd24(db)
    for sender in ("jobs@mail.xing.com", "MAILROBOT@Mail.Xing.com", "news@x.mail.xing.com"):
        fields = {**XING_ALERT, "sender": sender}
        message = _msg(account=SECONDARY, message_id=f"<{sender}>", **fields)
        assert correlate_message(message, [app]).reason == "JOB_PLATFORM_NOTIFICATION"
    lookalike = {**XING_ALERT, "sender": "jobs@mail.xing.com.evil.example"}
    message = _msg(account=SECONDARY, message_id="<lookalike>", **lookalike)
    assert correlate_message(message, [app]).reason != "JOB_PLATFORM_NOTIFICATION"


def test_unrelated_personal_mail_changes_nothing(db):
    _hd24(db)
    personal = _msg(
        account=SECONDARY,
        subject="Abendessen am Samstag?",
        body="Hi, hast du am Samstag Zeit?",
        sender="friend@example.org",
        message_id="<friend@example.org>",
    )
    result = _process(db, [personal])[0]
    assert result.correlation_status == "UNMATCHED" and _events(db) == 1


def test_own_mail_from_either_account_is_never_an_employer_response(db):
    _hd24(db)
    for own in (PRIMARY, SECONDARY):
        sent = _msg(
            account=SECONDARY,
            message_id=f"<own-{own}>",
            **{**HD24_RECEIPT, "sender": own},
        )
        assert _process(db, [sent])[0].reason == "OWN_MESSAGE"
    assert _events(db) == 1


def test_employer_reply_in_xing_mailbox_never_becomes_a_xing_job():
    receipt = _build_email(
        HD24_RECEIPT["sender"],
        HD24_RECEIPT["subject"],
        HD24_RECEIPT["body"],
        message_id="<receipt@hd24>",
    )
    collector = XingEmailCollector(
        imap_host="imap.example.com",
        imap_port=993,
        username=SECONDARY,
        app_password="secondary-app-password",
        imap_client=FakeImapClient([receipt]),
    )
    assert asyncio.run(collector.fetch()) == []


# --- scenario 6: duplicates and re-scans --------------------------------------------


def test_rescan_of_the_same_message_is_a_duplicate(db):
    _hd24(db)
    message = _msg(account=SECONDARY, message_id="<dup@hd24>", **HD24_RECEIPT)
    first = _process(db, [message])[0]
    again = _process(db, [message])[0]
    assert first.event_recorded and again.duplicate and not again.event_recorded
    assert _events(db) == 2


def test_same_message_id_in_both_mailboxes_is_recorded_once(db):
    app = _hd24(db)
    in_primary = _msg(account=PRIMARY, message_id="<both@hd24>", **HD24_RECEIPT)
    in_secondary = _msg(account=SECONDARY, message_id="<both@hd24>", **HD24_RECEIPT)
    first = _process(db, [in_primary])[0]
    second = _process(db, [in_secondary])[0]
    assert first.transition_applied
    assert second.duplicate and not second.event_recorded
    history = ApplicationTracker().get_application_history(db, app.id)
    assert len(history) == 2


def test_without_monitored_accounts_cross_mailbox_copy_is_not_a_duplicate(db):
    """Control: the cross-account check is what makes the copy a duplicate."""
    _hd24(db)
    _process(db, [_msg(account=PRIMARY, message_id="<ctl@hd24>", **HD24_RECEIPT)])
    copy = _msg(account=SECONDARY, message_id="<ctl@hd24>", **HD24_RECEIPT)
    result = ApplicationResponseProcessor().process(
        db, [copy], apply=True, applicant_addresses=(PRIMARY, SECONDARY)
    )[0]
    assert not result.duplicate


def test_dry_run_in_secondary_mailbox_writes_nothing(db):
    _hd24(db)
    result = _process(
        db, [_msg(account=SECONDARY, message_id="<dry@hd24>", **HD24_RECEIPT)], apply=False
    )[0]
    assert result.dry_run and result.new_status == "RECEIVED" and not result.event_recorded
    assert _events(db) == 1


# --- mailbox configuration and fetch ------------------------------------------------


def test_mailbox_selection_uses_existing_credentials_only():
    labels = [m.label for m in application_mailboxes(_settings())]
    assert labels == ["primary", "secondary"]
    assert [m.label for m in application_mailboxes(_settings(), "primary")] == ["primary"]
    assert [m.label for m in application_mailboxes(_settings(), "secondary")] == ["secondary"]
    unconfigured = _settings(xing_mailbox_app_password="")
    assert [m.label for m in application_mailboxes(unconfigured)] == ["primary"]
    same_account = _settings(xing_mailbox_username=PRIMARY.upper())
    assert [m.label for m in application_mailboxes(same_account)] == ["primary"]
    assert all("app-password" not in repr(m) for m in application_mailboxes(_settings()))


def _parsed(account, message_id, uid, arrival, subject="Hallo"):
    return ParsedGmailMessage(
        account_key=account,
        mailbox="INBOX",
        uid=uid,
        uid_validity=1,
        message_id_header=message_id,
        in_reply_to=None,
        references=(),
        from_address="hr@employer.example",
        from_display_name="HR",
        to_addresses=(account,),
        cc_addresses=(),
        subject=subject,
        sent_at=arrival,
        direction="INBOUND",
        body_plain="Text",
        body_truncated=False,
        has_html=False,
        provider_arrival_at=arrival,
    )


class _FakeProvider:
    instances: list["_FakeProvider"] = []
    by_user: dict[str, object] = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeProvider.instances.append(self)

    async def fetch(self, since):
        outcome = _FakeProvider.by_user[self.kwargs["username"]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fetch_merges_both_mailboxes_read_only_and_dedups_message_ids():
    early = datetime(2026, 10, 21, 8, tzinfo=UTC)
    late = early + timedelta(hours=3)
    _FakeProvider.instances = []
    _FakeProvider.by_user = {
        PRIMARY: GmailFetchResult(
            messages=(_parsed(PRIMARY, "<shared@x>", 1, late), _parsed(PRIMARY, "<p@x>", 2, late)),
            skipped_count=0,
        ),
        SECONDARY: GmailFetchResult(
            messages=(
                _parsed(SECONDARY, "<shared@x>", 7, late),
                _parsed(SECONDARY, "<s@x>", 8, early),
            ),
            skipped_count=1,
        ),
    }
    messages, reports = asyncio.run(
        fetch_application_mailboxes(_settings(), since=early, provider_factory=_FakeProvider)
    )
    assert [m.message_id_header for m in messages] == ["<s@x>", "<shared@x>", "<p@x>"]
    assert [(r.label, r.fetched, r.skipped, r.error) for r in reports] == [
        ("primary", 2, 0, None),
        ("secondary", 1, 1, None),
    ]
    for provider in _FakeProvider.instances:
        assert provider.kwargs["mailbox"] == "INBOX"
        assert provider.kwargs["trusted_outbound"] is False


def test_one_failing_mailbox_does_not_hide_the_other():
    _FakeProvider.by_user = {
        PRIMARY: GmailConnectionError("boom"),
        SECONDARY: GmailFetchResult(
            messages=(_parsed(SECONDARY, "<s@x>", 8, SUBMITTED_AT),), skipped_count=0
        ),
    }
    messages, reports = asyncio.run(
        fetch_application_mailboxes(_settings(), since=SUBMITTED_AT, provider_factory=_FakeProvider)
    )
    assert [m.message_id_header for m in messages] == ["<s@x>"]
    assert [(r.label, r.error) for r in reports] == [
        ("primary", "GmailConnectionError"),
        ("secondary", None),
    ]


def test_cli_reports_mailboxes_and_fails_on_an_unread_mailbox(tmp_path, monkeypatch, capsys):
    import scripts.stage15_application_tracker as cli
    from tests.test_stage15_response_tracking import _migrated_db

    path = _migrated_db(tmp_path)
    _FakeProvider.by_user = {
        PRIMARY: GmailFetchResult(messages=(), skipped_count=0),
        SECONDARY: GmailConnectionError("down"),
    }
    import app.services.application_response_processor as processor

    original = processor.fetch_application_mailboxes

    async def fake_fetch(settings, *, since, selection="all"):
        return await original(
            settings, since=since, selection=selection, provider_factory=_FakeProvider
        )

    monkeypatch.setattr(processor, "fetch_application_mailboxes", fake_fetch)
    code = cli.run(
        ["--database", str(path), "process-gmail", "--since", "2026-10-20"],
        settings=_settings(),
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "MAILBOX: primary fetched=0" in err
    assert "MAILBOX: secondary fetched=0 skipped=0" in err
    assert "error=GmailConnectionError" in err
    assert "app-password" not in err and SECONDARY not in err
