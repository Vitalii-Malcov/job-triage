"""Stage 9D: the APPEND-only Gmail Drafts provider -- modified UTF-7 codec,
LIST tokenizer/verification, MIME contract, APPEND result classification,
read-only lookup parsing, and the allowed-command trace. No network: a
fake client records every IMAP command."""

import ast
import imaplib
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
