"""XING email digest collector (IMAP + App Password).

=====================================================================
HARD SECURITY CONSTRAINT — DO NOT VIOLATE. THIS IS NOT TECH DEBT.
=====================================================================
XING job-digest emails embed a per-recipient tracking redirect as each
posting's link, formatted like:

    => https://www.xing.com/m/xxxxxxxxxxxxxxxxxx

Making ANY HTTP request to one of these URLs — GET, HEAD, following a
redirect, "just checking" it resolves, or anything else — is not an inert
read. It is a real action with a side effect on a third party: XING notifies
the recruiter who posted the job that the candidate viewed the listing. A
job search assistant silently generating "candidate viewed your job" events
without the candidate's knowledge or intent would be a serious, surprising
behavior for the user.

This module NEVER makes an outbound network/HTTP request to a URL extracted
from an email — not in fetch(), not in any helper, not for "resolving" or
"validating" the link. The only network I/O this module performs is IMAP
against the user's own configured mailbox (imap_host/imap_port). The
tracking URL is captured verbatim into `Job.url` purely as an opaque
reference string, for a human to open deliberately if they choose to.

Concretely: this module has no dependency on httpx, requests, aiohttp,
urllib, or any other HTTP client — none of those names appear anywhere in
this file. tests/test_collectors_xing_email.py asserts this by inspecting
this module's source, not just by testing behavior with a mocked client —
an HTTP call by this module would be a bug even if some future change added
an HTTP dependency for an unrelated reason.

See CLAUDE.md's Implementation rules for the project-wide version of this
rule (it applies to every future email/RSS/content-ingesting collector, not
just this one).
=====================================================================

Beyond that constraint, this collector:
- Reads the mailbox via IMAP4_SSL, SELECTed read-only — never marks
  messages read/unread, never deletes, never writes to the mailbox at all.
- Filters to the two known XING job-digest Subject patterns from
  jobs@mail.xing.com; any other subject or sender (e.g. "Wochencheck" from
  mailrobot@mail.xing.com, or news@mail.xing.com) is logged as skipped, not
  treated as an error.
- Parses the plaintext body (not HTML — confirmed substantially cleaner and
  more stable across the two observed digest formats) into per-posting
  blocks, separated by one or more consecutive lines of dashes.
- Tracks which Message-IDs have already been processed via the
  ProcessedEmailMessage table (app/db/repositories.is_message_processed /
  mark_message_processed) instead of mutating the mailbox's read state.
"""

import asyncio
import email
import imaplib
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from typing import Protocol

from pydantic import ValidationError

from app.collectors.base import CollectorError, JobCollector, is_configured
from app.models.job import Job

logger = logging.getLogger(__name__)

SOURCE_NAME = "xing"
XING_DIGEST_SENDER = "jobs@mail.xing.com"

# Both observed digest subject formats. Anything else from the same sender
# domain (e.g. "Wochencheck" from mailrobot@, industry news from news@) is
# not a job digest and must be skipped, not parsed.
_SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d+\s+neue\s+Stellenangebote\s+für\b", re.IGNORECASE),
    re.compile(r"^Entdecke\s+ähnliche\s+Jobs\s+wie\b", re.IGNORECASE),
)

# One or more consecutive separator lines act as a single block boundary —
# real digests observed with both one and two stacked dash lines between
# postings. 10+ dashes distinguishes a separator line from any incidental
# hyphen usage inside posting text.
_SEPARATOR_RE = re.compile(r"(?:^[ \t]*-{10,}[ \t]*\r?\n)+", re.MULTILINE)

# The tracking-redirect line is the one stable structural anchor in a
# posting block — the marketing badge above the title is optional and its
# text is unconstrained, so we anchor on this line and take the title as
# "whatever line immediately precedes it" rather than assuming a fixed
# line count from the top of the block.
_TRACKING_LINE_RE = re.compile(r"^=>\s*(https?://\S+)\s*$")

# Matches only the numeric "X € - Y €" salary range itself. Real emails
# render UI badges (e.g. "bevorzugtesTätigkeitsfeldKarriere-Stufe")
# concatenated directly onto this line with no separator — those are
# decorative and have no stable schema, so they're deliberately ignored
# rather than parsed as structured fields.
_SALARY_RE = re.compile(r"\d[\d.,]*\s*€\s*-\s*\d[\d.,]*\s*€")

# Closed set of real XING/German-job-board employment-type values. When a
# posting has no salary line, its decorative tail badges (e.g.
# "Karriere-Stufe", "bevorzugtes Tätigkeitsfeld") land on their own separate
# lines instead of being concatenated onto the salary line — with nothing to
# distinguish them positionally from the real employment type. A real
# digest for "(Junior) Consultant AI Security & Governance" at KPMG had
# exactly this shape ("Karriere-Stufe" then "Vollzeit", no salary line), and
# treating "the first non-salary tail line" as employment_type silently
# picked "Karriere-Stufe" and dropped the real "Vollzeit". Matching against
# a closed vocabulary instead of a heuristic means an unrecognized badge is
# just dropped (no `description` field) rather than reported as a wrong
# employment type.
_KNOWN_EMPLOYMENT_TYPES = frozenset(
    {
        "vollzeit",
        "teilzeit",
        "werkstudent",
        "praktikum",
        "ausbildung",
        "freelance",
        "minijob",
    }
)


class XingAuthError(CollectorError):
    """Raised when IMAP login is rejected (bad username/App Password), or
    when the mailbox is not configured at all. Not retried — retrying with
    the same credentials cannot succeed.
    """


class XingConnectionError(CollectorError):
    """Raised when the IMAP server can't be reached, or an IMAP command
    other than login fails (e.g. SELECT/SEARCH).
    """


class ImapClient(Protocol):
    """The subset of imaplib.IMAP4_SSL's interface this collector uses.

    Exists so tests can inject a lightweight fake instead of opening a real
    IMAP connection — see BundesagenturCollector's `http_client` parameter
    for the same pattern applied to HTTP instead of IMAP.
    """

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]: ...

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]: ...

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]: ...

    def fetch(self, message_set: str, message_parts: str) -> tuple[str, list]: ...

    def close(self) -> tuple[str, list[bytes]]: ...

    def logout(self) -> tuple[str, list[bytes]]: ...


def _decode_subject(raw_subject: str) -> str:
    if not raw_subject:
        return ""
    decoded_parts = []
    for text, encoding in decode_header(raw_subject):
        if isinstance(text, bytes):
            decoded_parts.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_parts.append(text)
    return "".join(decoded_parts)


def _is_job_digest_subject(subject: str) -> bool:
    return any(pattern.search(subject) for pattern in _SUBJECT_PATTERNS)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_plaintext_body(msg: Message) -> str:
    # Plaintext is used instead of the HTML part on purpose — confirmed
    # against real digest emails to be substantially cleaner (no markup,
    # no tracking pixels/CSS noise) and structurally identical across both
    # observed digest formats.
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disposition:
                return _decode_part(part)
        return ""
    if msg.get_content_type() == "text/plain":
        return _decode_part(msg)
    return ""


def _split_blocks(body: str) -> list[str]:
    return [block.strip() for block in _SEPARATOR_RE.split(body) if block.strip()]


def _parse_block(block: str) -> Job | None:
    lines = [line.strip() for line in block.splitlines() if line.strip()]

    tracking_index = None
    tracking_url = None
    for idx, line in enumerate(lines):
        match = _TRACKING_LINE_RE.match(line)
        if match:
            tracking_index = idx
            tracking_url = match.group(1)
            break

    if tracking_index is None or tracking_index == 0:
        # No tracking line at all (not a posting block — e.g. the "Alle
        # Suchergebnisse anzeigen" footer) or nothing before it to use as
        # a title. Either way, not a parseable posting.
        logger.warning("xing_email_skipped_invalid_block reason=no_title_before_tracking_line")
        return None

    title = lines[tracking_index - 1]
    remaining = lines[tracking_index + 1 :]
    if len(remaining) < 2:
        logger.warning(
            "xing_email_skipped_invalid_block reason=missing_company_or_location title=%s",
            title,
        )
        return None

    company, location = remaining[0], remaining[1]

    salary = None
    employment_type = None
    for line in remaining[2:]:
        salary_match = _SALARY_RE.search(line)
        if salary_match and salary is None:
            salary = salary_match.group(0)
        elif employment_type is None and line.strip().casefold() in _KNOWN_EMPLOYMENT_TYPES:
            employment_type = line

    description_parts = []
    if salary:
        description_parts.append(f"Gehalt: {salary}")
    if employment_type:
        description_parts.append(f"Beschäftigung: {employment_type}")

    try:
        return Job(
            source=SOURCE_NAME,
            title=title,
            company=company,
            location=location,
            # Tracking redirect stored verbatim for audit/reference only.
            # NEVER resolved or requested by this collector or any caller
            # of it — see the module docstring's hard security constraint.
            url=tracking_url,
            description="; ".join(description_parts),
        )
    except ValidationError:
        logger.warning(
            "xing_email_skipped_invalid_block reason=validation_error title=%s company=%s",
            title,
            company,
        )
        return None


class XingEmailCollector(JobCollector):
    """Collector for XING job-digest emails delivered to a mailbox via IMAP.

    Only fetches and maps postings into `Job` — it never writes to the jobs
    database and never scores jobs (see app/api/routes.py for that). It does
    read from and write to the separate ProcessedEmailMessage table via the
    injected `is_message_processed`/`mark_message_processed` callables, so
    it can skip already-parsed emails without mutating the mailbox itself —
    see the module docstring.
    """

    source = SOURCE_NAME

    def __init__(
        self,
        imap_host: str,
        imap_port: int,
        username: str,
        app_password: str,
        lookback_days: int = 7,
        imap_client: ImapClient | None = None,
        is_message_processed: Callable[[str], bool] | None = None,
        mark_message_processed: Callable[[str], None] | None = None,
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.app_password = app_password
        self.lookback_days = lookback_days
        # Injected only by tests, to avoid a real IMAP connection; production
        # code always opens (and closes) its own connection in _fetch_sync.
        self._injected_client = imap_client
        # Callables rather than a raw db.Session: keeps this collector
        # decoupled from SQLAlchemy (matching JobCollector's "never writes
        # to the database" spirit for the *jobs* table) while still letting
        # the caller (POST /collectors/xing/run) wire it to the real
        # ProcessedEmailMessage repository functions bound to its request's
        # Session. Default to "nothing is ever processed" so unit tests that
        # don't care about dedup don't need to wire anything.
        self._is_message_processed = is_message_processed or (lambda _message_id: False)
        self._mark_message_processed = mark_message_processed or (lambda _message_id: None)
        # Set on every fetch() call; read by callers after awaiting fetch()
        # to report how many blocks were skipped (mirrors
        # BundesagenturCollector.skipped_invalid_count).
        self.skipped_invalid_count = 0

    async def fetch(self, since: datetime | None = None) -> list[Job]:
        if not is_configured(self.username) or not is_configured(self.app_password):
            raise XingAuthError(
                "XING_MAILBOX_USERNAME / XING_MAILBOX_APP_PASSWORD is not configured"
            )

        self.skipped_invalid_count = 0
        since_date = since or (datetime.now(UTC) - timedelta(days=self.lookback_days))

        # IMAP (imaplib) is synchronous/blocking; run it off the event loop
        # via a worker thread so fetch() honors JobCollector's async
        # contract without blocking other requests for the duration of the
        # IMAP session. imaplib (stdlib) was chosen over aioimaplib (a
        # third-party, less mature dependency) — this collector runs
        # manually and infrequently (like Bundesagentur's), so native
        # asyncio I/O isn't worth the extra dependency here.
        #
        # Note: the injected is_message_processed/mark_message_processed
        # callables (bound to the caller's db.Session — see
        # app/api/routes.py) get invoked from this worker thread. That's
        # safe here only because the route handler awaits this call and
        # does not touch `db` concurrently while it's in flight.
        return await asyncio.to_thread(self._fetch_sync, since_date)

    def _fetch_sync(self, since: datetime) -> list[Job]:
        client = self._injected_client
        owns_connection = client is None
        if client is None:
            client = self._connect()

        try:
            typ, _ = client.select("INBOX", readonly=True)
            if typ != "OK":
                raise XingConnectionError(f"IMAP SELECT failed: {typ}")

            criteria = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
            typ, data = client.search(None, criteria)
            if typ != "OK":
                raise XingConnectionError(f"IMAP SEARCH failed: {typ}")

            message_numbers = data[0].split() if data and data[0] else []
            jobs: list[Job] = []
            for message_number in message_numbers:
                jobs.extend(self._fetch_and_process_message(client, message_number))
            return jobs
        finally:
            if owns_connection:
                self._disconnect(client)

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        except OSError as exc:
            raise XingConnectionError(
                f"Could not connect to {self.imap_host}:{self.imap_port}: {exc}"
            ) from exc

        try:
            client.login(self.username, self.app_password)
        except imaplib.IMAP4.error as exc:
            raise XingAuthError(f"XING mailbox IMAP login rejected: {exc}") from exc
        return client

    def _disconnect(self, client: ImapClient) -> None:
        try:
            client.close()
        except Exception:
            logger.warning("xing_email_imap_close_failed", exc_info=True)
        try:
            client.logout()
        except Exception:
            logger.warning("xing_email_imap_logout_failed", exc_info=True)

    def _fetch_and_process_message(self, client: ImapClient, message_number: bytes) -> list[Job]:
        typ, msg_data = client.fetch(message_number, "(RFC822)")
        if typ != "OK" or not msg_data or msg_data[0] is None:
            logger.warning("xing_email_message_fetch_failed message_number=%s", message_number)
            return []

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        return self._process_message(msg)

    def _process_message(self, msg: Message) -> list[Job]:
        sender = parseaddr(msg.get("From", ""))[1].casefold()
        if sender != XING_DIGEST_SENDER:
            return []

        subject = _decode_subject(msg.get("Subject", ""))
        if not _is_job_digest_subject(subject):
            logger.info("xing_email_skipped_subject subject=%s", subject)
            return []

        message_id = (msg.get("Message-ID") or "").strip()
        if not message_id:
            logger.warning("xing_email_skipped_missing_message_id subject=%s", subject)
            return []
        if self._is_message_processed(message_id):
            return []

        body = _extract_plaintext_body(msg)
        if not body:
            logger.warning("xing_email_no_plaintext_body message_id=%s", message_id)
            self._mark_message_processed(message_id)
            return []

        jobs: list[Job] = []
        for block in _split_blocks(body):
            job = _parse_block(block)
            if job is None:
                self.skipped_invalid_count += 1
            else:
                jobs.append(job)

        if not jobs:
            logger.warning("xing_email_no_valid_job_blocks message_id=%s", message_id)

        self._mark_message_processed(message_id)
        return jobs
