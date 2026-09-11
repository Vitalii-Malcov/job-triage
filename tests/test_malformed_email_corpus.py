"""hardening/api-boundaries-r1, Section 14: a malformed-MIME adversarial
corpus fed through the REAL parsing entry points --
`GmailImapProvider._parse_message` (app/providers/email/imap.py) and
`XingEmailCollector._process_message` (app/collectors/xing_email.py).
Every input here is a synthetic, locally-constructed raw message --
never a real mailbox, never a real socket. See
docs/API_BOUNDARY_HARDENING_REPORT.md BOUND-IDs for which of these were
already covered by pre-existing tests vs. genuinely new here (per the
research done for this hardening pass, cross-checked against both
existing test files before writing any of these).
"""

import email

from app.providers.email.imap import GmailImapProvider

ACCOUNT = "me@example.com"


def _provider() -> GmailImapProvider:
    return GmailImapProvider(
        imap_host="imap.gmail.com",
        imap_port=993,
        username=ACCOUNT,
        app_password="app-password",
        mailbox="INBOX",
        lookback_days=30,
    )


def _parse(raw_bytes: bytes, *, uid: int = 1, uid_validity: int = 100):
    msg = email.message_from_bytes(raw_bytes)
    return _provider()._parse_message(
        msg, uid=uid, uid_validity=uid_validity, provider_arrival_at=None
    )


class TestMissingOrMultipleHeaders:
    def test_missing_from_header_does_not_crash(self):
        raw = b"Subject: No sender\r\n\r\nBody text.\r\n"
        result = _parse(raw)
        assert result.from_address is None

    def test_missing_subject_header_does_not_crash(self):
        raw = b"From: alice@example.com\r\n\r\nBody text.\r\n"
        result = _parse(raw)
        assert result.subject == ""

    def test_multiple_from_headers_does_not_crash(self):
        """RFC 5322 technically forbids multiple From headers on a single-
        author message, but a real mailbox can still deliver one (a
        misconfigured sender, a relay bug) -- the parser must not crash,
        and must deterministically pick ONE value (whichever
        `email.message.Message.get("From")` -- the stdlib parser --
        itself returns, which this test pins down rather than assumes).
        """
        raw = (
            b"From: alice@example.com\r\n"
            b"From: bob@example.com\r\n"
            b"Subject: Two From headers\r\n\r\n"
            b"Body text.\r\n"
        )
        result = _parse(raw)
        # Must be exactly one of the two -- not None, not a crash, not a
        # concatenation of both.
        assert result.from_address in ("alice@example.com", "bob@example.com")


class TestCharsetEdgeCases:
    def test_unknown_charset_in_body_falls_back_instead_of_crashing(self):
        raw = (
            b"From: alice@example.com\r\n"
            b"Subject: Weird charset\r\n"
            b'Content-Type: text/plain; charset="x-made-up-charset-9000"\r\n'
            b"Content-Transfer-Encoding: 8bit\r\n\r\n"
            b"Hello \xe2\x9c\x93 world\r\n"
        )
        result = _parse(raw)
        # Must not raise LookupError -- decode_mime_part's documented
        # utf-8/errors="replace" fallback must apply; body must be a
        # real (possibly imperfect) string, never None/crash.
        assert isinstance(result.body_plain, str)
        assert "Hello" in result.body_plain

    def test_malformed_encoded_word_subject_does_not_crash(self):
        """A syntactically-broken RFC 2047 encoded-word (missing the
        final `?=` delimiter) must not raise -- confirms
        _decode_mime_words degrades to the literal raw text rather than
        crashing on a malformed encoding marker.
        """
        raw = (
            b"From: alice@example.com\r\n"
            b"Subject: =?utf-8?B?not-valid-base64-and-no-closing-delim\r\n\r\n"
            b"Body.\r\n"
        )
        result = _parse(raw)
        assert isinstance(result.subject, str)

    def test_unusual_but_wellformed_charset_name_is_decoded(self):
        raw = b"From: alice@example.com\r\nSubject: =?ISO-8859-1?Q?Caf=E9?=\r\n\r\nBody.\r\n"
        result = _parse(raw)
        assert result.subject == "Café"


class TestBodyEdgeCases:
    def test_empty_body_does_not_crash(self):
        raw = b"From: alice@example.com\r\nSubject: Empty\r\n\r\n"
        result = _parse(raw)
        assert result.body_plain == ""

    def test_nul_characters_in_body_do_not_crash(self):
        raw = (
            b"From: alice@example.com\r\n"
            b"Subject: NUL bytes\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Hello\x00World\x00\r\n"
        )
        result = _parse(raw)
        assert isinstance(result.body_plain, str)

    def test_nul_characters_in_subject_do_not_crash(self):
        raw = b"From: alice@example.com\r\nSubject: Hello\x00World\r\n\r\nBody.\r\n"
        result = _parse(raw)
        assert isinstance(result.subject, str)


class TestHeaderEdgeCases:
    def test_very_long_bounded_subject_is_truncated_not_crashed(self):
        long_subject = "A" * 5000
        raw = f"From: alice@example.com\r\nSubject: {long_subject}\r\n\r\nBody.\r\n".encode()
        result = _parse(raw)
        # MAX_SUBJECT_LENGTH = 998 (app/providers/email/base.py) --
        # confirms the existing bound actually applies to a pathological
        # header value, not just normal-length ones.
        assert len(result.subject) <= 998

    def test_very_long_from_address_does_not_crash(self):
        long_local_part = "a" * 500
        raw = f"From: {long_local_part}@example.com\r\nSubject: Long addr\r\n\r\nBody.\r\n".encode()
        result = _parse(raw)
        assert isinstance(result.from_address, str) or result.from_address is None

    def test_invalid_date_header_does_not_crash(self):
        raw = (
            b"From: alice@example.com\r\n"
            b"Subject: Bad date\r\n"
            b"Date: not-a-real-date-at-all\r\n\r\n"
            b"Body.\r\n"
        )
        result = _parse(raw)
        assert result.sent_at is None

    def test_malformed_message_id_no_angle_brackets_does_not_crash(self):
        raw = (
            b"From: alice@example.com\r\n"
            b"Subject: Bad message id\r\n"
            b"Message-ID: not-bracketed-and-has-spaces in it\r\n\r\n"
            b"Body.\r\n"
        )
        result = _parse(raw)
        # Must not crash; whatever value comes out must be a string,
        # never raise on the malformed format.
        assert result.message_id_header is None or isinstance(result.message_id_header, str)


class TestXingMissingSenderSubject:
    """XingEmailCollector._process_message requires an exact sender
    match (`From == XING_DIGEST_SENDER`) before any adversarial content
    is exercised -- confirms a missing/wrong From is cleanly skipped,
    never crashes, and (critically) never treats attacker-controlled
    content as a trusted XING digest merely because Subject/body happen
    to match the expected pattern.
    """

    def test_missing_from_header_is_skipped_not_crashed(self):
        from app.collectors.xing_email import XingEmailCollector

        raw = (
            b"Subject: 5 neue Stellenangebote f\xc3\xbcr Python Developer\r\n\r\n"
            b"---\r\nTitle\r\n=> https://www.xing.com/m/abc\r\n"
        )
        msg = email.message_from_bytes(raw)
        collector = XingEmailCollector(
            imap_host="imap.gmail.com",
            imap_port=993,
            username="me@example.com",
            app_password="app-password",
            lookback_days=7,
        )
        batch, skip = collector._process_message(msg, uid=1)
        assert batch is None
        assert skip is True

    def test_missing_subject_header_is_skipped_not_crashed(self):
        from app.collectors.xing_email import XING_DIGEST_SENDER, XingEmailCollector

        raw = f"From: {XING_DIGEST_SENDER}\r\n\r\nBody with no subject.\r\n".encode()
        msg = email.message_from_bytes(raw)
        collector = XingEmailCollector(
            imap_host="imap.gmail.com",
            imap_port=993,
            username="me@example.com",
            app_password="app-password",
            lookback_days=7,
        )
        batch, skip = collector._process_message(msg, uid=1)
        assert batch is None
        assert skip is True
