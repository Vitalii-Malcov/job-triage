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
runs a background `threading.Timer` that force-closes the session's bound
socket if the session hasn't finished within `total_seconds` of real
wall-clock time. Forcibly closing the socket actually unblocks whatever
blocking `recv()`/`sendall()` call is in flight -- the worker thread is
guaranteed to observe an `OSError` (a plain `OSError`, e.g. "bad file
descriptor", once the socket is fully closed -- not necessarily a
`ConnectionError` subclass, so callers must catch `OSError` broadly, not
just `ConnectionError`) and return promptly. This is deliberately NOT
`asyncio.wait_for(asyncio.to_thread(...), timeout=...)`: that construct
stops *awaiting* the thread but does nothing to unblock the blocking call
already running inside it -- the thread (and any resources/locks it
holds) keeps running for as long as the peer keeps trickling data,
indefinitely in the worst case. Only actually interrupting the blocking
I/O call itself -- by closing the socket it's blocked on -- guarantees the
thread returns and its worker-pool slot is freed.

Force-closing must also account for `socket.makefile()` (used by
`imaplib.IMAP4.open()` to create `self._file`): calling `.makefile()`
pins an internal reference count on the socket object, so
`sock.close()` alone silently becomes a no-op "soft close" (a refcount
decrement, not a real OS-level close) for as long as that makefile()
derived file object is still open -- confirmed empirically (see this
fix's test suite): a thread already blocked in `sock.recv()` is NOT
unblocked by `shutdown()` + `close()` alone once `.makefile()` has been
called on the same socket, even though neither call raises. `bind_socket`
below therefore accepts an optional `extra_closable` (imaplib's
`self._file`) that is closed FIRST, mirroring the ordering
`imaplib.IMAP4.shutdown()` itself uses (`self._file.close()` before
`self.sock.shutdown()`/`close()`) -- releasing that reference so the
socket close actually takes effect and the blocked read is really
unblocked, not left hanging behind a refcount.

Scope: `DeadlineIMAP4SSL` (below) binds the watchdog to the real socket
from the moment it exists -- the raw TCP socket right after connecting,
then the TLS-wrapped socket right after the handshake completes -- so the
deadline is already active before `imaplib.IMAP4.__init__` proceeds to
`IMAP4._connect()`, which reads the server's greeting and CAPABILITY
response. An earlier version of this fix only called `bind_socket` AFTER
`imaplib.IMAP4_SSL(...)` had already returned; that left the TCP
connect + TLS handshake + greeting/CAPABILITY exchange -- everything
`imaplib.IMAP4_SSL(...)`'s constructor itself does -- completely
unprotected, so a slow-drip peer during the greeting/CAPABILITY exchange
could keep the constructor call (and the worker thread blocked inside it)
alive indefinitely, exactly the class of bug this module exists to close.
`DeadlineIMAP4SSL` closes that gap by binding the deadline to the socket
from inside `_create_socket`, which `IMAP4.open()` calls (setting
`self.sock`) BEFORE `IMAP4.__init__` ever calls `IMAP4._connect()`.
"""

from __future__ import annotations

import imaplib
import logging
import socket
import threading
from types import TracebackType
from typing import Protocol

logger = logging.getLogger(__name__)


class HasClose(Protocol):
    """Anything closeable -- in practice, the file object returned by
    `socket.makefile()` (`imaplib.IMAP4.open()`'s `self._file`)."""

    def close(self) -> None: ...


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
            client = DeadlineIMAP4SSL(host, port, ssl_context=ctx,
                                       timeout=t, deadline=deadline)
            ... use client for the rest of the session ...

    `bind_socket` may simply never be called (e.g. a test-injected fake
    IMAP client with no real socket) -- an unbound deadline has nothing to
    close and is a no-op watchdog. `bind_socket` is also safe to call
    more than once in quick succession on the SAME deadline (see
    `DeadlineIMAP4SSL`, which rebinds raw-TCP-socket -> TLS-socket ->
    fully-opened-IMAP4-connection during construction): each call
    replaces the previously bound socket/closable pair under a lock, and
    if the deadline already fired by the time a later `bind_socket` call
    runs, the newly bound socket is force-closed immediately instead of
    being left unprotected.
    """

    def __init__(self, total_seconds: float) -> None:
        self._total_seconds = total_seconds
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._extra_closable: HasClose | None = None
        self._fired = threading.Event()
        self._timer: threading.Timer | None = None

    def bind_socket(
        self, sock: socket.socket | None, *, extra_closable: HasClose | None = None
    ) -> None:
        """Register (or replace) the live socket this deadline should
        force-close if it fires. Only the most recently bound socket
        (and `extra_closable`) is used -- safe to call again if a
        session reconnects or progresses to a new socket object.

        `extra_closable`: an optional companion object (e.g. imaplib's
        `self._file`, from `socket.makefile()`) that must ALSO be closed
        -- see this module's docstring for why `sock.close()` alone can
        silently fail to release the underlying file descriptor while a
        makefile()-derived object on the same socket is still open.
        """
        with self._lock:
            self._socket = sock
            self._extra_closable = extra_closable
            already_fired = self._fired.is_set()
        if already_fired and sock is not None:
            self._force_close(sock, extra_closable)

    @property
    def exceeded(self) -> bool:
        return self._fired.is_set()

    def _force_close(self, sock: socket.socket, extra_closable: HasClose | None) -> None:
        # extra_closable FIRST: releases socket.makefile()'s reference
        # count on `sock` (see module docstring) so the shutdown()/
        # close() below actually take effect instead of being deferred.
        if extra_closable is not None:
            try:
                extra_closable.close()
            except OSError:
                pass
        # shutdown() before close(): the documented, portable way to
        # unblock a peer thread already parked in a blocking recv()/
        # send() on this socket. close() alone is not guaranteed to
        # interrupt a call already in flight on some platforms.
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
            extra_closable = self._extra_closable
        if sock is not None:
            logger.warning("imap_session_deadline_exceeded")
            self._force_close(sock, extra_closable)

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


class DeadlineIMAP4SSL(imaplib.IMAP4_SSL):
    """An `imaplib.IMAP4_SSL` whose socket is registered with an
    `ImapSessionDeadline` from the moment it exists, not just after this
    constructor returns.

    AUD-005 (narrow re-review): `imaplib.IMAP4_SSL(...)`'s constructor
    itself performs blocking protocol work before returning at all -- TCP
    connect, TLS handshake, and (via `IMAP4.__init__` calling
    `IMAP4._connect()` right after `IMAP4.open()` sets `self.sock`)
    reading the server's greeting and CAPABILITY response. Binding the
    deadline only after the constructor call returns leaves ALL of that
    unprotected -- a slow-drip peer during the greeting/CAPABILITY
    exchange can keep the constructor call, and the worker thread blocked
    inside it, alive indefinitely.

    This subclass overrides `_create_socket` -- called by `IMAP4.open()`
    to produce the value assigned to `self.sock`, itself called from
    `IMAP4.__init__` BEFORE `IMAP4._connect()` runs -- to bind the
    watchdog to the real socket at each stage as soon as it exists: the
    raw TCP socket immediately after connecting, then the TLS-wrapped
    socket immediately after the handshake completes (`do_handshake_on_connect=False`
    defers the handshake until AFTER the final socket object is
    registered, so a slow-drip peer during the handshake itself is
    bounded too -- not just the greeting/CAPABILITY/command exchange that
    follows it). `ImapSessionDeadline.bind_socket` is race-safe across
    this raw-socket -> TLS-socket handoff: if the deadline fires in the
    narrow window between the two `bind_socket` calls (during which the
    raw socket's file descriptor has already been detached into the new
    TLS socket object by `wrap_socket`, so closing the stale raw-socket
    reference would be a no-op), the second `bind_socket` call sees
    `exceeded` already true and force-closes the TLS socket immediately
    instead of leaving it unprotected.

    A THIRD bind happens in `open()`, after `IMAP4.open()` (the immediate
    caller of `_create_socket`) additionally creates `self._file =
    self.sock.makefile('rb')`. That rebind is not optional decoration --
    confirmed empirically (see this fix's test suite): `socket.makefile()`
    pins a reference count on the socket, so `self.sock.close()` alone
    silently becomes a no-op "soft close" for as long as `self._file` is
    still open, which is exactly the state the connection is in for the
    ENTIRE rest of the session (greeting/CAPABILITY, LOGIN,
    SELECT/SEARCH/FETCH/CLOSE/LOGOUT). Without also closing `self._file`,
    a deadline fire during any of that would silently fail to unblock the
    blocked read despite `shutdown()`/`close()` both reporting success.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: object,
        timeout: float,
        deadline: ImapSessionDeadline,
    ) -> None:
        self._imap_deadline = deadline
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        raw_sock = imaplib.IMAP4._create_socket(self, timeout)
        self._imap_deadline.bind_socket(raw_sock)
        ssl_sock = self.ssl_context.wrap_socket(
            raw_sock, server_hostname=self.host, do_handshake_on_connect=False
        )
        self._imap_deadline.bind_socket(ssl_sock)
        ssl_sock.do_handshake()
        return ssl_sock

    def open(
        self, host: str = "", port: int = imaplib.IMAP4_SSL_PORT, timeout: float | None = None
    ) -> None:
        super().open(host, port, timeout)
        # AUD-005: self.sock is already bound (via _create_socket above),
        # but self._file (assigned by IMAP4.open() right after
        # _create_socket returns) is not -- rebind including it so a
        # deadline fire during the greeting/CAPABILITY read that follows
        # (and everything after) actually releases the socket instead of
        # being silently deferred by makefile()'s reference count. See
        # this class's docstring and ImapSessionDeadline.bind_socket's.
        self._imap_deadline.bind_socket(self.sock, extra_closable=self._file)
