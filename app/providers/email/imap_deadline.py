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

FINAL-003 (Astra R5A, corrected after Codex re-review): `_create_socket`
itself still had an unbounded gap -- `imaplib.IMAP4._create_socket` calls
`socket.create_connection()`, which does DNS resolution
(`socket.getaddrinfo()`) followed by the TCP `connect()`, and there is no
socket at all yet for `bind_socket`/`_force_close` to act on during that
phase. The first version of this fix ran `getaddrinfo()`+`connect()` on a
background daemon THREAD and merely stopped waiting on it after the
deadline -- Codex correctly rejected this: Python cannot forcibly
interrupt a thread blocked inside a C-level blocking syscall (the GIL is
released for the duration, and there is no portable way to inject a
signal into one specific thread), so an "abandoned" thread just keeps
running the real `getaddrinfo()` call for however long the OS resolver
actually takes -- unbounded WORKER lifetime, even though the CALLING
thread was bounded.

The corrected fix (`ImapSessionDeadline.resolve_addrinfo_bounded`) moves
ONLY the DNS resolution step to an isolated child PROCESS, not a thread:
`multiprocessing.Process.terminate()`/`.kill()` sends a real OS-level
signal (SIGTERM/SIGKILL on POSIX, TerminateProcess on Windows) that the
OS itself enforces against the WHOLE process, including whatever
blocking syscall it is in -- this is the one primitive Python actually
has for genuinely cancelling an uninterruptible blocking call, and it is
verified by this fix's own test suite: the worker process's
`is_alive()` is confirmed False after a timeout, not merely abandoned.
Only small, plain, picklable data (resolved addresses, or an `OSError`)
crosses the process boundary -- deliberately NOT a live connected
socket, which would require platform-fragile raw file-descriptor
-passing machinery with its own cancellation race conditions. The
actual TCP `connect()` happens back in THIS process afterward (see
`DeadlineIMAP4SSL._create_socket`), against each resolved candidate
address in turn, using the SAME socket-registration-before-connect +
`_force_close` mechanism already proven for the TLS handshake/greeting
phases below -- a real socket object exists in this process from the
moment it is created, so it is immediately interruptible by the
existing mechanism with no new primitive needed, and it shares the
SAME absolute total-session deadline the rest of this class already
enforces (no separate sub-budget for the connect phase).
"""

from __future__ import annotations

import imaplib
import logging
import multiprocessing
import socket
import sys
import threading
import time
from types import TracebackType
from typing import Protocol

logger = logging.getLogger(__name__)

# FINAL-003 (Astra R5A): "spawn" is used explicitly rather than relying
# on the platform default -- this module's DNS-resolution call always
# runs from inside a worker thread (see app.collectors.xing_email
# .fetch_message_batches's asyncio.to_thread / app.providers.email.imap
# .GmailImapProvider.fetch's own docstring), so the calling process is
# always multi-threaded by the time a child process would be forked.
# "fork" on POSIX only duplicates the CALLING thread -- any lock held by
# a DIFFERENT thread at that exact moment (e.g. the logging module's own
# internal lock) is duplicated in a permanently-locked state in the
# child, a well-known fork+threads hazard. "spawn" starts a fresh
# interpreter instead, sidestepping that entirely, and is already the
# only option on Windows (this project's local dev runtime) -- using it
# explicitly everywhere keeps behavior identical across platforms
# instead of silently depending on which one happens to be the default.
_MP_CONTEXT = multiprocessing.get_context("spawn")


def _resolve_addrinfo_worker(host: str | None, port: int, conn) -> None:
    """Runs in an isolated child PROCESS -- see
    `ImapSessionDeadline.resolve_addrinfo_bounded`'s own docstring for
    why a process, not a thread, is required for genuine cancellation.
    Sends only plain, picklable data back to the parent (never a live
    socket): `("ok", addrinfo)` on success, `("error", exc)` if
    `socket.getaddrinfo` itself raised. Uses a `multiprocessing.Pipe`
    `Connection.send()` (a synchronous write, not `multiprocessing
    .Queue`'s buffered-and-flushed-by-a-background-thread `put()`) so
    the result is guaranteed to have actually reached the OS-level pipe
    before this function returns and the process exits -- a `Queue`
    here would risk silently losing the result if the process exited
    before its internal feeder thread finished flushing.
    """
    try:
        addrinfo = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        try:
            conn.send(("error", exc))
        finally:
            conn.close()
        return
    try:
        conn.send(("ok", addrinfo))
    finally:
        conn.close()


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

# FINAL-003 (Astra R5A, corrected): ceiling for how long
# ImapSessionDeadline._terminate_and_confirm_dead waits, per escalation
# step, for the DNS resolution worker PROCESS to actually die -- see
# that method's own docstring for why this is a safety-margin ceiling
# (OS-level process termination is normally observable within low
# milliseconds), not an expected-case duration, and for the exact
# worst-case total additive latency this bounds
# (2 * this constant, across the terminate()-then-kill() escalation).
# Deliberately small: a short-lived resolver worker being reaped is not
# something that should ever legitimately need seconds, and this must
# never let a short configured session deadline return many seconds
# late just because cleanup used an overly generous timeout.
_WORKER_TERMINATION_JOIN_TIMEOUT_SECONDS = 0.5


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
        # FINAL-003: set in __enter__, read by resolve_addrinfo_bounded's
        # own remaining-time wait -- the SAME reference point self._timer
        # uses, so the pre-socket DNS/connect phase never gets a budget
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
            # Defensive only -- resolve_addrinfo_bounded is only ever
            # called from inside a `with deadline:` block in practice,
            # where __enter__ has always already set this. Treat "never
            # entered" as no time left rather than blocking
            # indefinitely.
            return 0.0
        return max(0.0, self._deadline_at - time.monotonic())

    def resolve_addrinfo_bounded(
        self, host: str | None, port: int, *, _worker: object = None
    ) -> list[tuple]:
        """FINAL-003 (Astra R5A, corrected): bounds `socket.getaddrinfo`
        (DNS resolution) by this deadline's own remaining wall-clock
        budget, using a genuinely terminable, isolated child PROCESS --
        not a thread. See this module's own docstring for the full
        rationale for why a process is required (a thread stuck inside
        a blocking C-level syscall cannot be forcibly interrupted from
        another thread in Python; a process CAN be, via a real OS-level
        signal).

        Guarantees, all verified by this fix's own test suite:

        1. This method always returns or raises within (approximately)
           the deadline's remaining time.
        2. If the deadline's remaining time elapses before resolution
           completes, the worker PROCESS is `.terminate()`d (escalating
           to `.kill()` if it hasn't died within a few seconds) and its
           death is confirmed via `.join()` BEFORE this method raises --
           never left running. This is the actual fix for the Codex
           finding that the first version of this method (a background
           THREAD merely abandoned after a timeout) left the resolver
           WORKER's lifetime unbounded even though the caller's own wait
           was bounded.
        3. Only plain, picklable data (a list of `getaddrinfo()`
           5-tuples, or an `OSError`) ever crosses the process boundary
           -- never a live socket, which would require platform-fragile
           raw file-descriptor-passing with its own handoff race. There
           is therefore no "late socket" this method itself could ever
           leak; see `DeadlineIMAP4SSL._create_socket` for how the
           actual TCP connect (which DOES need late-socket cancellation
           handling) reuses the existing, already-proven
           `bind_socket`/`_force_close` mechanism instead of needing a
           new one here.

        Raises `TimeoutError` (a `socket.OSError`/`socket.timeout`
        subclass -- every existing `except OSError` call site in
        app.collectors.xing_email / app.providers.email.imap already
        covers it, unchanged) if the deadline's remaining time elapses
        before resolution completes. Re-raises whatever `OSError`
        `socket.getaddrinfo` itself raised if it completes in time.

        `_worker` (test-only seam, never passed by production code):
        overrides which target function the child process runs, so a
        test can deterministically simulate a hung/failing resolver
        without depending on real, possibly-flaky DNS. Defaults to the
        real `_resolve_addrinfo_worker`, looked up from this MODULE's
        own current namespace at call time (not captured at function
        -definition time) so `monkeypatch.setattr(imap_deadline_module,
        "_resolve_addrinfo_worker", ...)` also works transparently for
        an end-to-end test that goes through `DeadlineIMAP4SSL`'s real
        constructor, which has no seam of its own to pass `_worker`
        through (it overrides `imaplib.IMAP4_SSL._create_socket`'s fixed
        signature). A replacement MUST itself be an importable
        module-level function (never a lambda/closure) -- `spawn`
        re-imports the target module fresh in the child process, so only
        a real, by-reference-picklable function works.
        """
        worker = _worker if _worker is not None else _resolve_addrinfo_worker
        parent_conn, child_conn = _MP_CONTEXT.Pipe(duplex=False)
        process = _MP_CONTEXT.Process(target=worker, args=(host, port, child_conn), daemon=True)
        process.start()
        # This process's own reference to the child's write end must be
        # closed too (mirrors the standard os.pipe()-after-fork pattern)
        # -- otherwise it stays open here even after the child process
        # itself exits/is killed, which is unnecessary fd/handle upkeep
        # this method has no use for (it never waits on pipe EOF as a
        # signal; the process's own lifecycle -- start/join/terminate --
        # is what's tracked and bounded, not the pipe's state).
        child_conn.close()
        try:
            if parent_conn.poll(self._remaining_seconds()):
                status, payload = parent_conn.recv()
            else:
                self._fired.set()
                logger.warning("imap_session_deadline_exceeded_during_dns_resolution")
                self._terminate_and_confirm_dead(process)
                raise TimeoutError("IMAP session deadline exceeded before DNS resolution completed")
        finally:
            parent_conn.close()
            # Defensive: normally already exited after sending its
            # result, but if it somehow didn't (e.g. sent then hung
            # before its own natural exit), never leave it running past
            # this method returning.
            if process.is_alive():
                self._terminate_and_confirm_dead(process)

        if status == "error":
            raise payload
        assert status == "ok"
        return payload

    def _terminate_and_confirm_dead(self, process: multiprocessing.Process) -> None:
        """Terminates `process` and BLOCKS until its death is confirmed
        -- the actual FINAL-003 correction: a worker must never merely
        be asked to stop and then forgotten about. `.terminate()` (a
        real OS-level SIGTERM/TerminateProcess) is escalated to
        `.kill()` (SIGKILL, not interceptable) if the process hasn't
        died within `_WORKER_TERMINATION_JOIN_TIMEOUT_SECONDS`.

        Bounded total added latency, not just "usually fast": OS-level
        process termination for a process blocked in a syscall (no
        installed signal handler intercepts SIGTERM by default, and
        `TerminateProcess` on Windows is not interceptable at all) is
        normally observable within low milliseconds -- these join()
        timeouts are safety-margin CEILINGS, not expected-case
        durations. Worst case (both `.terminate()` AND the `.kill()`
        escalation each need their full budget, itself already an
        unusual/pathological outcome) this method adds at most
        `2 * _WORKER_TERMINATION_JOIN_TIMEOUT_SECONDS` of wall-clock
        time on top of whatever the session deadline itself already
        allowed -- a small, fixed, provably bounded constant, never
        several seconds, regardless of how short the configured total
        session deadline is.
        """
        process.terminate()
        process.join(timeout=_WORKER_TERMINATION_JOIN_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=_WORKER_TERMINATION_JOIN_TIMEOUT_SECONDS)
        if process.is_alive():
            # Should be unreachable (SIGKILL/TerminateProcess cannot be
            # blocked by user-level code) -- logged, never silently
            # swallowed, if the OS itself somehow failed to reap it.
            logger.warning("imap_dns_resolution_worker_still_alive_after_kill")

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
    watchdog to the real socket at each stage as soon as it exists: DNS
    resolution first (via `ImapSessionDeadline.resolve_addrinfo_bounded`
    -- see FINAL-003 in this module's own docstring), then the raw TCP
    socket is registered BEFORE `connect()` is even attempted on it (so
    a hang during connect() itself is bounded too, not just phases after
    it succeeds), then the TLS-wrapped socket immediately after the
    handshake completes (`do_handshake_on_connect=False`
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
        # FINAL-003 (Astra R5A, corrected): reimplements
        # imaplib.IMAP4._create_socket / socket.create_connection's own
        # getaddrinfo()-then-connect() logic (rather than delegating to
        # either) so DNS resolution can be bounded via a genuinely
        # terminable child process (see
        # ImapSessionDeadline.resolve_addrinfo_bounded's own docstring)
        # while the TCP connect below stays in THIS process, using the
        # same bind_socket-before-connect + _force_close mechanism
        # already proven for the TLS handshake/greeting phases -- a real
        # socket object exists here from the moment it's created, so a
        # hang during connect() itself is bounded by the EXISTING
        # mechanism, sharing the same absolute total-session deadline;
        # no new primitive or separate sub-budget is needed for this
        # phase.
        #
        # sys.audit parity: imaplib.IMAP4._create_socket calls this
        # exact audit event before connecting -- preserved here since
        # this method no longer delegates to it at all.
        sys.audit("imaplib.open", self, self.host, self.port)

        host = None if not self.host else self.host
        addrinfo = self._imap_deadline.resolve_addrinfo_bounded(host, self.port)

        last_exc: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in addrinfo:
            if self._imap_deadline.exceeded:
                # The deadline already fired (e.g. while resolving, or
                # while a PRIOR candidate's connect() attempt was
                # force-closed below) -- every further candidate would
                # just be force-closed the instant it's registered (see
                # bind_socket's own already-fired handling), so stop
                # here instead of needlessly cycling through the rest.
                break
            raw_sock: socket.socket | None = None
            try:
                raw_sock = socket.socket(family, socktype, proto)
                if timeout is not None:
                    raw_sock.settimeout(timeout)
                # Registered BEFORE connect() is attempted -- exactly
                # the same ordering already used for the TLS handshake
                # below, so a hang inside connect() itself is bounded by
                # the existing force-close mechanism, not just reads
                # after the socket is already established.
                self._imap_deadline.bind_socket(raw_sock)
                raw_sock.connect(sockaddr)
            except OSError as exc:
                last_exc = exc
                if raw_sock is not None:
                    raw_sock.close()
                continue

            ssl_sock = self.ssl_context.wrap_socket(
                raw_sock, server_hostname=self.host, do_handshake_on_connect=False
            )
            self._imap_deadline.bind_socket(ssl_sock)
            ssl_sock.do_handshake()
            return ssl_sock

        if last_exc is not None:
            raise last_exc
        raise OSError(f"getaddrinfo({self.host!r}) returned no usable address")

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
