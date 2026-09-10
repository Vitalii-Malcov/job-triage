"""Gmail outbound SMTP provider (Stage 7D).

Sends exactly one message per `send()` call, over SMTPS (implicit TLS),
using the SAME Gmail account credentials as the read-only IMAP provider
(`GMAIL_USERNAME`/`GMAIL_APP_PASSWORD`) — a Gmail App Password is valid
for both IMAP and SMTP against the same account, so no new secret is
introduced. This module is completely independent of
app/providers/email/imap.py; see app/providers/email/outbound_base.py's
module docstring for why the two are never merged.

**No mailbox read access whatsoever.** This provider only ever opens an
SMTP connection — never IMAP — so it structurally cannot read, label,
mark, or delete anything in the mailbox; the "read-only IMAP contract"
that provider enforces is simply not reachable from here.

**No content is ever read from raw email here.** The only inputs this
module ever sees are `OutboundMessage`'s already-resolved, already-
trusted fields (see outbound_base.py's module docstring) — this module
does not parse MIME, does not read `body_plain`, and never touches
`app.db.models.GmailMessageRecord` directly.

**Hard connection timeout (S7E-014, Codex re-review).** `_connect()`
always opens the underlying socket with `timeout=SMTP_OPERATION_TIMEOUT_SECONDS`
(see that constant's own docstring) — a hung/black-holed SMTP peer raises
within a bounded time instead of blocking indefinitely, which is what
lets `app.services.follow_up_send` safely hold a per-Gmail-thread lock
across the whole `send()` call without that lease expiring mid-send.

**Ambiguous-outcome handling (see outbound_base.py's "honest
delivery-outcome contract").** `send()` builds and validates the
outbound `EmailMessage` BEFORE opening a connection or calling
`send_message()` — a malformed message (e.g. a CRLF/header-injection
attempt) is therefore always a DEFINITE pre-transmission failure
(`EmailSendConnectionError`), never confused with a genuinely ambiguous
outcome. Once `send_message()` is actually invoked, this provider makes
NO claim that a raised exception proves the message was not delivered —
`smtplib.SMTPException`/`OSError` raised during or after that call
(including a dropped connection mid-transmission) is reported as
`EmailSendOutcomeUnknownError`, distinctly from a pre-transmission
failure. This is a deliberately conservative ("safest-acceptable")
classification: this project does not attempt to distinguish
"definitely rejected before send" from "possibly accepted, response
never received" among post-`send_message()` exceptions.
"""

import logging
import smtplib
import socket
import ssl
import threading
from collections.abc import Callable
from email.message import EmailMessage
from typing import Protocol

from app.collectors.base import is_configured
from app.providers.email.outbound_base import (
    EmailSendAuthError,
    EmailSendConnectionError,
    EmailSendOutcomeUnknownError,
    OutboundMessage,
    OutboundSendResult,
)

logger = logging.getLogger(__name__)

# S7E-014 (Codex re-review, final lock hardening): a hard socket-level
# timeout applied to EVERY blocking operation on this provider's SMTP
# connection (connect, TLS handshake, login, send_message, quit — all
# share the SAME underlying socket once opened, so a single
# `timeout=` at construction covers the whole session uniformly; see
# `_connect`). Exists so `app.services.follow_up_send.send_follow_up`
# (and app.services.response_draft_send, which shares this provider) can
# safely hold a per-Gmail-thread guard
# (`app.db.gmail_repository.THREAD_LOCK_TTL_SECONDS`, currently 30s)
# across the ENTIRE `provider.send()` call without that lease expiring
# out from under a still-running send — a hung/black-holed SMTP peer must
# raise well before the lease can lapse, never rely on "it should
# probably finish in time".
#
# Sized with a large safety margin below THREAD_LOCK_TTL_SECONDS (not
# imported here — this provider module stays DB-free; the safety
# relationship between the two constants is asserted directly by
# tests/test_providers_email_smtp.py::test_operation_timeout_is_safely_below_thread_lock_ttl,
# which is the actual proof that this margin holds, not just prose).
#
# This bounds each INDIVIDUAL blocking socket operation's INACTIVITY —
# a peer that keeps trickling SOME bytes before every read times out
# (never idle long enough to trip this) can still hold a single
# operation open far longer than this value; see
# `SMTP_TOTAL_DEADLINE_SECONDS` below for the actual wall-clock cap on
# the whole `send()` call, which is what interrupts that case.
SMTP_OPERATION_TIMEOUT_SECONDS = 8.0

# Codex gate follow-up (Astra R4B, NEW-006: SMTP total deadline). A
# genuine wall-clock deadline for the ENTIRE `send()` call (connect +
# login + send_message + quit combined), not merely
# `SMTP_OPERATION_TIMEOUT_SECONDS`'s per-operation INACTIVITY bound.
#
# **Why the per-operation timeout alone is not enough.** Python's socket
# timeout resets on any activity — it bounds how long a single recv/send
# call may sit IDLE, not how long a whole logical operation (which can
# involve many internal recv/send round trips, e.g. `login()`'s own
# EHLO/AUTH exchange) may run in total. A peer that keeps writing SOME
# bytes before every read's timeout expires — never actually idle long
# enough to trip `SMTP_OPERATION_TIMEOUT_SECONDS` — can hold the
# connection open indefinitely under that bound alone. A hard preemptive
# deadline (`signal.alarm`) is main-thread-only and unusable here (Stage
# 7E's HTTP handlers run in FastAPI's worker thread pool), so `send()`
# instead runs the actual blocking smtplib work on a dedicated daemon
# thread and imposes this deadline from the CALLING thread via
# `threading.Event.wait(timeout=...)`.
#
# **Active interruption, not passive abandonment (Codex gate follow-up,
# Astra R4B, NEW-006 take 2).** When the deadline fires BEFORE
# `send_message()` was reached, `send()` does two things, not one:
# (1) it actively closes the worker's live socket, if by then it has
# one, so a genuinely blocked real recv/send call unblocks promptly
# instead of running to its own natural `SMTP_OPERATION_TIMEOUT_SECONDS`;
# (2) — the actual CORRECTNESS guarantee, independent of whether (1)
# succeeds (e.g. an injected test double, or a block that isn't
# socket-shaped at all) — it atomically marks the send as cancelled
# under a shared lock the worker itself checks, under the SAME lock,
# immediately before calling `send_message()`. Whichever side reaches
# the lock first wins: if the deadline-side wins, the worker is
# GUARANTEED to observe "cancelled" and return without ever calling
# `send_message()`, even if whatever blocked it (real or fake) later
# resolves on its own. If the worker already committed to sending
# before the deadline-side could grab the lock, the deadline-side
# correctly reports the outcome as UNCERTAIN instead of DEFINITE
# instead. See `send()`'s own docstring for the full mechanism. The
# worker thread stays `daemon=True` purely so it can never block process
# shutdown if it does not exit promptly — that property is not the
# cancellation mechanism itself.
#
# Margin below `THREAD_LOCK_TTL_SECONDS` (30s, imported only by
# tests/test_providers_email_smtp.py — this module stays DB-free, see
# `SMTP_OPERATION_TIMEOUT_SECONDS`'s own note) covers this thread-based
# deadline's own small scheduling overhead plus the caller's work after
# `send()` returns, before it releases the per-thread lock — proven by
# tests/test_providers_email_smtp.py::test_total_deadline_is_safely_below_thread_lock_ttl,
# not just this comment.
SMTP_TOTAL_DEADLINE_SECONDS = 20.0


class SmtpClient(Protocol):
    """The subset of smtplib.SMTP_SSL's interface this provider uses —
    mirrors app.providers.email.base.ImapClient's "narrow Protocol so
    tests can inject a fake instead of a real connection" rationale.
    """

    def login(self, user: str, password: str) -> tuple[int, bytes]: ...

    def send_message(self, msg: EmailMessage) -> dict: ...

    def quit(self) -> tuple[int, bytes]: ...


class GmailSmtpProvider:
    """Structurally satisfies `app.providers.email.outbound_base.
    OutboundEmailProvider` (a `Protocol` — no explicit inheritance
    needed, mirrors `GmailImapProvider`'s own relationship to `ImapClient`).
    Sends one outbound reply via Gmail SMTP. Not async (mirrors
    XingEmailCollector's synchronous IMAP calls) — the caller
    (app.services.response_draft_send) is responsible for offloading this
    to a worker thread if called from an async context, exactly as
    app.providers.email.imap.GmailImapProvider._fetch_sync's own
    docstring documents for its blocking imaplib calls.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        app_password: str,
        from_address: str | None = None,
        smtp_client: SmtpClient | None = None,
        timeout_seconds: float = SMTP_OPERATION_TIMEOUT_SECONDS,
        total_deadline_seconds: float = SMTP_TOTAL_DEADLINE_SECONDS,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.app_password = app_password
        # The account's own address is used as the From header — falls
        # back to `username` (a Gmail username IS the account's email
        # address) when not given a separate value.
        self.from_address = from_address or username
        # Injected only by tests, to avoid a real SMTP connection —
        # mirrors GmailImapProvider._injected_client.
        self._injected_client = smtp_client
        # S7E-014: overridable only for tests that need a much smaller
        # bound to keep a real-socket timeout proof fast — production
        # callers (app/api/routes.py) always use the safe module default.
        self.timeout_seconds = timeout_seconds
        # NEW-006 (Astra R4B): same override contract as timeout_seconds
        # above, for the whole-call wall-clock deadline -- see
        # SMTP_TOTAL_DEADLINE_SECONDS's own docstring.
        self.total_deadline_seconds = total_deadline_seconds

    def send(self, message: OutboundMessage) -> OutboundSendResult:
        if not is_configured(self.username) or not is_configured(self.app_password):
            raise EmailSendAuthError("GMAIL_USERNAME / GMAIL_APP_PASSWORD is not configured")

        # Build/validate the message BEFORE any connection/transmission
        # attempt — a malformed header (e.g. a CRLF-header-injection
        # attempt smuggled into subject, which Python's email.message
        # rejects with ValueError) must remain a DEFINITE pre-send
        # failure, never misclassified as an ambiguous outcome. No
        # connection has been opened yet at this point, so "not sent" is
        # provably true here.
        try:
            msg = self._build_message(message)
        except ValueError as exc:
            logger.warning("outbound_email_build_failed error_type=%s", type(exc).__name__)
            raise EmailSendConnectionError("Building the outbound email failed") from exc

        # Codex gate follow-up (Astra R4B, NEW-006 take 2: SMTP total
        # deadline, active interruption). The actual blocking smtplib
        # work (connect + login + send_message + quit) runs on a
        # dedicated `daemon=True` thread; THIS (calling) thread imposes
        # the hard wall-clock deadline via
        # `done.wait(timeout=self.total_deadline_seconds)` instead of
        # trusting any per-socket-operation timeout to bound the whole
        # call — see `SMTP_TOTAL_DEADLINE_SECONDS`'s own docstring for why
        # that is NOT equivalent.
        #
        # **The correctness guarantee is NOT "the thread is abandoned and
        # we hope it never sends."** `gate_lock` + `state["phase"]` form a
        # single atomic checkpoint shared by both threads: the worker
        # must hold `gate_lock` to transition from "before_send" to
        # "sending" immediately before calling `send_message()`; the
        # deadline-side, on timeout, must hold the SAME lock to transition
        # to "cancelled". Whichever side reaches the lock first wins --
        # there is no window where both "the deadline already returned a
        # DEFINITE failure to the caller" and "the worker still goes on to
        # call send_message()" can both be true. `state["client"]` is
        # populated as early as possible (via `_connect`'s `on_connected`
        # callback, right after the socket-level connect succeeds, before
        # `login()`) so the deadline-side can ALSO actively close the live
        # transport when it wins the race -- a genuinely blocked real
        # recv/send call (e.g. a hung `login()`) then raises promptly
        # instead of running to its own natural per-operation timeout.
        # This is real interruption, not merely a hope that the daemon
        # thread eventually gives up.
        gate_lock = threading.Lock()
        state: dict[str, object] = {"phase": "before_send", "client": None}
        done = threading.Event()
        outcome: dict[str, BaseException | OutboundSendResult] = {}

        def _register_client(connected_client: SmtpClient) -> None:
            with gate_lock:
                state["client"] = connected_client

        def _do_send() -> None:
            try:
                client = self._injected_client
                owns_connection = client is None
                if client is None:
                    client = self._connect(on_connected=_register_client)
                else:
                    _register_client(client)
                try:
                    with gate_lock:
                        if state["phase"] == "cancelled":
                            # The deadline already won this race and
                            # reported a DEFINITE pre-transmission failure
                            # to the caller -- send_message() must never
                            # be reached after that, regardless of what
                            # unblocked this worker (a real socket error
                            # from the forced close below, or -- for an
                            # injected/fake transport with nothing to
                            # actually close -- the blocked call simply
                            # returning on its own).
                            return
                        state["phase"] = "sending"
                    client.send_message(msg)
                except (smtplib.SMTPException, OSError) as exc:
                    # Transmission was ATTEMPTED — the server may or may
                    # not have accepted the message before this exception
                    # occurred (a dropped connection, a timeout waiting
                    # for the final reply, etc.). This package has no
                    # positive proof of non-delivery for ANY exception
                    # raised past this point (safest-acceptable rule —
                    # see EmailSendOutcomeUnknownError's docstring), so
                    # this is NEVER reported as a definite failure.
                    logger.warning(
                        "outbound_email_send_outcome_unknown error_type=%s", type(exc).__name__
                    )
                    # Deliberately NOT `from exc` (unlike this same
                    # classification pre-thread-refactor): this object is
                    # stored and re-raised in the CALLING thread below,
                    # not raised immediately inside this `except` block,
                    # so Python would not auto-chain it anyway — and per
                    # NEW-003's identical rationale (see
                    # app.collectors.xing_email), never attach a raw
                    # OSError/SMTPException as `__cause__` on purpose:
                    # a future `logger.exception()`/traceback dump of
                    # this exception must not resurrect the underlying
                    # provider detail this classification exists to keep
                    # out of logs.
                    outcome["error"] = EmailSendOutcomeUnknownError(
                        "Sending the outbound email had an uncertain outcome"
                    )
                    return
                finally:
                    if owns_connection:
                        self._disconnect(client)
                outcome["result"] = OutboundSendResult(provider_message_id=msg.get("Message-Id"))
            except BaseException as exc:  # noqa: BLE001 -- re-raised in the caller's thread below
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=_do_send, daemon=True, name="smtp-send-total-deadline")
        worker.start()
        finished = done.wait(timeout=self.total_deadline_seconds)

        if finished:
            error = outcome.get("error")
            if error is not None:
                raise error
            return outcome["result"]

        # Deadline fired: resolve the race atomically against the
        # worker's own pre-send_message() checkpoint above.
        with gate_lock:
            already_sending = state["phase"] != "before_send"
            if not already_sending:
                state["phase"] = "cancelled"
            client = state["client"]

        # Codex gate follow-up (Astra R4B, NEW-006 take 3: SMTP
        # lifecycle). Actively abort the live transport on ANY total
        # deadline -- pre-send (belt-and-braces alongside the atomic
        # `gate_lock` above: even a real, genuinely hung connect/login is
        # now unblocked, not merely logically prevented from proceeding)
        # AND post-send (the outcome is ALREADY classified UNCERTAIN
        # either way; there is no reason to let a possibly-doomed worker
        # linger indefinitely on its own natural per-operation timeout
        # just because we can't improve on "unknown" by waiting longer).
        # `worker.join(...)` afterward gives real assurance -- not just
        # hope -- that the worker actually exits promptly once its
        # transport is gone, before this call returns to the caller.
        if client is not None:
            self._force_close(client)
            worker.join(timeout=1.0)
            if worker.is_alive():
                # Honest, not overclaiming: the abort did not (or not
                # yet) unblock the worker within a bounded wait -- do
                # NOT silently proceed as though it already terminated.
                # This never changes the classification below (still
                # correctly DEFINITE pre-send or UNCERTAIN post-send,
                # per `already_sending` above) -- it only makes the
                # still-lingering thread visible instead of hidden. The
                # thread stays `daemon=True`, so it still cannot block
                # process shutdown even if it never exits.
                logger.warning("outbound_smtp_worker_still_alive_after_abort")

        if already_sending:
            logger.warning("outbound_smtp_total_deadline_exceeded_after_send_attempted")
            raise EmailSendOutcomeUnknownError(
                "Sending the outbound email exceeded the total send deadline "
                "after transmission may have begun"
            )

        logger.warning("outbound_smtp_total_deadline_exceeded_before_send")
        raise EmailSendConnectionError("Total send deadline exceeded before transmission began")

    def _build_message(self, message: OutboundMessage) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = message.to_address
        msg["Subject"] = message.subject
        if message.in_reply_to:
            msg["In-Reply-To"] = message.in_reply_to
        if message.references:
            msg["References"] = " ".join(message.references)
        msg.set_content(message.body)
        return msg

    def _connect(
        self, *, on_connected: Callable[[SmtpClient], None] | None = None
    ) -> smtplib.SMTP_SSL:
        """`on_connected` (NEW-006 take 4: constructor-time gap).
        Invoked with the client object IMMEDIATELY after construction --
        BEFORE any network I/O whatsoever (TCP connect, TLS handshake,
        OR the SMTP greeting read) -- not merely "right after the
        socket-level connect succeeds" as a prior version of this
        docstring claimed.

        **The gap this closes.** `smtplib.SMTP_SSL(host, port, ...)`
        performs the ENTIRE connect + TLS handshake + greeting-read
        sequence INSIDE its own constructor when given a `host` -- so
        `send()`'s deadline watchdog had NO client object to register
        (and therefore nothing to `_force_close`) for the whole
        duration of that call. A peer that accepts the TCP+TLS
        connection but never sends its greeting banner (`getreply()`
        blocks reading it) could hold the worker thread hostage for
        that entire phase with no way for the deadline side to
        intervene. Constructing `SMTP_SSL` WITHOUT a `host` (smtplib
        defers ALL connect/greeting I/O to a later explicit `connect()`
        call when none is given at construction time -- see
        `smtplib.SMTP.__init__`) lets `on_connected` fire on a
        genuinely idle, not-yet-connected client, registering it with
        the watchdog before ANY of that I/O can even start. The
        watchdog can then `_force_close` it (via `client.sock`, set by
        `connect()` as soon as the TCP+TLS handshake itself completes,
        independent of whether the SUBSEQUENT greeting read ever does)
        as soon as it exists -- in particular, for exactly the
        "greeting never arrives" scenario this take specifically closes.

        (The narrower window BEFORE `client.sock` itself is assigned --
        a hung raw TCP connect or TLS handshake -- is unchanged from
        before: still bounded only by `timeout_seconds` below, since
        there is still no handle to abort during that sub-phase either
        way. This refactor closes the greeting-read gap specifically,
        not that pre-existing, structurally different limitation.)
        """
        # AUD-001: an explicit verifying SSLContext -- smtplib.SMTP_SSL's
        # own default (context=None) resolves to
        # ssl._create_stdlib_context(), which sets verify_mode=CERT_NONE
        # and check_hostname=False, i.e. no certificate verification at
        # all despite the connection authenticating with a real mailbox
        # password.
        ssl_context = ssl.create_default_context()
        # S7E-014: `timeout` bounds connect + the TLS handshake + the
        # initial greeting read (via the explicit `client.connect(...)`
        # below) — and, since it is set on the underlying socket for the
        # lifetime of the connection, every later blocking call on this
        # SAME client (login, send_message, quit) inherits it too. A
        # hung/black-holed peer raises `socket.timeout` (an `OSError`
        # subclass) rather than blocking indefinitely — see this
        # module's `SMTP_OPERATION_TIMEOUT_SECONDS` docstring for the
        # full rationale and its honest limitation. No `host`/`port`
        # here -- see this method's own docstring for why deferring the
        # connect is the whole point of this refactor.
        client = smtplib.SMTP_SSL(timeout=self.timeout_seconds, context=ssl_context)
        # smtplib's own TLS wrap (SMTP_SSL._get_socket) uses `self._host`
        # for SNI/certificate-hostname verification -- NOT whatever host
        # a later `connect()` call is given. `self._host` is normally
        # set from `__init__`'s own `host` argument, which we deliberately
        # did NOT pass (that is what triggers the auto-connect this
        # refactor avoids) -- so it must be set explicitly here, BEFORE
        # `connect()`, or TLS verification would silently target an EMPTY
        # hostname (`ssl.SSLContext.wrap_socket` then raises `ValueError:
        # check_hostname requires server_hostname` outright, since
        # `check_hostname=True` requires a non-empty one -- this is not
        # a theoretical concern, it was reproduced directly while writing
        # this refactor).
        client._host = self.smtp_host

        if on_connected is not None:
            on_connected(client)

        try:
            client.connect(self.smtp_host, self.smtp_port)
        except OSError as exc:
            # Never interpolate the underlying OSError/host/port into the
            # raised message — same GMAIL-003-style rationale as
            # GmailImapProvider._connect. Covers a connect-phase timeout
            # exactly like any other connection failure: no transmission
            # was ever attempted, so this is safely a DEFINITE pre-send
            # failure.
            logger.warning("outbound_smtp_connect_failed error_type=%s", type(exc).__name__)
            raise EmailSendConnectionError(
                "Could not connect to the configured outbound SMTP host"
            ) from exc

        try:
            client.login(self.username, self.app_password)
        except smtplib.SMTPException as exc:
            logger.warning("outbound_smtp_login_failed error_type=%s", type(exc).__name__)
            raise EmailSendAuthError("Outbound SMTP login was rejected") from exc
        except OSError as exc:
            # A timeout (or any other socket-level failure) waiting for
            # the login exchange — NOT an `smtplib.SMTPException`, so it
            # needs its own handler (previously unhandled here, which
            # would have leaked a raw OSError/TimeoutError instead of an
            # `EmailSendError` — see this module's test suite). Still
            # strictly pre-`send_message()`, so still a DEFINITE
            # pre-transmission failure, not an ambiguous one; distinct
            # from `EmailSendAuthError` because this is not a proven
            # credential rejection.
            logger.warning("outbound_smtp_login_failed error_type=%s", type(exc).__name__)
            raise EmailSendConnectionError("Could not complete outbound SMTP login") from exc
        return client

    def _disconnect(self, client: SmtpClient) -> None:
        try:
            client.quit()
        except Exception as exc:
            logger.warning("outbound_smtp_quit_failed error_type=%s", type(exc).__name__)

    def _force_close(self, client: SmtpClient) -> None:
        """Codex gate follow-up (Astra R4B, NEW-006 take 3: SMTP
        lifecycle): actively aborts a live transport `send()`'s
        total-deadline watchdog has decided to abandon, whether that's
        PRE-transmission (belt-and-braces alongside `send()`'s own
        `gate_lock`, which is the actual correctness guarantee that
        `send_message()` is never reached -- this is what makes a
        genuinely hung real connect/login unblock promptly instead of
        running to its own natural `SMTP_OPERATION_TIMEOUT_SECONDS`) or
        POST-transmission (the outcome is already UNCERTAIN either way;
        this just stops the worker thread from lingering).

        **`shutdown()` before `close()` -- not `close()` alone.**
        `smtplib`'s own `client.file` (from `sock.makefile(...)`, used
        internally by `getreply()` to read each server response line)
        holds an INDEPENDENT reference to the same socket -- `socket`'s
        own reference-counting means a bare `sock.close()` here only
        decrements a Python-level refcount; the underlying OS-level file
        descriptor stays open (and any `recv()` already blocked on it,
        including one happening via that buffered file object rather
        than the socket object directly, stays blocked) until
        `client.file` is ALSO closed -- which only the WORKER thread
        does, and only AFTER its own blocked call already returns. That
        is exactly the deadlock this method exists to prevent, so
        `close()` alone is not reliable here. `shutdown(SHUT_RDWR)`
        operates at the OS socket level, independent of how many Python
        wrapper objects reference the fd -- it reliably makes ANY
        blocked recv() on this socket, from ANY thread and through ANY
        wrapper (raw socket or `makefile()`-backed buffered reader),
        return immediately (typically 0 bytes, or `ConnectionError`).

        Best-effort only: an injected test double (or any `SmtpClient`
        with no real `.sock`) is silently tolerated -- for a real
        transport this is a genuine correctness step for the
        post-transmission/worker-termination case (not merely a
        defense-in-depth speed-up the way it was pre-transmission, where
        `gate_lock` alone already guarantees `send_message()` is never
        reached).
        """
        sock = getattr(client, "sock", None)
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
