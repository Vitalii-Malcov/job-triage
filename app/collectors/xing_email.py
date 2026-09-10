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
- Skips Message-IDs already acknowledged in the ProcessedEmailMessage table;
  the persistence-owning caller records acknowledgment only after every job
  parsed from that message has been saved successfully.
"""

import asyncio
import contextlib
import email
import imaplib
import logging
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from typing import Protocol

from pydantic import ValidationError

from app.collectors.base import CollectorError, JobCollector, is_configured
from app.models.job import Job
from app.providers.email.imap_deadline import (
    IMAP_SESSION_DEADLINE_SECONDS,
    DeadlineIMAP4SSL,
    ImapSessionDeadline,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "xing"
XING_DIGEST_SENDER = "jobs@mail.xing.com"

# AUD-005: mirrors app.providers.email.imap.IMAP_OPERATION_TIMEOUT_SECONDS
# -- bounds every blocking socket operation on this collector's IMAP
# connection so a hung/black-holed mailbox server can't stall the worker
# thread (see fetch_message_batches's asyncio.to_thread docstring)
# indefinitely.
IMAP_OPERATION_TIMEOUT_SECONDS = 30.0

# Both observed digest subject formats. Anything else from the same sender
# domain (e.g. "Wochencheck" from mailrobot@, industry news from news@) is
# not a job digest and must be skipped, not parsed.
_SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d+\s+neue\s+Stellenangebote\s+für\b", re.IGNORECASE),
    re.compile(r"^Entdecke\s+ähnliche\s+Jobs\s+wie\b", re.IGNORECASE),
)

# Codex gate follow-up (Astra R4A HIGH): extracts a bare "Message-ID:"
# header value from a HEADER.FIELDS-scoped FETCH response -- see
# `_read_message_id_header`'s own docstring.
_MESSAGE_ID_HEADER_RE = re.compile(rb"Message-ID:\s*(.+)", re.IGNORECASE)

# Codex gate follow-up (Astra R4A MEDIUM, starvation): extracts the
# numeric UIDVALIDITY value from a `STATUS INBOX (UIDVALIDITY)` response
# line -- see `_parse_uid_validity`'s own docstring.
_UID_VALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)")

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


@dataclass(frozen=True)
class XingEmailBatch:
    """Jobs parsed from one XING digest message, kept with its Message-ID.

    `uid` (Codex gate follow-up, Astra R4A MEDIUM starvation fix): the
    message's IMAP UID within this run's `UIDVALIDITY` epoch -- lets
    `app.services.collector_runner.run_xing` compute the contiguous
    confirmed-handled prefix it persists via `advance_xing_scan_progress`
    once it knows whether this batch's jobs actually persisted. Not used
    for deduplication (that is still `message_id`, via
    `ProcessedEmailMessage`) -- purely a scan-position bookkeeping value.
    """

    message_id: str
    jobs: tuple[Job, ...]
    uid: int


def _parse_uid_validity(status_data: list) -> int | None:
    """Extracts the numeric UIDVALIDITY from a `STATUS INBOX
    (UIDVALIDITY)` response. Returns None if it could not be determined
    (some fakes/edge cases) -- callers then treat any previously
    persisted scan watermark as not applicable this run (same
    fail-safe-to-full-rescan behavior as an actual UIDVALIDITY mismatch),
    never as a reason to skip messages without evidence.
    """
    for item in status_data or []:
        if not isinstance(item, bytes | bytearray):
            continue
        match = _UID_VALIDITY_RE.search(bytes(item))
        if match:
            return int(match.group(1))
    return None


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

    def uid(self, command: str, *args) -> tuple[str, list]: ...

    def status(self, mailbox: str, names: str) -> tuple[str, list[bytes]]: ...

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
    database and never scores jobs (see app/api/routes.py for that). It
    consults the separate ProcessedEmailMessage table via the injected
    `is_message_processed` callable, so it can skip acknowledged emails
    without mutating the mailbox itself. The persistence-owning caller marks
    a message only after all jobs in its XingEmailBatch are saved.
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
        scan_from_uid: int | None = None,
        expected_uid_validity: int | None = None,
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.app_password = app_password
        self.lookback_days = lookback_days
        # Codex gate follow-up (Astra R4A MEDIUM, starvation): durable
        # scan-position inputs from the caller's persisted
        # `XingScanProgressRecord` (see app.db.models for the full
        # rationale) -- `scan_from_uid` is only honored if this run's own
        # observed `UIDVALIDITY` (read fresh every call, never trusted
        # from a prior run) equals `expected_uid_validity`; otherwise the
        # stored watermark is silently ignored and every candidate is
        # scanned, exactly like a brand-new installation.
        self._scan_from_uid = scan_from_uid
        self._expected_uid_validity = expected_uid_validity
        # Injected only by tests, to avoid a real IMAP connection; production
        # code always opens (and closes) its own connection in _fetch_sync.
        self._injected_client = imap_client
        # A callable rather than a raw db.Session keeps this collector
        # decoupled from SQLAlchemy while still allowing it to skip messages
        # acknowledged by the caller after persistence. The collector never
        # marks a message itself: only the caller knows whether every parsed
        # job was persisted successfully.
        self._is_message_processed = is_message_processed or (lambda _message_id: False)
        # Set on every fetch() call; read by callers after awaiting fetch()
        # to report how many blocks were skipped (mirrors
        # BundesagenturCollector.skipped_invalid_count).
        self.skipped_invalid_count = 0
        # NEW-001 (Astra R4A): True if the IMAP session's total wall-clock
        # deadline (AUD-005) fired before every candidate message could be
        # fetched this run -- set (never raised) by `_fetch_sync_body`, so
        # every batch that DID complete fetch+parse before that happened
        # is still returned to the caller instead of being discarded. Read
        # by callers after awaiting fetch()/fetch_message_batches(),
        # mirroring `skipped_invalid_count`'s own out-of-band reporting
        # convention -- see `app.services.collector_runner.run_xing`.
        self.deadline_exceeded = False
        # Codex gate follow-up (Astra R4A MEDIUM, starvation): this run's
        # observed mailbox UIDVALIDITY (set in `_fetch_sync_body`, always
        # freshly read -- never inherited from `expected_uid_validity`)
        # and the ascending list of UIDs this run confirmed are safe to
        # never look at again (already-acknowledged via the Message-ID
        # pre-check, or confirmed not a job digest at all). Read by
        # `app.services.collector_runner.run_xing` after awaiting
        # fetch_message_batches() to compute the new persisted watermark
        # -- see `XingEmailBatch.uid`'s own docstring for why a UID that
        # DID yield a batch is deliberately NOT included here (its
        # safety to skip next time depends on whether the caller's own
        # persistence succeeded, which this collector has no visibility
        # into).
        self.uid_validity: int | None = None
        self.confirmed_uids: list[int] = []
        # Codex gate follow-up (Astra R4A HIGH, watermark gap): the FULL
        # ordered (ascending) list of UIDs this run considered candidates
        # -- set once in `_fetch_sync_body` before the per-UID loop, and
        # never mutated afterward, so it also includes UIDs the loop never
        # reached (session deadline) or that failed FETCH outright (not in
        # `confirmed_uids`, no batch produced). `run_xing` walks this list
        # in order -- not `confirmed_uids`/its own persisted-batch map
        # keys alone -- so a UID that is neither confirmed-skippable nor a
        # successfully persisted batch stops the watermark computation
        # immediately, instead of simply being absent and silently
        # stepped over by a later UID that DID resolve.
        self.candidate_uids: list[int] = []

    async def fetch(self, since: datetime | None = None) -> list[Job]:
        batches = await self.fetch_message_batches(since)
        return [job for batch in batches for job in batch.jobs]

    async def fetch_message_batches(self, since: datetime | None = None) -> list[XingEmailBatch]:
        """Fetch jobs grouped by source Message-ID for safe acknowledgment.

        The caller must acknowledge a batch only after every job in it has
        been persisted. Keeping this XING-specific method separate preserves
        the generic JobCollector.fetch() -> list[Job] contract.
        """
        if not is_configured(self.username) or not is_configured(self.app_password):
            raise XingAuthError(
                "XING_MAILBOX_USERNAME / XING_MAILBOX_APP_PASSWORD is not configured"
            )

        self.skipped_invalid_count = 0
        self.deadline_exceeded = False
        self.uid_validity = None
        self.confirmed_uids = []
        self.candidate_uids = []
        since_date = since or (datetime.now(UTC) - timedelta(days=self.lookback_days))

        # IMAP (imaplib) is synchronous/blocking; run it off the event loop
        # via a worker thread so fetch() honors JobCollector's async
        # contract without blocking other requests for the duration of the
        # IMAP session. imaplib (stdlib) was chosen over aioimaplib (a
        # third-party, less mature dependency) — this collector runs
        # manually and infrequently (like Bundesagentur's), so native
        # asyncio I/O isn't worth the extra dependency here.
        #
        # Note: the injected is_message_processed callable (bound to the
        # caller's db.Session — see app/api/routes.py) gets invoked from this
        # worker thread. That's safe here only because the route handler
        # awaits this call and does not touch `db` concurrently while it runs.
        return await asyncio.to_thread(self._fetch_sync, since_date)

    def _fetch_sync(self, since: datetime) -> list[XingEmailBatch]:
        client = self._injected_client
        owns_connection = client is None
        # AUD-005: a real total wall-clock deadline for this whole session
        # (login through select/search/fetch/close/logout), not just the
        # per-read inactivity timeout `timeout=` already gives the socket
        # -- see app/providers/email/imap_deadline.py's module docstring
        # for why a slow-drip peer needs this. Only applied when this call
        # owns the connection: a test-injected client has no real socket
        # to bound.
        deadline = ImapSessionDeadline(IMAP_SESSION_DEADLINE_SECONDS) if owns_connection else None

        with contextlib.ExitStack() as stack:
            if deadline is not None:
                stack.enter_context(deadline)
            if client is None:
                client = self._connect(deadline)
            try:
                return self._fetch_sync_body(client, since, deadline)
            except OSError as exc:
                if deadline is not None and deadline.exceeded:
                    logger.warning("xing_email_imap_session_deadline_exceeded")
                else:
                    logger.warning(
                        "xing_email_imap_operation_failed error_type=%s", type(exc).__name__
                    )
                raise XingConnectionError("IMAP operation failed") from exc
            finally:
                if owns_connection:
                    self._disconnect(client)

    def _fetch_sync_body(
        self,
        client: ImapClient,
        since: datetime,
        deadline: ImapSessionDeadline | None,
    ) -> list[XingEmailBatch]:
        typ, _ = client.select("INBOX", readonly=True)
        if typ != "OK":
            raise XingConnectionError(f"IMAP SELECT failed: {typ}")

        # Codex gate follow-up (Astra R4A MEDIUM, starvation): read this
        # run's REAL UIDVALIDITY before trusting any persisted watermark
        # -- see `_parse_uid_validity`'s own docstring for why a mismatch
        # (or an undetermined value) must make the stored
        # `scan_from_uid` inert rather than risk skipping reused UIDs.
        typ, status_data = client.status("INBOX", "(UIDVALIDITY)")
        self.uid_validity = _parse_uid_validity(status_data) if typ == "OK" else None

        criteria = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
        # UID SEARCH (not plain SEARCH/sequence numbers): a persisted
        # scan-position watermark is only meaningful against an
        # identifier RFC 3501 guarantees never shifts or gets reused
        # within one UIDVALIDITY epoch -- see
        # app.db.models.XingScanProgressRecord's docstring for why a
        # sequence-number- or DB-row-id-based watermark would not be
        # safe here.
        typ, data = client.uid("search", None, criteria)
        if typ != "OK":
            raise XingConnectionError(f"IMAP SEARCH failed: {typ}")

        all_uids = [int(x) for x in (data[0].split() if data and data[0] else [])]
        scan_from_uid = None
        if self.uid_validity is not None and self.uid_validity == self._expected_uid_validity:
            scan_from_uid = self._scan_from_uid
        if scan_from_uid is None:
            candidate_uids = all_uids
        else:
            # The actual starvation fix: a confirmed-handled prefix is
            # dropped here, client-side, BEFORE issuing a single IMAP
            # call for any of it -- not merely before the expensive
            # RFC822 body transfer (the earlier Message-ID pre-check
            # fix), which still paid for one header FETCH per already
            # -processed message every run.
            candidate_uids = [uid for uid in all_uids if uid > scan_from_uid]

        # Codex gate follow-up (Astra R4A HIGH, watermark gap): captured
        # BEFORE the loop runs, and never mutated afterward -- see
        # `self.candidate_uids`'s own docstring in `__init__` for why
        # `run_xing` needs the full ordered candidate list, not just the
        # subset that ended up in `confirmed_uids`/produced a batch.
        self.candidate_uids = candidate_uids

        batches: list[XingEmailBatch] = []
        for uid in candidate_uids:
            # AUD-005: once the total session deadline has fired, the
            # connection's socket is already forcibly closed (see
            # ImapSessionDeadline) -- stop issuing further FETCHes on it
            # instead of letting each remaining message fail one at a
            # time.
            if deadline is not None and deadline.exceeded:
                break
            uid_bytes = str(uid).encode("ascii")
            try:
                batch, confirmed = self._fetch_and_process_message(client, uid_bytes, uid)
            except OSError:
                # Codex gate follow-up (Astra R4A HIGH): a transport
                # -level failure mid-FETCH (the exact case the earlier
                # NEW-001 fix's between-iterations `break` above did NOT
                # cover -- the deadline watchdog can force-close the
                # socket WHILE a FETCH is already blocked in flight,
                # raising OSError from inside
                # _fetch_and_process_message rather than being observed
                # cleanly at the top of the next iteration). If the
                # deadline is what caused this, stop cleanly here and let
                # the SAME post-loop logic below return the batches
                # already completed, flagged via `deadline_exceeded` --
                # never silently discarded. If the deadline did NOT
                # cause it (a genuine, unexpected connection failure),
                # re-raise so `_fetch_sync`'s existing outer handler logs
                # and raises `XingConnectionError`, unchanged.
                if deadline is not None and deadline.exceeded:
                    break
                raise
            if batch is not None:
                batches.append(batch)
            elif confirmed:
                # Codex gate follow-up (Astra R4A MEDIUM, starvation):
                # provably safe to never look at again (already
                # -acknowledged, or confirmed not a job digest at all) --
                # NOT a transient fetch failure (`confirmed=False` for
                # that case, see `_fetch_and_process_message`), so a
                # later run is free to skip straight past this UID
                # without even a header FETCH once
                # `app.services.collector_runner.run_xing` persists it
                # as part of the confirmed contiguous prefix.
                self.confirmed_uids.append(uid)

        # NEW-001 (Astra R4A): a prior version of this method RAISED
        # XingConnectionError here, discarding `batches` entirely -- every
        # message batch that had already completed fetch+parse before the
        # deadline fired was silently lost, with no acknowledgment ever
        # recorded for it (see app.services.collector_runner.run_xing,
        # which only marks a source message processed after ALL its jobs
        # persist). A backlog that consistently exceeds the deadline would
        # then repeatedly re-fetch the same oldest-message prefix while
        # making zero persisted progress, and those messages could
        # eventually age out of the lookback window entirely. Recording
        # the flag instead (never silently reported as full success --
        # see `run_xing`'s own `deadline_exceeded` counter) lets the
        # caller persist everything that finished in time and report a
        # truthful partial outcome. A GENUINE connection/auth failure
        # (the socket dying during SELECT/SEARCH/one message's own FETCH)
        # still raises normally via `_fetch_sync`'s OSError handler below
        # -- this change touches ONLY the case where the loop above
        # already finished cleanly and nothing failed except running out
        # of time.
        if deadline is not None and deadline.exceeded:
            self.deadline_exceeded = True

        return batches

    def _connect(self, deadline: ImapSessionDeadline | None = None) -> imaplib.IMAP4_SSL:
        try:
            # AUD-001: an explicit verifying SSLContext -- imaplib's own
            # default (ssl_context=None) resolves to
            # ssl._create_stdlib_context(), which sets verify_mode=CERT_NONE
            # and check_hostname=False, i.e. no certificate verification at
            # all. AUD-005: `timeout=` bounds each individual blocking
            # socket read on this connection.
            ssl_context = ssl.create_default_context()
            if deadline is not None:
                # AUD-005 (narrow re-review): DeadlineIMAP4SSL binds the
                # watchdog to the real socket from the moment it's
                # created -- covering the TCP connect, TLS handshake, and
                # (once this constructor call returns and
                # imaplib.IMAP4.__init__ proceeds to IMAP4._connect())
                # the greeting/CAPABILITY reads, not just the commands
                # this collector issues after _connect() returns. See
                # app/providers/email/imap_deadline.py's DeadlineIMAP4SSL
                # docstring for why binding the socket only after this
                # call returned (the previous version of this fix) left
                # the whole constructor unprotected.
                client = DeadlineIMAP4SSL(
                    self.imap_host,
                    self.imap_port,
                    ssl_context=ssl_context,
                    timeout=IMAP_OPERATION_TIMEOUT_SECONDS,
                    deadline=deadline,
                )
            else:
                client = imaplib.IMAP4_SSL(
                    self.imap_host,
                    self.imap_port,
                    ssl_context=ssl_context,
                    timeout=IMAP_OPERATION_TIMEOUT_SECONDS,
                )
        except (OSError, imaplib.IMAP4.error) as exc:
            # AUD-005 (Codex final review, MEDIUM): never interpolate the
            # underlying OSError/host/port into the raised message -- an
            # operator-visible surface (see app/api/routes.py's
            # `run_xing_collector`, which puts `str(exc)` straight into
            # an HTTP 502 `detail`) must not leak connection internals or
            # raw exception text. Mirrors GMAIL-003's sanitization
            # exactly (app/providers/email/imap.py's `_connect`).
            # Constructor-time protocol failures -- imaplib.IMAP4.error/
            # abort, e.g. a malformed/aborted greeting or CAPABILITY
            # response, or the deadline forcing the socket closed
            # mid-read -- can carry raw server-controlled text just like
            # an OSError can carry raw host/port text.
            # `imaplib.IMAP4.abort` subclasses `imaplib.IMAP4.error`, so
            # catching `error` alone already covers both. Internal-only
            # diagnosis uses type(exc).__name__, never str(exc).
            logger.warning("xing_connect_failed error_type=%s", type(exc).__name__)
            raise XingConnectionError(
                "Could not connect to the configured XING mailbox IMAP host"
            ) from exc

        try:
            client.login(self.username, self.app_password)
        except imaplib.IMAP4.error as exc:
            logger.warning("xing_login_failed error_type=%s", type(exc).__name__)
            raise XingAuthError("XING mailbox IMAP login was rejected") from exc
        return client

    def _disconnect(self, client: ImapClient) -> None:
        # Codex gate follow-up (Astra R4B, NEW-003: XING log leakage):
        # `exc_info=True` logs the FULL traceback, including the
        # exception's own str(exc) -- for a close/logout failure that can
        # be a raw imaplib.IMAP4.error/OSError carrying server-controlled
        # or connection-internal text, exactly what AUD-005's
        # `type(exc).__name__`-only convention exists to keep out of logs
        # (see `_connect`'s identical rationale above). Mirrors
        # `app.providers.email.imap.GmailImapProvider._disconnect`
        # (GMAIL-003) exactly.
        try:
            client.close()
        except Exception as exc:
            logger.warning("xing_email_imap_close_failed error_type=%s", type(exc).__name__)
        try:
            client.logout()
        except Exception as exc:
            logger.warning("xing_email_imap_logout_failed error_type=%s", type(exc).__name__)

    def _read_message_id_header(self, client: ImapClient, uid: bytes) -> str | None:
        """Codex gate follow-up (Astra R4A HIGH): a lightweight pre-check
        -- mirrors `app.providers.email.imap.GmailImapProvider
        ._read_message_size`'s "cheap probe before the expensive
        transfer" pattern -- that fetches ONLY the Message-ID header via
        `BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]` instead of the full
        RFC822 body. This is what lets `_fetch_and_process_message` skip
        an already-acknowledged message WITHOUT paying for a full-body
        transfer at all (the starvation this closes: a backlog where
        most messages are already-processed used to spend the ENTIRE
        per-run fetch budget re-transferring their full bodies before
        ever reaching a genuinely new message).

        Returns None if the header could not be determined (some
        servers/fakes may omit or malform it, or a real transport error
        occurred) -- the caller then falls back to the full fetch rather
        than failing closed, an honest documented gap exactly like
        `_read_message_size`'s own. `OSError` is NOT swallowed here: a
        transport-level failure (including the deadline watchdog forcing
        the socket closed mid-read) must propagate to
        `_fetch_and_process_message`'s own caller, never be silently
        reinterpreted as "header absent, fall back to full fetch".
        """
        try:
            typ, data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        except OSError:
            raise
        except Exception:
            return None
        if typ != "OK" or not data:
            return None
        for item in data:
            candidate = item[1] if isinstance(item, tuple) else item
            if not isinstance(candidate, bytes | bytearray):
                continue
            match = _MESSAGE_ID_HEADER_RE.search(bytes(candidate))
            if match:
                return match.group(1).decode("ascii", errors="replace").strip()
        return None

    def _fetch_and_process_message(
        self, client: ImapClient, uid: bytes, uid_int: int
    ) -> tuple[XingEmailBatch | None, bool]:
        """Returns `(batch, confirmed)`. `confirmed=True` iff this UID is
        provably safe to skip on every future run without looking at it
        again (Codex gate follow-up, Astra R4A MEDIUM starvation fix) --
        `batch is None and confirmed is False` means a transient fetch
        failure instead, which must be retried, never remembered as
        handled. `batch is not None` (`confirmed` is then always False,
        unused) leaves the "is this UID safe to skip later" decision to
        the caller, which alone knows whether persisting this batch's
        jobs actually succeeded -- see `XingEmailBatch.uid`'s docstring.
        """
        # Codex gate follow-up (Astra R4A HIGH): skip already-acknowledged
        # messages BEFORE transferring their full RFC822 body -- see
        # `_read_message_id_header`'s own docstring for the starvation
        # this prevents. A pre-check that couldn't determine the header
        # (None) falls through to the normal full fetch below, where
        # `_process_message`'s own (pre-existing, unchanged)
        # `is_message_processed` check still applies as a safety net.
        precheck_message_id = self._read_message_id_header(client, uid)
        if precheck_message_id and self._is_message_processed(precheck_message_id):
            return None, True

        typ, msg_data = client.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not msg_data or msg_data[0] is None:
            logger.warning("xing_email_message_fetch_failed uid=%s", uid)
            return None, False

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        return self._process_message(msg, uid_int)

    def _process_message(self, msg: Message, uid: int) -> tuple[XingEmailBatch | None, bool]:
        sender = parseaddr(msg.get("From", ""))[1].casefold()
        if sender != XING_DIGEST_SENDER:
            return None, True

        subject = _decode_subject(msg.get("Subject", ""))
        if not _is_job_digest_subject(subject):
            logger.info("xing_email_skipped_subject subject=%s", subject)
            return None, True

        message_id = (msg.get("Message-ID") or "").strip()
        if not message_id:
            logger.warning("xing_email_skipped_missing_message_id subject=%s", subject)
            return None, True
        if self._is_message_processed(message_id):
            return None, True

        body = _extract_plaintext_body(msg)
        if not body:
            logger.warning("xing_email_no_plaintext_body message_id=%s", message_id)
            return XingEmailBatch(message_id=message_id, jobs=(), uid=uid), False

        jobs: list[Job] = []
        for block in _split_blocks(body):
            job = _parse_block(block)
            if job is None:
                self.skipped_invalid_count += 1
            else:
                jobs.append(job)

        if not jobs:
            logger.warning("xing_email_no_valid_job_blocks message_id=%s", message_id)

        return XingEmailBatch(message_id=message_id, jobs=tuple(jobs), uid=uid), False
