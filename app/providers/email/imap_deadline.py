"""AUD-005: a REAL total wall-clock deadline for one IMAP session.

Both `app.providers.email.imap.GmailImapProvider` and
`app.collectors.xing_email.XingEmailCollector` already set a `timeout=` on
their `imaplib.IMAP4_SSL` connection (`IMAP_OPERATION_TIMEOUT_SECONDS` in
each module). That bounds every individual blocking `socket.recv()`/
`sendall()` call -- but imaplib's own `IMAP4.read()`/`readline()` (used by
every command this project issues: LOGIN, SELECT/STATUS, UID SEARCH/
SEARCH, UID FETCH/FETCH, CLOSE, LOGOUT) each run a `while` loop that keeps
calling `sock.recv()` for as long as the peer keeps sending *any* bytes at
all, with no cap on how many times that loop may run. A peer that sends a
single byte just before each `IMAP_OPERATION_TIMEOUT_SECONDS` window
expires never trips that per-read timeout, and can keep the loop -- and
the worker thread running it (see `fetch()`'s `asyncio.to_thread` in both
callers) -- alive indefinitely. That is the "slow-drip" class of attack
this module closes: an inactivity timeout is not a total deadline.

`ImapSessionDeadline` is a watchdog, not an `asyncio`-level timeout: it
runs a background `threading.Timer` that force-closes
(`shutdown()` + `close()`) the session's bound socket if the session
hasn't finished within `total_seconds` of real wall-clock time. Forcibly
closing the socket actually unblocks whatever blocking `recv()`/
`sendall()` call is in flight -- the worker thread is guaranteed to
observe an `OSError` (a plain `OSError`, e.g. "bad file descriptor", once
the socket is fully closed -- not necessarily a `ConnectionError`
subclass, so callers must catch `OSError` broadly, not just
`ConnectionError`) and return promptly. This is deliberately NOT
`asyncio.wait_for(asyncio.to_thread(...), timeout=...)`: that construct
stops *awaiting* the thread but does nothing to unblock the blocking call
already running inside it -- the thread (and any resources/locks it
holds) keeps running for as long as the peer keeps trickling data,
indefinitely in the worst case. Only actually interrupting the blocking
I/O call itself -- by closing the socket it's blocked on -- guarantees the
thread returns and its worker-pool slot is freed.

Scope (documented, not a silent gap): this watchdog is bound to the
session's socket AFTER the TCP connect + TLS handshake already completed
(see `GmailImapProvider._connect` / `XingEmailCollector._connect`) -- it
covers LOGIN through LOGOUT, which is where the exploit is actually
unbounded (an arbitrarily large FETCH literal trickled a byte at a time,
or arbitrarily many SEARCH results/messages fetched one at a time). The
TCP connect + TLS handshake is not given this same total-deadline
treatment: it is already bounded by both the existing per-op socket
`timeout=` AND the TLS protocol's own small, fixed number of handshake
round trips -- a peer cannot make a valid handshake take more round trips
just by trickling bytes within one of them -- so its worst case is already
finite (a small constant multiple of `IMAP_OPERATION_TIMEOUT_SECONDS`),
unlike the unbounded-iteration-count command/response phase this module
targets.
"""

from __future__ import annotations

import logging
import socket
import threading
from types import TracebackType

logger = logging.getLogger(__name__)

# AUD-005: total wall-clock budget for one IMAP session (LOGIN through
# LOGOUT) once the connection is established. Generous enough for a normal
# sync (GmailImapProvider caps one run at MAX_MESSAGES_PER_SYNC=500
# individually fetched messages) while still giving a slow-drip peer a
# fixed, finite ceiling instead of "as long as it keeps sending any bytes
# at all".
IMAP_SESSION_DEADLINE_SECONDS = 300.0


class ImapSessionDeadline:
    """Watchdog for one IMAP session: force-closes the bound socket if the
    session is not finished within `total_seconds`.

    Usage::

        deadline = ImapSessionDeadline(IMAP_SESSION_DEADLINE_SECONDS)
        with deadline:
            client = self._connect(deadline)  # binds client.sock
            ... use client for the rest of the session ...

    `bind_socket` may simply never be called (e.g. a test-injected fake
    IMAP client with no real socket) -- an unbound deadline has nothing to
    close and is a no-op watchdog.
    """

    def __init__(self, total_seconds: float) -> None:
        self._total_seconds = total_seconds
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._fired = threading.Event()
        self._timer: threading.Timer | None = None

    def bind_socket(self, sock: socket.socket | None) -> None:
        """Register (or replace) the live socket this deadline should
        force-close if it fires. Only the most recently bound socket is
        closed -- safe to call again if a session reconnects.
        """
        with self._lock:
            self._socket = sock
            already_fired = self._fired.is_set()
        if already_fired and sock is not None:
            self._force_close(sock)

    @property
    def exceeded(self) -> bool:
        return self._fired.is_set()

    def _force_close(self, sock: socket.socket) -> None:
        # shutdown() first: the documented, portable way to unblock a
        # peer thread already parked in a blocking recv()/send() on this
        # socket. close() alone is not guaranteed to interrupt a call
        # already in flight on some platforms.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _on_fire(self) -> None:
        self._fired.set()
        with self._lock:
            sock = self._socket
        if sock is not None:
            logger.warning("imap_session_deadline_exceeded")
            self._force_close(sock)

    def __enter__(self) -> ImapSessionDeadline:
        self._timer = threading.Timer(self._total_seconds, self._on_fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._timer is not None:
            self._timer.cancel()
