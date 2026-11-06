"""Audit FLOW-04 (no-reply recipients) and FLOW-13 (HTML-only mail stored with its text)."""

import pytest

from app.agents.email_classifier import classify_email
from app.services.response_draft_recipient import ReplyRecipientInvalidError, derive_reply_recipient

ACCOUNT = "me@example.com"


@pytest.mark.parametrize(
    "address",
    [
        "noreply@ats.example.com",
        "no-reply@ats.example.com",
        "NoReply@ats.example.com",
        "no_reply@ats.example.com",
        "do-not-reply@ats.example.com",
        "donotreply@ats.example.com",
        "noreply+abc123@ats.example.com",
        "no-reply-jobs@ats.example.com",
        "mailer-daemon@example.com",
        "postmaster@example.com",
        "bounces@mail.example.com",
    ],
)
def test_no_reply_senders_get_no_reply_recipient(address):
    with pytest.raises(ReplyRecipientInvalidError) as excinfo:
        derive_reply_recipient(address, account_key=ACCOUNT)
    assert "no-reply" in str(excinfo.value)
    assert address not in str(excinfo.value)  # the address is never echoed


@pytest.mark.parametrize(
    "address",
    [
        "hr@muster.de",
        "jane.doe@muster.de",
        "recruiting@muster.de",
        "reply@muster.de",
        "noreplyhr@muster.de",
        "anna.noreply@muster.de",
        "jobs@noreply-corp.example.com",
    ],
)
def test_real_people_and_team_mailboxes_stay_valid(address):
    assert derive_reply_recipient(address, account_key=ACCOUNT) == address


def test_the_interview_invitation_from_an_ats_is_not_replyable():
    result = classify_email(
        "Einladung zum Vorstellungsgespräch", "Wir freuen uns.", "noreply@ats.example.com"
    )
    assert result.is_automated
    with pytest.raises(ReplyRecipientInvalidError):
        derive_reply_recipient("noreply@ats.example.com", account_key=ACCOUNT)


def test_html_only_message_keeps_its_text_when_stored(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import gmail_repository
    from app.db.base import Base
    from app.providers.email.base import ParsedGmailMessage

    engine = create_engine(f"sqlite:///{tmp_path / 'html_only.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        parsed = ParsedGmailMessage(
            account_key=ACCOUNT,
            mailbox="INBOX",
            uid_validity=1,
            uid=1,
            message_id_header="<html-only@example.com>",
            in_reply_to=None,
            references=(),
            from_address="hr@muster.de",
            from_display_name="HR",
            to_addresses=(ACCOUNT,),
            cc_addresses=(),
            subject="Ihre Bewerbung",
            sent_at=None,
            direction="INBOUND",
            body_plain="",
            body_truncated=False,
            has_html=True,
            body_html_text=(
                "Leider müssen wir Ihnen mitteilen, dass wir uns "
                "für einen anderen Kandidaten entschieden haben."
            ),
            attachments=(),
        )
        record, created = gmail_repository.upsert_message(db, parsed)
        assert created
        assert "anderen Kandidaten" in record.body_plain
        assert record.has_html is True


def test_truncated_html_text_is_flagged_as_truncated(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import gmail_repository
    from app.db.base import Base
    from app.providers.email.base import MAX_BODY_LENGTH, ParsedGmailMessage

    engine = create_engine(f"sqlite:///{tmp_path / 'html_trunc.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        parsed = ParsedGmailMessage(
            account_key=ACCOUNT,
            mailbox="INBOX",
            uid_validity=1,
            uid=2,
            message_id_header="<long-html@example.com>",
            in_reply_to=None,
            references=(),
            from_address="hr@muster.de",
            from_display_name="HR",
            to_addresses=(ACCOUNT,),
            cc_addresses=(),
            subject="Ihre Bewerbung",
            sent_at=None,
            direction="INBOUND",
            body_plain="",
            body_truncated=False,
            has_html=True,
            body_html_text="x" * MAX_BODY_LENGTH,
            attachments=(),
        )
        record, _ = gmail_repository.upsert_message(db, parsed)
        assert record.body_truncated is True


def test_imap_flags_html_text_cut_by_the_input_cap():
    from email.message import EmailMessage

    from app.providers.email.base import MAX_BODY_LENGTH
    from app.providers.email.imap import _extract_html_text_bounded

    short = EmailMessage()
    short.set_content("<p>Hallo</p>", subtype="html")
    assert _extract_html_text_bounded(short) == ("Hallo", False)

    long_markup = EmailMessage()
    long_markup.set_content("<p>x</p>" + "<!-- c -->" * (MAX_BODY_LENGTH * 2), subtype="html")
    text, truncated = _extract_html_text_bounded(long_markup)
    assert truncated is True and len(text) <= MAX_BODY_LENGTH
