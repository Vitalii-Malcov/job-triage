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
`imaplib.IMAP4.open()` to create a buffered reader -- `self.file` on
CPython <= 3.13, `self._file` on 3.14+, see `get_imap_makefile_reader`
below): calling `.makefile()` pins an internal reference count on the
socket object, so `sock.close()` alone silently becomes a no-op "soft
close" (a refcount decrement, not a real OS-level close) for as long as
that makefile()-derived reader is still open -- confirmed empirically
(see this fix's test suite): a thread already blocked in `sock.recv()`
(or, on CPython <= 3.13, in `self.file.readline()`, which reads through
that same buffered reader) is NOT unblocked by `shutdown()` + `close()`
alone once `.makefile()` has been called on the same socket, even though
neither call raises.

Codex's final review caught a real bug in an earlier version of this
fix, which closed that buffered reader FIRST to release the reference
before shutting down the socket: CPython's buffered I/O objects
(`io.BufferedReader`) serialize ALL of their own operations -- including
`close()` -- through one internal lock, held for the whole duration of
whatever call is in flight. If a `readline()` is already blocked inside
that reader when the watchdog fires, `extra_closable.close()` would
itself block waiting for the SAME lock, forever -- the watchdog thread
hanging is exactly the failure this whole mechanism exists to prevent
elsewhere. `_force_close` below therefore NEVER closes `extra_closable`
first. It shuts down `sock` (a lock-free syscall, always safe to call
immediately), then forces sock's REAL close by bypassing
`socket.socket`'s refcount-deferred soft-close directly (via the
stable, version-independent `_real_close`/`_io_refs` internals of
`socket.py` itself -- confirmed identical across CPython 3.11-3.14,
unlike imaplib's `self.file`/`self._file` naming) -- THIS is what
actually unblocks a thread blocked in `sock.recv()` OR in
`self.file.readline()` (the real close causes the underlying blocking
read to fail, which releases the buffered reader's lock naturally, from
the thread that was already holding it). Only once that has happened is
it safe to also close `extra_closable`, purely as tidy-up.

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

FINAL-003 (Astra R5A): `_create_socket` itself still had an unbounded
gap -- `imaplib.IMAP4._create_socket` calls `socket.create_connection()`,
which does DNS resolution (`socket.getaddrinfo()`) followed by the TCP
`connect()`, and there is no socket at all yet for `bind_socket`/
`_force_close` to act on during that phase. `ImapSessionDeadline
.run_bounded` closes this: it runs that call on a background thread and
bounds the CALLING thread's wait to the deadline's own remaining time,
without ever leaving the background thread's eventual result
unhandled -- see its own docstring for the full detail and for why this
is not the "spawn an unbounded resolver thread and walk away" anti
-pattern it deliberately avoids.
"""

from __future__ import annotations

import imaplib
import logging
import socket
import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Protocol

logger = logging.getLogger(__name__)


class HasClose(Protocol):
    """Anything closeable -- in practice, the file object returned by
    `socket.makefile()` (`imaplib.IMAP4.open()`'s `self.file`/
    `self._file` -- see `get_imap_makefile_reader`)."""

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
        # FINAL-003: set in __enter__, read by run_bounded's own
        # remaining-time wait -- the SAME reference point self._timer
        # uses, so the pre-socket connect phase never gets a budget
        # larger than the total session deadline.
        self._deadline_at: float | None = None

    def bind_socket(
        self, sock: socket.socket | None, *, extra_closable: HasClose | None = None
    ) -> None:
        """Register (or replace) the live socket this deadline should
        force-close if it fires. Only the most recently bound socket
        (and `extra_closable`) is used -- safe to call again if a
        session reconnects or progresses to a new socket object.

        `extra_closable`: an optional companion object (e.g. imaplib's
        `self.file`/`self._file`, from `socket.makefile()`) that is
        ALSO closed, purely for tidy-up -- see this module's docstring
        for why `sock.close()` alone can silently fail to release the
        underlying file descriptor while a
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

    def _remaining_seconds(self) -> float:
        if self._deadline_at is None:
            # Defensive only -- run_bounded is only ever called from
            # inside a `with deadline:` block in practice, where
            # __enter__ has always already set this. Treat "never
            # entered" as no time left rather than blocking
            # indefinitely.
            return 0.0
        return max(0.0, self._deadline_at - time.monotonic())

    def run_bounded(self, fn: Callable[[], socket.socket]) -> socket.socket:
        """FINAL-003: bounds a blocking call that PRODUCES a socket (in
        practice, `imaplib.IMAP4._create_socket` -- DNS resolution via
        `socket.getaddrinfo()` followed by the TCP `connect()`) by this
        deadline's own remaining wall-clock budget, closing the gap left
        by `bind_socket`/`_force_close` alone: those can only ever
        interrupt a socket that already exists, so before this method
        existed, the entire DNS-resolution-plus-connect phase ran
        completely unbounded by the session deadline -- a slow/
        unresponsive DNS resolver could block the calling thread
        indefinitely, with no socket yet for the watchdog to force-close.

        Python cannot forcibly interrupt a blocking `getaddrinfo()`/
        `connect()` syscall from another thread -- there is no portable
        equivalent of `_force_close`'s socket-shutdown trick for a
        socket that doesn't exist yet, and a signal-based approach
        (`signal.alarm`) is POSIX-only and would not work on this
        project's Windows dev runtime. So `fn` genuinely does run on a
        background daemon thread for however long the underlying OS
        call actually takes -- exactly like any other blocking Python
        call. What this method actually guarantees, and what makes it
        NOT "spawn a resolver thread and abandon it":

        1. This method itself always returns or raises within
           (approximately) the deadline's remaining time -- the calling
           thread is never blocked past the total session deadline just
           because no socket existed yet.
        2. If `fn` only succeeds AFTER this method has already given up
           and raised, the resulting socket is closed immediately by the
           background thread itself -- never silently handed back, never
           left open and unmanaged/leaked.

        Exactly one side of a lock-guarded phase handoff "wins" this
        race -- the same proven pattern as the SMTP total-deadline fix's
        `gate_lock`/phase state machine
        (app/providers/email/smtp.py's `send()`), applied here to a
        socket-producing call instead of a send.

        Raises `TimeoutError` (a `socket.OSError`/`socket.timeout`
        subclass -- every existing `except OSError` call site in
        app.collectors.xing_email / app.providers.email.imap already
        covers it, unchanged) if the deadline's remaining time elapses
        before `fn` completes. Re-raises whatever `fn` itself raised if
        it completes in time.
        """
        phase_lock = threading.Lock()
        state = {"phase": "pending"}
        outcome: dict[str, object] = {}
        done = threading.Event()

        def _worker() -> None:
            try:
                sock = fn()
            except Exception as exc:  # noqa: BLE001 -- forwarded to the caller verbatim
                with phase_lock:
                    delivered = state["phase"] == "pending"
                    if delivered:
                        outcome["error"] = exc
                        state["phase"] = "delivered"
                if delivered:
                    done.set()
                return
            with phase_lock:
                delivered = state["phase"] == "pending"
                if delivered:
                    outcome["sock"] = sock
                    state["phase"] = "delivered"
            if delivered:
                done.set()
                return
            # The caller already gave up (deadline exceeded) by the time
            # this connect finally completed -- close the now-orphaned
            # socket instead of leaking an open, unmanaged connection.
            try:
                sock.close()
            except OSError:
                pass

        threading.Thread(target=_worker, daemon=True).start()

        finished = done.wait(timeout=self._remaining_seconds())
        if not finished:
            with phase_lock:
                # Only claim the timeout if the worker has not ALREADY
                # delivered a result in the tiny window between wait()
                # timing out and this thread acquiring the lock -- if it
                # has, use that real result instead of falsely reporting
                # a timeout.
                still_pending = state["phase"] == "pending"
                if still_pending:
                    state["phase"] = "abandoned_by_caller"
            if still_pending:
                self._fired.set()
                logger.warning("imap_session_deadline_exceeded_before_connect")
                raise TimeoutError(
                    "IMAP session deadline exceeded before a connection could be established"
                )

        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        sock = outcome["sock"]
        assert isinstance(sock, socket.socket)
        return sock

    def _force_close(self, sock: socket.socket, extra_closable: HasClose | None) -> None:
        # Codex final review: closing `extra_closable` (a
        # socket.makefile()-derived buffered reader) FIRST -- the
        # previous version of this fix -- is itself unsafe: CPython's
        # buffered I/O objects (`io.BufferedReader`, what `self.file`/
        # `self._file` actually is) serialize ALL operations, including
        # close(), through one internal lock. If a readline() is
        # currently blocked reading from the underlying socket, it is
        # holding that lock for the whole blocking duration -- calling
        # `extra_closable.close()` from THIS (watchdog) thread would
        # then itself block waiting for the same lock, forever, which is
        # exactly the "watchdog can hang" failure this order must never
        # produce. Confirmed empirically (see this fix's test suite).
        #
        # shutdown() first, unconditionally: cheap, never blocks (a
        # plain socket-level syscall, no Python-level lock involved), and
        # on some platforms/situations is already sufficient on its own.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        # Then force the REAL close of `sock` -- bypassing
        # socket.socket's `_io_refs`-deferred "soft close" (see module
        # docstring: a plain `sock.close()` here would silently no-op
        # while `extra_closable` is still open, since it still holds a
        # reference). `_real_close`/`_io_refs` are stable, version-
        # independent socket.py internals (confirmed identical on
        # CPython 3.11 through 3.14) -- not the imaplib attribute this
        # fix's Problem 1 is about -- but a missing-attribute fallback is
        # kept anyway so a hypothetical future rename degrades to the
        # pre-existing (still finite, per-op-timeout-bounded) behavior
        # instead of raising.
        self._force_real_close(sock)
        # Only NOW -- after the real close, so nothing can still be
        # legitimately blocked inside it holding its lock -- clean up
        # the makefile()-derived wrapper. This is cosmetic (releases the
        # Python-level file object promptly instead of waiting for GC),
        # not required for the unblocking guarantee above.
        if extra_closable is not None:
            try:
                extra_closable.close()
            except OSError:
                pass

    def _force_real_close(self, sock: socket.socket) -> None:
        real_close = getattr(sock, "_real_close", None)
        if callable(real_close):
            try:
                real_close()
                return
            except Exception:
                return
        # Fallback if a future socket implementation ever removes
        # _real_close: zero the refcount bookkeeping directly so the
        # normal close() below performs the real close instead of
        # deferring it.
        if hasattr(sock, "_io_refs"):
            try:
                sock._io_refs = 0
            except Exception:
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
        self._deadline_at = time.monotonic() + self._total_seconds
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


def get_imap_makefile_reader(imap_client: imaplib.IMAP4) -> HasClose | None:
    """Returns `imaplib.IMAP4`'s internal `socket.makefile()`-backed
    reader, tolerant of its private-attribute rename across CPython
    versions this project supports (>=3.11, per pyproject.toml; CI runs
    3.13):

    * CPython <= 3.13: `self.file`
    * CPython 3.14+: `self._file`

    Never hard-codes either name alone -- `imaplib.IMAP4.open()`'s
    docstring/implementation is not a public API contract, so this
    checks both and returns whichever is actually present, preferring
    `_file` (the newer name) only because that's what this project's
    primary development runtime (3.14) uses; the order does not matter
    functionally since only one of the two ever exists on a given
    version.

    Returns None if NEITHER attribute exists (a hypothetical future
    CPython rename this project hasn't seen yet). That is a safe,
    explicit degrade, not a silent one: `ImapSessionDeadline._force_close`
    does not need this object to guarantee the core unblocking property
    (it forces the real socket close directly -- see that method's
    docstring) -- this reader is only used for a cosmetic follow-up
    close, so returning None here just skips that cleanup rather than
    reintroducing any blocking risk.
    """
    for attr in ("_file", "file"):
        candidate = getattr(imap_client, attr, None)
        if candidate is not None:
            return candidate
    return None


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
    caller of `_create_socket`) additionally creates a makefile()-backed
    reader for buffered reads -- `self.file` on CPython <= 3.13,
    `self._file` on CPython 3.14+ (an unannounced private rename between
    versions; see `get_imap_makefile_reader` below, which this class
    uses instead of hard-coding either name). That companion object is
    not optional decoration to track -- confirmed empirically (see this
    fix's test suite): `socket.makefile()` pins a reference count on the
    socket, so `self.sock.close()` alone silently becomes a no-op "soft
    close" for as long as that reader is still open, which is exactly
    the state the connection is in for the ENTIRE rest of the session
    (greeting/CAPABILITY, LOGIN, SELECT/SEARCH/FETCH/CLOSE/LOGOUT).
    `ImapSessionDeadline._force_close` handles releasing that reference
    safely -- see its docstring for why the ORDER matters (closing the
    makefile reader before forcing the real socket close can itself
    deadlock the watchdog thread).
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
        # FINAL-003: imaplib.IMAP4._create_socket does socket.getaddrinfo()
        # (DNS resolution) followed by connect() -- see
        # ImapSessionDeadline.run_bounded's own docstring for why that
        # whole phase was previously completely unbounded by the session
        # deadline (no socket exists yet for bind_socket/_force_close to
        # act on) and how run_bounded closes that gap without spawning an
        # abandoned resolver thread.
        raw_sock = self._imap_deadline.run_bounded(
            lambda: imaplib.IMAP4._create_socket(self, timeout)
        )
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
        # AUD-005 (Codex final review, HIGH): self.sock is already bound
        # (via _create_socket above), but the makefile()-backed reader
        # IMAP4.open() just created (self.file on CPython <=3.13,
        # self._file on 3.14+ -- get_imap_makefile_reader handles the
        # rename, never hard-coding one) is not -- rebind including it
        # for the cosmetic cleanup ImapSessionDeadline._force_close does
        # AFTER forcing the real socket close (see that method's
        # docstring for why the order matters and why finding this
        # reader is not required for the core unblocking guarantee).
        self._imap_deadline.bind_socket(self.sock, extra_closable=get_imap_makefile_reader(self))
