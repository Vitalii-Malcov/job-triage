import inspect
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

import app.collectors.xing_email as xing_email_module
from app.collectors.xing_email import (
    XingAuthError,
    XingEmailCollector,
)

XING_SENDER = "jobs@mail.xing.com"

SEPARATOR = "-" * 40

BLOCK_WITH_ALL_OPTIONAL_FIELDS = """Bis 18% mehr Gehalt
(Junior) Software Entwickler (m/w/d)
=> https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1

CAREL Deutschland GmbH
Gelnhausen

44.000 € - 59.000 €bevorzugtesTätigkeitsfeldKarriere-Stufe
Vollzeit"""

BLOCK_WITHOUT_OPTIONAL_FIELDS = """Junior Informatiker (m/w/d)
=> https://www.xing.com/m/BBBBBBBBBBBBBBBBBBBB2

Institut für Kommunikations- und Prüfungsforschung gGmbH
Heidelberg"""

FOOTER_BLOCK = """Alle Suchergebnisse anzeigen
=> https://www.xing.com/jobs/search?query=python"""

# Real digest shape for KPMG's "(Junior) Consultant AI Security &
# Governance" posting: no salary line at all, so the decorative
# "Karriere-Stufe" badge and the real employment type each land on their
# own line instead of being concatenated onto a salary line.
KPMG_BLOCK = """(Junior) Consultant AI Security & Governance
=> https://www.xing.com/m/CCCCCCCCCCCCCCCCCCCC3

KPMG
Frankfurt am Main

Karriere-Stufe
Vollzeit"""

BLOCK_WITH_ONLY_DECORATIVE_TAGS = """Werkstudent Data Engineering
=> https://www.xing.com/m/DDDDDDDDDDDDDDDDDDDD4

Example Analytics GmbH
Munich

Karriere-Stufe
bevorzugtes Tätigkeitsfeld"""


def _digest_body(*blocks: str, separator_repeat: int = 1) -> str:
    separator = ("\n" + SEPARATOR) * separator_repeat
    return ("\n" + separator + "\n").join(["Hallo, hier sind deine neuen Jobs:", *blocks, ""])


def _build_email(
    sender: str,
    subject: str,
    plaintext_body: str,
    message_id: str = "<default@mail.xing.com>",
    html_body: str | None = None,
) -> bytes:
    if html_body is not None:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(plaintext_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(plaintext_body, "plain", "utf-8")
    msg["From"] = sender
    msg["Subject"] = Header(subject, "utf-8").encode()
    msg["Message-ID"] = message_id
    return msg.as_bytes()


class FakeImapClient:
    """Minimal fake matching the subset of imaplib.IMAP4_SSL this collector
    uses. No real socket/network I/O anywhere in this class.
    """

    def __init__(self, messages: list[bytes]) -> None:
        self._messages = messages
        self.select_calls: list[tuple[str, bool]] = []
        self.search_calls: list[tuple[str, ...]] = []
        self.closed = False
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return ("OK", [str(len(self._messages)).encode()])

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
        self.search_calls.append(criteria)
        numbers = b" ".join(str(i + 1).encode() for i in range(len(self._messages)))
        return ("OK", [numbers])

    def fetch(self, message_set, message_parts: str) -> tuple[str, list]:
        index = int(message_set) - 1
        raw = self._messages[index]
        return ("OK", [(b"1 (RFC822 {%d}" % len(raw), raw)])

    def close(self) -> tuple[str, list[bytes]]:
        self.closed = True
        return ("OK", [b"CLOSE completed"])

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("OK", [b"BYE"])


def _make_collector(messages: list[bytes], **kwargs) -> tuple[XingEmailCollector, FakeImapClient]:
    client = FakeImapClient(messages)
    collector = XingEmailCollector(
        imap_host="imap.example.com",
        imap_port=993,
        username="user@example.com",
        app_password="app-password",
        imap_client=client,
        **kwargs,
    )
    return collector, client


# ---------------------------------------------------------------------------
# Hard security constraint: no HTTP client anywhere in this module.
# ---------------------------------------------------------------------------


def test_module_never_imports_an_http_client():
    """The tracking URLs in XING digest emails are personal recruiter-view
    redirects (see module docstring) — resolving them is a real side effect
    on a third party, not an inert read. This collector must not have the
    means to make an HTTP request at all: no httpx/requests/aiohttp/urllib
    import anywhere in the module.
    """
    source = inspect.getsource(xing_email_module)
    forbidden_imports = ["httpx", "requests", "aiohttp", "urllib.request", "http.client"]
    for name in forbidden_imports:
        assert f"import {name}" not in source, f"module must not import {name}"

    for name in ("httpx", "requests", "aiohttp"):
        assert not hasattr(xing_email_module, name)


@pytest.mark.asyncio
async def test_tracking_url_is_stored_verbatim_and_never_fetched():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "3 neue Stellenangebote für Python", body)
    collector, client = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert str(jobs[0].url) == "https://www.xing.com/m/BBBBBBBBBBBBBBBBBBBB2"
    # FakeImapClient never performs any HTTP call by construction (it only
    # implements IMAP methods) — the only "requests" that happened at all
    # were IMAP protocol calls to the mocked mailbox, not to the job URL.


# ---------------------------------------------------------------------------
# Subject / sender filtering.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_processes_both_known_digest_subject_patterns():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw1 = _build_email(
        XING_SENDER,
        "3 neue Stellenangebote für Python Entwickler in Heidelberg",
        body,
        message_id="<digest-1@mail.xing.com>",
    )
    raw2 = _build_email(
        XING_SENDER,
        "Entdecke ähnliche Jobs wie Junior Informatiker (m/w/d)",
        body,
        message_id="<digest-2@mail.xing.com>",
    )
    collector, _ = _make_collector([raw1, raw2])

    jobs = await collector.fetch()

    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_skips_non_job_digest_subjects_and_senders_without_crashing():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    wochencheck = _build_email(
        "mailrobot@mail.xing.com", "Dein Wochencheck", body, message_id="<w@mail.xing.com>"
    )
    news = _build_email(
        "news@mail.xing.com", "Neuigkeiten aus deiner Branche", body, message_id="<n@mail.xing.com>"
    )
    unrelated_subject = _build_email(
        XING_SENDER, "Dein XING-Profil wurde besucht", body, message_id="<v@mail.xing.com>"
    )
    collector, _ = _make_collector([wochencheck, news, unrelated_subject])

    jobs = await collector.fetch()

    assert jobs == []


# ---------------------------------------------------------------------------
# Block parsing: variable separators, optional fields, invalid blocks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parses_block_with_all_optional_fields_present():
    body = _digest_body(BLOCK_WITH_ALL_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Software Entwickler", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "(Junior) Software Entwickler (m/w/d)"
    assert job.company == "CAREL Deutschland GmbH"
    assert job.location == "Gelnhausen"
    assert str(job.url) == "https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1"
    assert "44.000" in job.description
    assert "Vollzeit" in job.description


@pytest.mark.asyncio
async def test_parses_block_with_no_optional_fields_present():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "2 neue Stellenangebote für Informatiker", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Junior Informatiker (m/w/d)"
    assert job.company == "Institut für Kommunikations- und Prüfungsforschung gGmbH"
    assert job.location == "Heidelberg"
    assert job.description == ""


@pytest.mark.asyncio
async def test_parses_block_with_no_salary_and_standalone_occupation_badge():
    # Real KPMG digest shape: no salary line, so "Karriere-Stufe" (decorative)
    # and "Vollzeit" (real employment type) each sit on their own line. The
    # employment type must be matched against the known-values list, not
    # taken as "the first non-salary tail line" — that bug picked
    # "Karriere-Stufe" and lost "Vollzeit" entirely.
    body = _digest_body(KPMG_BLOCK)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Consultant", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "(Junior) Consultant AI Security & Governance"
    assert job.company == "KPMG"
    assert job.location == "Frankfurt am Main"
    assert job.description == "Beschäftigung: Vollzeit"
    assert "Karriere-Stufe" not in job.description
    assert "Karriere-Stufe" not in job.title
    assert "Karriere-Stufe" not in job.company
    assert "Karriere-Stufe" not in job.location


@pytest.mark.asyncio
async def test_parses_block_with_multiple_decorative_tags_and_no_employment_type():
    # Tail lines are all decorative badges — none matches a known employment
    # type. The block is still a valid posting (title/company/location are
    # fine); description must simply omit "Beschäftigung: ..." rather than
    # guess at one of the decorative tags.
    body = _digest_body(BLOCK_WITH_ONLY_DECORATIVE_TAGS)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Data Engineering", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Example Analytics GmbH"
    assert job.location == "Munich"
    assert "Beschäftigung:" not in job.description
    assert job.description == ""


@pytest.mark.asyncio
async def test_handles_variable_number_of_separator_lines_between_blocks():
    body = _digest_body(
        BLOCK_WITH_ALL_OPTIONAL_FIELDS, BLOCK_WITHOUT_OPTIONAL_FIELDS, separator_repeat=2
    )
    raw = _build_email(XING_SENDER, "2 neue Stellenangebote für Python", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_skips_footer_block_without_crashing():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS, FOOTER_BLOCK)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    # 2 skips, not just the footer: _digest_body's greeting line before the
    # first separator is itself a "block" with no tracking line, matching
    # real digests where preamble text precedes the first separator too.
    assert collector.skipped_invalid_count == 2


@pytest.mark.asyncio
async def test_email_with_no_valid_job_blocks_yields_empty_list_not_exception():
    # Only the footer/"show all" block and a greeting line — no real posting.
    body = _digest_body(FOOTER_BLOCK)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert jobs == []
    assert collector.skipped_invalid_count >= 1


@pytest.mark.asyncio
async def test_reads_plaintext_part_of_multipart_message():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    html = "<html><body>Some HTML the collector must ignore</body></html>"
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body, html_body=html)
    collector, _ = _make_collector([raw])

    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert jobs[0].company == "Institut für Kommunikations- und Prüfungsforschung gGmbH"


# ---------------------------------------------------------------------------
# Read-only IMAP access.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_is_called_readonly():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(XING_SENDER, "1 neue Stellenangebote für Python", body)
    collector, client = _make_collector([raw])

    await collector.fetch()

    assert client.select_calls == [("INBOX", True)]


# ---------------------------------------------------------------------------
# Message-ID based deduplication.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_message_id_is_not_reprocessed_on_second_fetch():
    body = _digest_body(BLOCK_WITHOUT_OPTIONAL_FIELDS)
    raw = _build_email(
        XING_SENDER,
        "1 neue Stellenangebote für Python",
        body,
        message_id="<digest-1@mail.xing.com>",
    )
    processed: set[str] = set()
    collector, client = _make_collector(
        [raw],
        is_message_processed=lambda message_id: message_id in processed,
    )

    first_batches = await collector.fetch_message_batches()
    first = [job for batch in first_batches for job in batch.jobs]
    assert len(first_batches) == 1

    # A persistence-owning caller acknowledges only after all jobs in this
    # batch succeed. The collector itself deliberately never acknowledges.
    processed.add(first_batches[0].message_id)
    second = await collector.fetch()

    assert len(first) == 1
    assert second == []


# ---------------------------------------------------------------------------
# Auth / configuration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_raises_auth_error_when_not_configured():
    collector = XingEmailCollector(
        imap_host="imap.example.com",
        imap_port=993,
        username="",
        app_password="",
    )

    with pytest.raises(XingAuthError):
        await collector.fetch()
