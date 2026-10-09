"""Stage 9D: the APPEND-only Gmail Drafts provider -- modified UTF-7 codec,
LIST tokenizer/verification, MIME contract, APPEND result classification,
read-only lookup parsing, and the allowed-command trace. No network: a
fake client records every IMAP command."""

import ast
import binascii
import imaplib
import re
import ssl
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

import app.providers.email.imap_draft as imap_draft
from app.providers.email import draft_base
from app.providers.email.draft_base import (
    DraftAuthError,
    DraftBudgetExhaustedError,
    DraftConnectError,
    DraftCreateDefiniteError,
    DraftCreateOutcomeUnknownError,
    DraftCreateRejectedError,
    DraftLookupError,
    DraftMessage,
    DraftMessageInvalidError,
    DraftsDisabledError,
    DraftsMailboxInvalidError,
    DraftTarget,
    ReconcileTarget,
    build_draft_mime,
)
from app.providers.email.imap_draft import (
    GmailImapDraftProvider,
    ListParseError,
    MailboxNameError,
    decode_modified_utf7,
    encode_mailbox_wire,
    encode_modified_utf7,
    parse_list_response,
    verify_drafts_target,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
ACCOUNT = "me@example.com"
MARKER = "<s9d." + "0123456789abcdef" * 2 + "@ai-job-search.invalid>"
DRAFTS_LINE = b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"'
LIST_DATA = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
    DRAFTS_LINE,
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
]
ALLOWED_COMMANDS = {"LOGIN", "LIST", "RESPONSE", "APPEND", "EXAMINE", "UID", "LOGOUT"}


# RFC 2047 section 2: encoded-text excludes "?" and space.
ENCODED_WORD = re.compile(r"=\?[^?\s]+\?[QqBb]\?[!->@-~]*\?=")
HEX_UPPER = "0123456789ABCDEF"


def _strict_decode_word(word: str) -> str:
    """Independent strict decode of one UTF-8 encoded word (S9D-CODEX-FINAL-001):
    nonempty payload; B is strict, canonical base64; Q is literal printable
    ASCII except "=", "?" and space, "_" for space, "=" + two uppercase hex
    digits; the bytes must be valid UTF-8. Raises ValueError otherwise."""
    charset, encoding, payload = word[2:-2].split("?")
    if charset.lower() != "utf-8" or not payload:
        raise ValueError(word)
    if encoding.lower() == "b":
        raw = binascii.a2b_base64(payload, strict_mode=True)
        if binascii.b2a_base64(raw, newline=False).decode("ascii") != payload:
            raise ValueError(word)
    else:
        out, i = bytearray(), 0
        while i < len(payload):
            char = payload[i]
            if char == "=":
                pair = payload[i + 1 : i + 3]
                if len(pair) != 2 or any(c not in HEX_UPPER for c in pair):
                    raise ValueError(word)
                out.append(int(pair, 16))
                i += 3
                continue
            if char in "? " or not "!" <= char <= "~":
                raise ValueError(word)
            out.append(0x20 if char == "_" else ord(char))
            i += 1
        raw = bytes(out)
    return raw.decode("utf-8")


# S9D-CODEX-FINAL-001: synthetic malformed encoder output, each refused.
MALFORMED_ENCODED = {
    "empty-q": "=?utf-8?q??=",
    "empty-b": "=?utf-8?b??=",
    "truncated-q": "=?utf-8?q?=0?=",
    "truncated-q-end": "=?utf-8?q?A=?=",
    "invalid-q-hex": "=?utf-8?q?=GG?=",
    "lowercase-q-hex": "=?utf-8?q?=c3=a9?=",
    "codex-invalid-q": "=?utf-8?q?=3D=3Futf-8=3Fq=3FX=3F=3D_=GG?=",
    "invalid-b-alphabet": "=?utf-8?b?!!!!?=",
    "codex-invalid-b": "=?utf-8?b?PT91!dGYtOD9xP1g/PQ==?=",
    "invalid-b-length": "=?utf-8?b?A?=",
    "invalid-b-length-5": "=?utf-8?b?QUJDR?=",
    "invalid-b-padding": "=?utf-8?b?QQ=?=",
    "discontinuous-b-padding": "=?utf-8?b?QQ=A?=",
    "excess-b-after-padding": "=?utf-8?b?QQ==QQ==?=",
    "noncanonical-b-bits": "=?utf-8?b?QR==?=",
    "invalid-utf8-q": "=?utf-8?q?=FF?=",
    "invalid-utf8-q-truncated-seq": "=?utf-8?q?=C3?=",
    "invalid-utf8-b": "=?utf-8?b?/w==?=",
    "invalid-utf8-b-overlong": "=?utf-8?b?wK8=?=",
    "valid-then-invalid": "=?utf-8?q?ok?= =?utf-8?q?=GG?=",
}
# Hand-written strictly valid output, accepted by the validator.
VALID_ENCODED = {
    "q-ascii": "=?utf-8?q?Bewerbung_als_Developer?=",
    "q-unicode": "=?utf-8?q?Gr=C3=B6=C3=9Fe_=F0=9F=9A=80?=",
    "b-unicode": "=?utf-8?b?R3LDtsOfZSDwn5qA?=",
    "q-multi-word": "=?utf-8?q?Bewerbung_?= =?utf-8?q?als_Developer?=",
    "b-multi-word": "=?utf-8?b?QmV3ZXJidW5n?=\n =?utf-8?b?IGFscyBEZXZlbG9wZXI=?=",
    "q-literal-safe-chars": "=?utf-8?q?a!*+-/b?=",
}


def _subject_wire_lines(data: bytes) -> list[str]:
    """The raw (folded, undecoded) Subject header lines of serialized MIME."""
    head = data.split(b"\r\n\r\n", 1)[0].decode("ascii").split("\r\n")
    (start,) = [i for i, line in enumerate(head) if line.lower().startswith("subject:")]
    end = start + 1
    while end < len(head) and head[end][:1] in (" ", "\t"):
        end += 1
    return head[start:end]


def _assert_rfc2047_subject(data: bytes) -> tuple[int, int]:
    """Independent RFC 2047 check of an encoded-path Subject: every token is
    one encoded word of at most 75 characters, every line is at most 76
    characters and every continuation line starts with whitespace. Returns
    (max encoded-word length, max line length)."""
    lines = _subject_wire_lines(data)
    tokens = lines[0][len("Subject:") :].split() + [
        token for line in lines[1:] for token in line.split()
    ]
    assert tokens and all(ENCODED_WORD.fullmatch(token) for token in tokens), lines
    for token in tokens:
        _strict_decode_word(token)  # raises on a malformed payload
    assert all(len(token) <= 75 for token in tokens), lines
    assert all(len(line) <= 76 for line in lines), lines
    assert all(line[:1] in (" ", "\t") for line in lines[1:]), lines
    return max(map(len, tokens)), max(map(len, lines))


def _target(mailbox="[Gmail]/Drafts", account=ACCOUNT) -> DraftTarget:
    return DraftTarget(account, mailbox, encode_mailbox_wire(mailbox))


def _message(**overrides) -> DraftMessage:
    values = dict(
        from_address=ACCOUNT,
        subject="Bewerbung als Junior Python Developer",
        body_lf="Sehr geehrte Damen und Herren,\n\nich bewerbe mich.\n",
        message_id=MARKER,
    )
    values.update(overrides)
    return DraftMessage(**values)


# S9D-ASTRA-001 adversarial subject matrix.
SUBJECT_ACCEPT_EXACT = {
    "ascii": "Bewerbung als Junior Python Developer",
    "unicode-german": "Bewerbung als Softwareentwickler (m/w/d) – Müller & Söhne GmbH, Köln",
    "emoji-supplementary": "Bewerbung 🚀 als 𝔇eveloper ✓",
    "long": "Bewerbung als " + "Senior Python Backend Entwickler für Datenplattformen " * 6,
    "q-encoded-word": "Bewerbung als Junior Python Developer =?utf-8?q?Unapproved_Subject_Text?=",
    "b-encoded-word": "Bewerbung als =?UTF-8?B?VW5hcHByb3ZlZCBTdWJqZWN0?= Developer",
    "encoded-cr": "Bewerbung =?utf-8?q?=0D?= Developer",
    "encoded-lf": "Bewerbung =?utf-8?q?=0A?=Bcc: x@example.com",
    "encoded-nul": "Bewerbung =?utf-8?q?=00?= Developer",
    "adjacent-encoded-words": "=?utf-8?q?a?= =?utf-8?q?b?=",
    "long-encoded-word-unicode": "Bewerbung =?utf-8?q?X?= Größe 🚀 " * 8,
    # S9D-CODEX-ASTRAFIX-001: long encoded-path subjects. With the stdlib
    # default line length these produced 76/77-character encoded words.
    "rfc2047-codex-repro": "Bewerbung =?utf-8?q?X?= " + "A" * 85,
    "rfc2047-threshold-75": "Bewerbung =?utf-8?q?X?= " + "A" * 84,
    "rfc2047-threshold-77": "Bewerbung =?utf-8?q?X?= " + "A" * 86,
    "rfc2047-long-mixed": "Bewerbung =?utf-8?q?X?= Größe 🚀 =?UTF-8?B?VW5h?= Müller " * 6,
    "rfc2047-long-q": "=?utf-8?q?" + "Unapproved_Subject_Text_=3D=0A" * 10 + "?=",
    "rfc2047-long-b": "=?UTF-8?B?" + "VW5hcHByb3ZlZCBTdWJqZWN0" * 10 + "?=",
    "rfc2047-adjacent-spaced": " ".join(f"=?utf-8?q?seg{i}_Text?=" for i in range(12)),
    "rfc2047-adjacent-unspaced": "".join(f"=?utf-8?b?U2VnbWVudA{i}?=" for i in range(12)),
}
# Header-injection vectors: accepted only as the literal approved text.
SUBJECT_INJECTION = {
    "q-lf-bcc": "=?utf-8?q?=0A?=Bcc: attacker@example.com",
    "q-crlf-bcc": "=?utf-8?q?=0D=0ABcc=3A_attacker@example.com?=",
    **{
        f"then-{name.lower()}": f"Bewerbung =?utf-8?q?X?= {name}: attacker@example.com"
        for name in ("Bcc", "Cc", "To", "Reply-To", "Content-Type", "Subject")
    },
    "folding-boundary": "A" * 60 + " =?utf-8?q?=0A?=Bcc: attacker@example.com " + "B" * 70,
}
SUBJECT_ENCODED_PATH = {
    name: subject
    for name, subject in {**SUBJECT_ACCEPT_EXACT, **SUBJECT_INJECTION}.items()
    if "=?" in subject
}
SUBJECT_REJECT = {
    "raw-cr": "Bewerbung\rBcc: x@example.com",
    "raw-lf": "Bewerbung\nBcc: x@example.com",
    "raw-nul": "Bewerbung\x00Developer",
}


class FakeClient:
    """Records every IMAP command; scripted results; imaplib-like untagged
    response cache for APPENDUID / UIDVALIDITY."""

    def __init__(
        self,
        *,
        list_result=("OK", LIST_DATA),
        append_result=("OK", [b"[APPENDUID 7 42] (Success)"]),
        appenduid=(b"7 42",),
        append_exc=None,
        login_result=("OK", [b"ok"]),
        login_exc=None,
        logout_exc=None,
        select_result=("OK", [b"3"]),
        uidvalidity=(b"7",),
        search_result=("OK", [b"42"]),
        stale_appenduid=None,
        on_list=None,
    ):
        self.trace: list[tuple] = []
        self.appended: list[bytes] = []
        self.untagged: dict[str, list] = {}
        if stale_appenduid is not None:
            self.untagged["APPENDUID"] = [stale_appenduid]
        self.capabilities = ("IMAP4REV1", "UIDPLUS")
        self.debug = 0
        self.list_result = list_result
        self.append_result = append_result
        self.appenduid = appenduid
        self.append_exc = append_exc
        self.login_result = login_result
        self.login_exc = login_exc
        self.logout_exc = logout_exc
        self.select_result = select_result
        self.uidvalidity = uidvalidity
        self.search_result = search_result
        self.on_list = on_list

    def login(self, user, password):
        self.trace.append(("LOGIN",))
        if self.login_exc:
            raise self.login_exc
        return self.login_result

    def list(self, directory='""', pattern="*"):
        self.trace.append(("LIST", directory, pattern))
        if self.on_list:
            self.on_list()
        return self.list_result

    def response(self, code):
        self.trace.append(("RESPONSE", code))
        return code, self.untagged.pop(code, [None])

    def append(self, mailbox, flags, date_time, message):
        self.trace.append(("APPEND", mailbox, flags, date_time))
        self.appended.append(message)
        if self.append_exc:
            raise self.append_exc
        if self.appenduid is not None and self.append_result[0] == "OK":
            self.untagged["APPENDUID"] = list(self.appenduid)
        return self.append_result

    def select(self, mailbox, readonly=False):
        self.trace.append(("EXAMINE" if readonly else "SELECT", mailbox))
        if self.uidvalidity is not None:
            self.untagged["UIDVALIDITY"] = list(self.uidvalidity)
        return self.select_result

    def uid(self, command, *args):
        self.trace.append(("UID", command, *args))
        return self.search_result

    def logout(self):
        self.trace.append(("LOGOUT",))
        if self.logout_exc:
            raise self.logout_exc
        return "BYE", [b""]

    def commands(self):
        return [entry[0] for entry in self.trace]


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def _provider(client=None, *, enabled=True, clock=None, factory_calls=None):
    def factory(deadline, timeout):
        if factory_calls is not None:
            factory_calls.append(timeout)
        return client

    return GmailImapDraftProvider(
        "imap.example.com",
        993,
        ACCOUNT,
        "app-password-secret",
        drafts_enabled=enabled,
        client_factory=factory,
        clock=clock or Clock(),
    )


def _create(client, *, deadline=NOW + timedelta(seconds=60), **kwargs):
    return _provider(client, **kwargs).create_draft(_message(), _target(), deadline)


# --- modified UTF-7 ---------------------------------------------------------------


class TestModifiedUtf7:
    @pytest.mark.parametrize(
        ("decoded", "encoded"),
        [
            ("[Gmail]/Drafts", "[Gmail]/Drafts"),
            ("[Gmail]/Entwürfe", "[Gmail]/Entw&APw-rfe"),
            ("A&B", "A&-B"),
            ("Mein Ordner", "Mein Ordner"),
            ("日本語", "&ZeVnLIqe-"),
            ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
            ("😀", "&2D3eAA-"),
        ],
    )
    def test_round_trip(self, decoded, encoded):
        assert encode_modified_utf7(decoded) == encoded
        assert decode_modified_utf7(encoded) == decoded

    @pytest.mark.parametrize(
        "raw",
        [
            "&AOk",  # unterminated
            "&AOk-&AOk-",  # non-canonical split run
            "&AGE-",  # encodes printable ASCII
            "&AO-",  # odd UTF-16 length
            "&2D0-",  # lone surrogate
            "&A/k-",  # '/' is not modified base64
            "café",  # raw non-ASCII on the wire
            "a\x01b",
        ],
    )
    def test_strict_decode_rejects_malformed_or_non_canonical(self, raw):
        with pytest.raises(MailboxNameError):
            decode_modified_utf7(raw)

    def test_quoting_escapes_quotes_and_backslashes(self):
        name = 'Ent "wurf" \\ A&B'
        wire = encode_mailbox_wire(name)
        assert wire == '"Ent \\"wurf\\" \\\\ A&-B"'
        assert imap_draft._unquote(wire) == 'Ent "wurf" \\ A&-B'


class TestMailboxValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "",
            "   ",
            " [Gmail]/Drafts",
            "[Gmail]/Drafts ",
            "Dra\rfts",
            "Dra\nfts",
            "Dra\x00fts",
            "Dra\x1bfts",
            "Dra\x7ffts",
            "Drafts*",
            "Dra%fts",
            "INBOX",
            "inbox",
            "x" * 201,
        ],
    )
    def test_invalid_names(self, name):
        with pytest.raises(MailboxNameError) as exc:
            encode_mailbox_wire(name)
        assert exc.value.code == imap_draft.MAILBOX_INVALID
        assert name.strip() == "" or name.strip() not in str(exc.value)

    def test_lone_surrogate_is_unencodable(self):
        with pytest.raises(MailboxNameError) as exc:
            encode_mailbox_wire("Drafts\ud800")
        assert exc.value.code == imap_draft.MAILBOX_UNENCODABLE

    @pytest.mark.parametrize(
        "name",
        [
            "😀" * 200,  # one long supplementary run
            "😀a" * 100,  # isolated supplementary characters
            '😀"' * 100,  # isolated supplementary + escaped quote
            "😀\\" * 100,
            "&" * 200,
            'é"' * 100,
        ],
    )
    def test_worst_case_200_code_points_fit_the_approved_2048_bound(self, name):
        assert len(name) <= draft_base.MAX_DRAFTS_MAILBOX_LENGTH
        wire = encode_mailbox_wire(name)
        assert len(wire) <= 2048
        assert decode_modified_utf7(imap_draft._unquote(wire)) == name

    def test_conservative_per_code_point_upper_bound(self):
        # Every code point costs at most 8 wire chars (an isolated
        # supplementary char: "&" + 6 base64 + "-"), plus 2 outer quotes.
        assert 8 * draft_base.MAX_DRAFTS_MAILBOX_LENGTH + 2 == 1602 <= 2048
        assert draft_base.MAX_DRAFTS_MAILBOX_WIRE_LENGTH == 2048

    def test_encoded_value_exceeding_the_bound_fails_closed(self, monkeypatch):
        monkeypatch.setattr(draft_base, "MAX_DRAFTS_MAILBOX_WIRE_LENGTH", 40)
        name = "Entwürfe-" * 4
        with pytest.raises(MailboxNameError) as exc:
            encode_mailbox_wire(name)
        assert exc.value.code == imap_draft.MAILBOX_UNENCODABLE


# --- LIST ---------------------------------------------------------------------------


class TestListParsing:
    def test_gmail_style_response(self):
        entries = parse_list_response(LIST_DATA)
        drafts = [entry for entry in entries if entry.name == "[Gmail]/Drafts"]
        assert len(drafts) == 1 and "\\drafts" in drafts[0].flags
        assert drafts[0].delimiter == "/"

    def test_quoted_escapes_atoms_nil_and_literals(self):
        data = [
            b'(\\Drafts) "/" "Ent \\"wurf\\" \\\\ x"',
            b"(\\HasNoChildren) NIL Plain",
            (b'(\\Drafts \\HasNoChildren) "/" {20}', b"[Gmail]/Entw&APw-rfe"),
            b"",
            b'(\\Marked) "\\\\" "A&-B"',
        ]
        entries = parse_list_response(data)
        assert [entry.name for entry in entries] == [
            'Ent "wurf" \\ x',
            "Plain",
            "[Gmail]/Entwürfe",
            "A&B",
        ]
        assert entries[1].delimiter is None and entries[3].delimiter == "\\"

    def test_no_entries(self):
        assert parse_list_response([None]) == []

    @pytest.mark.parametrize(
        "data",
        [
            [b'\\Drafts "/" "x"'],  # no flag list
            [b'(\\Drafts "/" "x"'],  # unclosed flags
            [b'(\\Drafts)  "/" "x"'],  # double space
            [b'(\\Drafts) "/" "x" trailing'],
            [b'(\\Drafts) "/" "unterminated'],
            [b'(\\Drafts) "/" "bad \\x escape"'],
            [b'(\\Drafts) "//" "x"'],  # delimiter longer than one char
            [b'(\\Drafts) "/" a b'],  # atom with a space
            [b'(\\Drafts) "/" ""'],  # empty name
            [b'(\\Drafts) "/" "caf\xc3\xa9"'],  # raw 8-bit name
            [b'(\\Drafts) "/" "&AGE-"'],  # non-canonical modified UTF-7
            [(b'(\\Drafts) "/" {5}', b"abc"), b""],  # literal length mismatch
            [(b'(\\Drafts) "/" {3}', b"abc")],  # missing trailer
            [(b'(\\Drafts) "/" {3}', b"abc"), b" junk"],
            [(b'(\\Drafts) "/" {3}', b"a\nb"), b""],
            [42],
            "not a list",
        ],
    )
    def test_malformed_forms_are_unverifiable(self, data):
        with pytest.raises(ListParseError):
            parse_list_response(data)


class TestDraftsVerification:
    def _verify(self, lines, mailbox="[Gmail]/Drafts"):
        verify_drafts_target(parse_list_response(lines), _target(mailbox))

    def test_normal_and_localized(self):
        self._verify(LIST_DATA)
        self._verify([b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Entw&APw-rfe"'], "[Gmail]/Entwürfe")

    @pytest.mark.parametrize(
        "line",
        [
            b'(\\HasNoChildren) "/" "[Gmail]/Drafts"',  # no \Drafts
            b'(\\Drafts \\Noselect) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\NonExistent) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\Sent) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\Inbox) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\All) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\Trash) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\Junk) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\Flagged) "/" "[Gmail]/Drafts"',
            b'(\\Drafts \\Important) "/" "[Gmail]/Drafts"',
        ],
    )
    def test_wrong_special_use_is_refused(self, line):
        with pytest.raises(DraftsMailboxInvalidError):
            self._verify([line])

    def test_missing_and_ambiguous_are_refused(self):
        with pytest.raises(DraftsMailboxInvalidError):
            self._verify([b'(\\Drafts) "/" "[Gmail]/Entw&APw-rfe"'])
        with pytest.raises(DraftsMailboxInvalidError):
            self._verify([DRAFTS_LINE, b'(\\Drafts) "." "[Gmail]/Drafts"'])

    def test_inbox_named_target_is_refused(self):
        entries = parse_list_response([b'(\\Drafts) "/" "INBOX"'])
        with pytest.raises(DraftsMailboxInvalidError):
            verify_drafts_target(entries, DraftTarget(ACCOUNT, "INBOX", '"INBOX"'))


# --- MIME ----------------------------------------------------------------------------


class TestMime:
    def test_contract(self):
        data = build_draft_mime(_message(subject="Bewerbung – Größe ✓"), date=NOW)
        parsed = BytesParser(policy=policy.SMTP).parsebytes(data)
        assert {name.lower() for name in parsed.keys()} == draft_base.ALLOWED_HEADERS
        for name in ("To", "Cc", "Bcc", "Reply-To", "In-Reply-To", "References"):
            assert parsed[name] is None
            assert f"\r\n{name}:".encode() not in data and not data.startswith(f"{name}:".encode())
        assert parsed.get_content_type() == "text/plain" and not parsed.is_multipart()
        assert parsed.get_content_charset() == "utf-8"
        assert str(parsed["Subject"]) == "Bewerbung – Größe ✓"
        assert str(parsed["Message-ID"]) == MARKER
        assert b"<html" not in data.lower() and b"attachment" not in data.lower()
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"message_id": "<x@example.com>"},
            {"from_address": "me@example.com\r\nBcc: x@example.com"},
            {"subject": "a\nBcc: x@example.com"},
            {"subject": "a\x00b"},
            {"subject": ""},
            {"body_lf": "a\x00b\n"},
            {"body_lf": "a\r\nb\n"},
        ],
    )
    def test_unsafe_messages_are_refused(self, overrides):
        with pytest.raises(DraftMessageInvalidError):
            build_draft_mime(_message(**overrides), date=NOW)

    @pytest.mark.parametrize(
        "subject", list(SUBJECT_ACCEPT_EXACT.values()), ids=list(SUBJECT_ACCEPT_EXACT)
    )
    def test_subject_round_trips_exactly(self, subject):
        # S9D-ASTRA-001: logical subject -> MIME bytes -> stdlib parse ->
        # decoded logical subject must be the approved string, literally.
        data = build_draft_mime(_message(subject=subject), date=NOW)
        parsed = BytesParser(policy=policy.SMTP).parsebytes(data)
        (decoded,) = parsed.get_all("Subject")
        assert str(decoded) == subject
        assert not any(char in str(decoded) for char in ("\r", "\n", "\x00"))
        assert {name.lower() for name in parsed.keys()} == draft_base.ALLOWED_HEADERS
        assert len(parsed.keys()) == len(draft_base.ALLOWED_HEADERS)  # one Subject
        for name in ("To", "Cc", "Bcc", "Reply-To", "In-Reply-To", "References"):
            assert parsed[name] is None
        assert parsed.get_content_type() == "text/plain" and not parsed.is_multipart()
        assert parsed.get_content_charset() == "utf-8"
        assert str(parsed["From"]) == ACCOUNT and str(parsed["Message-ID"]) == MARKER
        assert parsed.get_content().replace("\r\n", "\n") == _message().body_lf
        assert data.isascii() and all(len(line) <= 998 for line in data.split(b"\r\n"))
        if "=?" in subject:
            _assert_rfc2047_subject(data)

    @pytest.mark.parametrize(
        "subject", list(SUBJECT_INJECTION.values()), ids=list(SUBJECT_INJECTION)
    )
    def test_header_injection_vectors_stay_one_literal_subject(self, subject):
        data = build_draft_mime(_message(subject=subject), date=NOW)
        parsed = BytesParser(policy=policy.SMTP).parsebytes(data)
        (decoded,) = parsed.get_all("Subject")
        assert str(decoded) == subject
        assert len(parsed.keys()) == len(draft_base.ALLOWED_HEADERS)
        for name in ("To", "Cc", "Bcc", "Reply-To", "In-Reply-To", "References"):
            assert parsed[name] is None
        assert parsed.get_content_type() == "text/plain"
        _assert_rfc2047_subject(data)

    @pytest.mark.parametrize(
        "subject", list(SUBJECT_ENCODED_PATH.values()), ids=list(SUBJECT_ENCODED_PATH)
    )
    def test_encoded_subject_meets_rfc2047_limits(self, subject):
        # S9D-CODEX-ASTRAFIX-001: a permissive parser decodes oversized
        # encoded words, so decoded equality alone cannot prove compliance.
        data = build_draft_mime(_message(subject=subject), date=NOW)
        max_word, max_line = _assert_rfc2047_subject(data)
        assert max_word <= 75 and max_line <= 76

    @pytest.mark.parametrize("pad", range(70, 100))
    def test_encoded_word_length_threshold_sweep(self, pad):
        subject = "Bewerbung =?utf-8?q?X?= " + "A" * pad
        data = build_draft_mime(_message(subject=subject), date=NOW)
        _assert_rfc2047_subject(data)
        parsed = BytesParser(policy=policy.SMTP).parsebytes(data)
        assert str(parsed["Subject"]) == subject

    def test_oversized_encoder_output_fails_closed(self, monkeypatch):
        # The stdlib default line length (78) emits a 76-character encoded
        # word for this subject; it must be refused, never transmitted.
        default_header = draft_base.Header

        def header_with_default_line_length(*args, maxlinelen=None, **kwargs):
            return default_header(*args, **kwargs)

        monkeypatch.setattr(draft_base, "Header", header_with_default_line_length)
        subject = SUBJECT_ACCEPT_EXACT["rfc2047-codex-repro"]
        with pytest.raises(DraftMessageInvalidError):
            build_draft_mime(_message(subject=subject), date=NOW)

    @pytest.mark.parametrize(
        "encoded",
        [
            "=?utf-8?q?" + "A" * 64 + "?=",  # 76-character encoded word
            "=?utf-8?q?A?= =?utf-8?q?" + "B" * 52 + "?=",  # line over 76 characters
            "=?utf-8?q?A?=\n=?utf-8?q?B?=",  # continuation without whitespace
            "=?utf-8?q?A?= plain",  # bare text the parser would not decode
            "=?utf-8?q?A B?=",  # space inside encoded-text
            "=?utf-8?q?A?=\n ",  # empty continuation line
        ],
        ids=["word-76", "line-77", "no-fold-space", "bare-text", "inner-space", "empty-line"],
    )
    def test_noncompliant_encoder_output_fails_closed(self, monkeypatch, encoded):
        class FakeHeader:
            def __init__(self, *args, **kwargs):
                pass

            def encode(self):
                return encoded

        monkeypatch.setattr(draft_base, "Header", FakeHeader)
        with pytest.raises(DraftMessageInvalidError):
            build_draft_mime(_message(subject="=?utf-8?q?A?="), date=NOW)

    @pytest.mark.parametrize(
        "encoded", list(MALFORMED_ENCODED.values()), ids=list(MALFORMED_ENCODED)
    )
    def test_malformed_payload_is_not_compliant(self, encoded):
        # S9D-CODEX-FINAL-001: structure and length are not enough; the
        # payload must be strictly valid Q/B and decode to UTF-8.
        with pytest.raises(ValueError):
            for word in encoded.split():
                _strict_decode_word(word)
        assert draft_base._is_rfc2047_compliant(encoded) is False

    @pytest.mark.parametrize(
        "encoded", list(MALFORMED_ENCODED.values()), ids=list(MALFORMED_ENCODED)
    )
    def test_malformed_encoder_output_fails_closed(self, monkeypatch, encoded):
        # The subject is what a permissive parser makes of the malformed
        # output (prefixed by a valid "=?" word), so the final round-trip
        # alone could not catch it: only payload validation refuses it.
        fake = "=?utf-8?q?=3D=3F?= " + encoded
        raw = f"Subject: {fake}\r\n\r\n".encode("ascii")
        subject = str(BytesParser(policy=policy.SMTP).parsebytes(raw)["Subject"])

        class FakeHeader:
            def __init__(self, *args, **kwargs):
                pass

            def encode(self):
                return fake

        monkeypatch.setattr(draft_base, "Header", FakeHeader)
        with pytest.raises(DraftMessageInvalidError):
            build_draft_mime(_message(subject=subject), date=NOW)

    def test_codex_malformed_base64_round_trips_permissively_but_is_refused(self, monkeypatch):
        # The exact Codex demonstration: Python's parser decodes the invalid
        # Base64 word to the approved subject, yet it must never be sent.
        encoded = MALFORMED_ENCODED["codex-invalid-b"]
        raw = f"Subject: {encoded}\r\n\r\n".encode("ascii")
        assert str(BytesParser(policy=policy.SMTP).parsebytes(raw)["Subject"]) == "=?utf-8?q?X?="

        class FakeHeader:
            def __init__(self, *args, **kwargs):
                pass

            def encode(self):
                return encoded

        monkeypatch.setattr(draft_base, "Header", FakeHeader)
        with pytest.raises(DraftMessageInvalidError):
            build_draft_mime(_message(subject="=?utf-8?q?X?="), date=NOW)

    @pytest.mark.parametrize("encoded", list(VALID_ENCODED.values()), ids=list(VALID_ENCODED))
    def test_strictly_valid_payload_is_compliant(self, encoded):
        for word in encoded.split():
            _strict_decode_word(word)
        assert draft_base._is_rfc2047_compliant(encoded) is True

    @pytest.mark.parametrize(
        "subject", list(SUBJECT_ENCODED_PATH.values()), ids=list(SUBJECT_ENCODED_PATH)
    )
    def test_generated_output_passes_strict_payload_validation(self, subject):
        encoded = draft_base.Header(subject, "utf-8", maxlinelen=76, header_name="Subject").encode()
        assert draft_base._is_rfc2047_compliant(encoded) is True
        decoded = "".join(_strict_decode_word(word) for word in encoded.split())
        assert decoded == subject

    @pytest.mark.parametrize("subject", list(SUBJECT_REJECT.values()), ids=list(SUBJECT_REJECT))
    def test_raw_control_subjects_are_refused(self, subject):
        with pytest.raises(DraftMessageInvalidError):
            build_draft_mime(_message(subject=subject), date=NOW)

    def test_ordinary_subject_wire_form_is_unchanged(self):
        data = build_draft_mime(_message(), date=NOW)
        assert b"\r\nSubject: Bewerbung als Junior Python Developer\r\n" in data

    def test_a_subject_that_does_not_round_trip_fails_closed(self, monkeypatch):
        # Backstop: whatever the encoding step yields, the FINAL decoded
        # subject must equal the approved one -- else a pre-APPEND failure.
        monkeypatch.setattr(draft_base, "_subject_header_value", lambda subject: subject)
        with pytest.raises(DraftMessageInvalidError):
            build_draft_mime(_message(subject=SUBJECT_ACCEPT_EXACT["q-encoded-word"]), date=NOW)

    def test_dto_reprs_leak_nothing(self):
        texts = " ".join(
            repr(value)
            for value in (
                _message(),
                _target(),
                ReconcileTarget(_target(), MARKER),
                draft_base.DraftCreateResult(7, 42),
                draft_base.DraftLookupResult(7, (42,)),
                _provider(FakeClient()),
            )
        )
        for secret in (ACCOUNT, MARKER, "Bewerbung", "Drafts", "42", "app-password-secret"):
            assert secret not in texts


# --- create_draft ----------------------------------------------------------------------


class TestCreateDraft:
    def test_ok_with_appenduid(self):
        client = FakeClient()
        result = _create(client)
        assert (result.uid_validity, result.uid) == (7, 42)
        appends = [entry for entry in client.trace if entry[0] == "APPEND"]
        assert appends == [("APPEND", '"[Gmail]/Drafts"', "(\\Draft)", None)]
        assert client.commands() == ["LOGIN", "LIST", "RESPONSE", "APPEND", "RESPONSE", "LOGOUT"]
        parsed = BytesParser(policy=policy.SMTP).parsebytes(client.appended[0])
        assert parsed["To"] is None and str(parsed["Message-ID"]) == MARKER

    @pytest.mark.parametrize(
        ("append_result", "appenduid"),
        [
            (("OK", [b"(Success)"]), None),  # no UIDPLUS
            (("OK", [b"[APPENDUID 7 1:3] (Success)"]), (b"7 1:3",)),  # range
            (("OK", [b"[APPENDUID 0 42] (Success)"]), (b"0 42",)),
            (("OK", [b"[APPENDUID 7 4294967296] (Success)"]), (b"7 4294967296",)),
            (("OK", [b"[APPENDUID x y] (Success)"]), (b"x y",)),
            (("OK", [b"(Success)"]), (b"7 42", b"7 43")),  # multiple
            (("OK", [b"[APPENDUID 8 42] (Success)"]), (b"7 42",)),  # conflicting
        ],
    )
    def test_ok_without_usable_appenduid_is_still_created(self, append_result, appenduid):
        result = _create(FakeClient(append_result=append_result, appenduid=appenduid))
        assert (result.uid_validity, result.uid) == (None, None)

    def test_stale_appenduid_is_drained_and_never_attributed(self):
        client = FakeClient(
            stale_appenduid=b"1 1", append_result=("OK", [b"(Success)"]), appenduid=None
        )
        result = _create(client)
        assert (result.uid_validity, result.uid) == (None, None)
        assert client.trace.index(("RESPONSE", "APPENDUID")) < client.commands().index("APPEND")

    def test_logout_failure_after_ok_is_still_created(self):
        result = _create(FakeClient(logout_exc=OSError("reset")))
        assert result.uid == 42

    def test_tagged_no_is_a_definite_rejection(self):
        client = FakeClient(append_result=("NO", [b"[TRYCREATE] no such mailbox"]))
        with pytest.raises(DraftCreateRejectedError):
            _create(client)
        assert client.commands().count("APPEND") == 1

    @pytest.mark.parametrize(
        "exc",
        [
            imaplib.IMAP4.error("APPEND command error: BAD [b'x']"),
            imaplib.IMAP4.abort("socket error"),
            OSError("connection reset"),
            TimeoutError("timed out"),
            ValueError("parse"),
            RuntimeError("anything"),
        ],
    )
    def test_post_invocation_failures_are_outcome_unknown(self, exc):
        client = FakeClient(append_exc=exc)
        with pytest.raises(DraftCreateOutcomeUnknownError) as raised:
            _create(client)
        assert client.commands().count("APPEND") == 1
        assert raised.value.__cause__ is None and str(exc) not in str(raised.value)

    @pytest.mark.parametrize("typ", ["BAD", "PREAUTH", None])
    def test_unexpected_tagged_type_is_outcome_unknown(self, typ):
        with pytest.raises(DraftCreateOutcomeUnknownError):
            _create(FakeClient(append_result=(typ, [b"x"])))

    def test_kill_switch_refuses_before_any_connection(self):
        calls = []
        provider = _provider(FakeClient(), enabled=False, factory_calls=calls)
        with pytest.raises(DraftsDisabledError):
            provider.create_draft(_message(), _target(), NOW + timedelta(seconds=60))
        assert calls == []

    def test_budget_exhausted_before_connecting(self):
        calls = []
        provider = _provider(FakeClient(), factory_calls=calls)
        with pytest.raises(DraftBudgetExhaustedError):
            provider.create_draft(_message(), _target(), NOW + timedelta(seconds=5))
        assert calls == []

    def test_budget_exhausted_after_list_never_appends(self):
        clock = Clock()
        client = FakeClient()
        client.on_list = lambda: setattr(clock, "now", NOW + timedelta(seconds=56))
        with pytest.raises(DraftBudgetExhaustedError):
            _create(client, clock=clock)
        assert "APPEND" not in client.commands()

    def test_socket_timeout_never_exceeds_the_remaining_budget(self):
        calls = []
        _provider(FakeClient(), factory_calls=calls).create_draft(
            _message(), _target(), NOW + timedelta(seconds=12)
        )
        assert calls == [12.0]

    @pytest.mark.parametrize(
        "client",
        [
            FakeClient(login_exc=imaplib.IMAP4.error("bad credentials")),
            FakeClient(login_result=("NO", [b"x"])),
        ],
    )
    def test_login_failure_is_definite_and_never_appends(self, client):
        with pytest.raises(DraftAuthError):
            _create(client)
        assert "APPEND" not in client.commands()

    def test_connect_failure_is_definite(self):
        def factory(deadline, timeout):
            raise OSError("dns")

        provider = GmailImapDraftProvider(
            "h", 993, ACCOUNT, "pw", drafts_enabled=True, client_factory=factory, clock=Clock()
        )
        with pytest.raises(DraftConnectError):
            provider.create_draft(_message(), _target(), NOW + timedelta(seconds=60))

    @pytest.mark.parametrize(
        "client",
        [
            FakeClient(list_result=("NO", [None])),
            FakeClient(list_result=("OK", [b'(\\HasNoChildren) "/" "[Gmail]/Drafts"'])),
            FakeClient(list_result=("OK", [b"garbage"])),
            FakeClient(list_result=("OK", [None])),
        ],
    )
    def test_target_verification_failure_is_definite_and_never_appends(self, client):
        with pytest.raises(DraftsMailboxInvalidError):
            _create(client)
        assert "APPEND" not in client.commands()

    def test_list_raising_is_definite(self):
        client = FakeClient()
        client.on_list = lambda: (_ for _ in ()).throw(imaplib.IMAP4.abort("x"))
        with pytest.raises(DraftCreateDefiniteError):
            _create(client)
        assert "APPEND" not in client.commands()

    def test_frozen_target_of_another_account_is_refused(self):
        client = FakeClient()
        provider = _provider(client)
        with pytest.raises(DraftsMailboxInvalidError):
            provider.create_draft(
                _message(), _target(account="other@example.com"), NOW + timedelta(seconds=60)
            )
        assert client.trace == []

    def test_tampered_wire_is_refused_before_connecting(self):
        client = FakeClient()
        target = DraftTarget(ACCOUNT, "[Gmail]/Drafts", '"[Gmail]/Sent Mail"')
        with pytest.raises(DraftsMailboxInvalidError):
            _provider(client).create_draft(_message(), target, NOW + timedelta(seconds=60))
        assert client.trace == []

    def test_invalid_message_is_definite_before_connecting(self):
        client = FakeClient()
        with pytest.raises(DraftMessageInvalidError):
            _provider(client).create_draft(
                _message(subject="x\r\nBcc: a@b.c"), _target(), NOW + timedelta(seconds=60)
            )
        assert client.trace == []

    def test_frozen_localized_target_is_used_byte_identically(self):
        lines = [b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Entw&APw-rfe"']
        client = FakeClient(list_result=("OK", lines))
        _provider(client).create_draft(
            _message(), _target("[Gmail]/Entwürfe"), NOW + timedelta(seconds=60)
        )
        assert ("APPEND", '"[Gmail]/Entw&APw-rfe"', "(\\Draft)", None) in client.trace

    def test_every_call_issues_only_allowed_commands_and_one_append(self):
        for client in (
            FakeClient(),
            FakeClient(append_result=("NO", [b"x"])),
            FakeClient(append_exc=OSError("x")),
        ):
            try:
                _create(client)
            except draft_base.DraftProviderError:
                pass
            assert set(client.commands()) <= ALLOWED_COMMANDS
            assert client.commands().count("APPEND") == 1


# --- find_by_message_id ------------------------------------------------------------------


def _lookup(client, *, marker=MARKER, mailbox="[Gmail]/Drafts", clock=None):
    provider = _provider(client, clock=clock)
    return provider.find_by_message_id(
        ReconcileTarget(_target(mailbox), marker), NOW + timedelta(seconds=30)
    )


class TestLookup:
    def test_one_match(self):
        client = FakeClient()
        result = _lookup(client)
        assert (result.uid_validity, result.uids) == (7, (42,))
        assert client.commands() == ["LOGIN", "LIST", "EXAMINE", "RESPONSE", "UID", "LOGOUT"]
        assert ("UID", "SEARCH", "HEADER", "Message-ID", f'"{MARKER}"') in client.trace
        assert ("EXAMINE", '"[Gmail]/Drafts"') in client.trace

    @pytest.mark.parametrize(("line", "uids"), [(b"", ()), (b"3 9", (3, 9))])
    def test_zero_and_many(self, line, uids):
        assert _lookup(FakeClient(search_result=("OK", [line]))).uids == uids

    @pytest.mark.parametrize(
        "client",
        [
            FakeClient(search_result=("NO", [b"x"])),
            FakeClient(search_result=("OK", [None])),  # partial: no untagged SEARCH
            FakeClient(search_result=("OK", [b"1", b"2"])),
            FakeClient(search_result=("OK", [b"1 x"])),
            FakeClient(search_result=("OK", [b"0"])),
            FakeClient(search_result=("OK", [b"4294967296"])),
            FakeClient(search_result=("OK", [b"5 5"])),
            FakeClient(search_result=("OK", [b"1  2"])),
            FakeClient(select_result=("NO", [b"x"])),
            FakeClient(uidvalidity=None),
            FakeClient(uidvalidity=(b"0",)),
            FakeClient(uidvalidity=(b"7", b"8")),
            FakeClient(list_result=("OK", [b'(\\HasNoChildren) "/" "[Gmail]/Drafts"'])),
            FakeClient(list_result=("OK", [None])),  # original target renamed/gone
            FakeClient(login_exc=OSError("x")),
        ],
    )
    def test_every_failure_is_an_error_never_an_empty_result(self, client):
        with pytest.raises(DraftLookupError):
            _lookup(client)
        assert "APPEND" not in client.commands()

    def test_malformed_marker_and_unusable_target_never_connect(self):
        client = FakeClient()
        with pytest.raises(DraftLookupError):
            _lookup(client, marker="<x@example.com>")
        provider = _provider(client)
        bad = ReconcileTarget(DraftTarget(ACCOUNT, "[Gmail]/Drafts", '"other"'), MARKER)
        with pytest.raises(DraftLookupError):
            provider.find_by_message_id(bad, NOW + timedelta(seconds=30))
        assert client.trace == []

    def test_lookup_is_independent_of_the_kill_switch_but_read_only(self):
        client = FakeClient()
        result = _provider(client, enabled=False).find_by_message_id(
            ReconcileTarget(_target(), MARKER), NOW + timedelta(seconds=30)
        )
        assert result.uids == (42,)
        assert set(client.commands()) <= ALLOWED_COMMANDS - {"APPEND"}


# --- real connection wiring and static safety -----------------------------------------------


def test_default_connection_uses_verified_tls_bounded_timeout_and_no_debug(monkeypatch):
    created = {}

    class FakeSSL(FakeClient):
        def __init__(self, host, port, *, ssl_context, timeout, deadline):
            super().__init__()
            self.debug = 4
            created.update(host=host, ctx=ssl_context, timeout=timeout, deadline=deadline)

    monkeypatch.setattr(imap_draft, "DeadlineIMAP4SSL", FakeSSL)
    provider = GmailImapDraftProvider(
        "imap.example.com", 993, ACCOUNT, "pw", drafts_enabled=True, clock=Clock()
    )
    provider.create_draft(_message(), _target(), NOW + timedelta(seconds=60))
    assert created["ctx"].verify_mode == ssl.CERT_REQUIRED and created["ctx"].check_hostname
    assert created["timeout"] == imap_draft.IMAP_OPERATION_TIMEOUT_SECONDS
    assert created["deadline"] is not None


_FORBIDDEN_CALLS = {
    "store",
    "expunge",
    "copy",
    "move",
    "delete",
    "create",
    "rename",
    "fetch",
    "close",
    "send",
    "sendmail",
    "send_message",
    "subscribe",
    "setacl",
}
_FORBIDDEN_IMPORTS = (
    "smtplib",
    "httpx",
    "requests",
    "urllib",
    "aiohttp",
    "google",
    "googleapiclient",
)


@pytest.mark.parametrize(
    "relative", ["app/providers/email/imap_draft.py", "app/providers/email/draft_base.py"]
)
def test_static_no_forbidden_imports_or_mailbox_mutations(relative):
    tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(_FORBIDDEN_IMPORTS), alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(_FORBIDDEN_IMPORTS), node.module
            assert "smtp" not in node.module and "outbound" not in node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr.lower() not in _FORBIDDEN_CALLS, node.func.attr
            if node.func.attr == "uid":
                assert isinstance(node.args[0], ast.Constant) and node.args[0].value == "SEARCH"
            if node.func.attr == "select":
                assert any(
                    kw.arg == "readonly" and getattr(kw.value, "value", None) is True
                    for kw in node.keywords
                )
