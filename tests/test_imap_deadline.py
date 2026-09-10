"""Tests for app.providers.email.imap_deadline (AUD-005).

`ImapSessionDeadline` itself is tested with real, unencrypted TCP sockets
(no imaplib/TLS involved) -- the mechanism is socket-agnostic and this
keeps most of these tests fast and deterministic while still exercising
real blocking I/O and real cross-thread socket closure, not a mock.

`DeadlineIMAP4SSL` (the narrow AUD-005 re-review fix: binding the
deadline to the socket from inside the constructor, before the
greeting/CAPABILITY reads, not just after `imaplib.IMAP4_SSL(...)`
returns) is tested with a REAL local TLS server -- real TCP connect, real
TLS handshake, real `imaplib` greeting/CAPABILITY parsing -- to actually
reproduce the Codex-flagged vulnerability class, not just re-test
`ImapSessionDeadline` in isolation. The server's self-signed certificate
is generated via the `openssl` CLI (skipped if unavailable); the TEST
client's `ssl.SSLContext` deliberately disables verification so a
throwaway self-signed cert works -- AUD-001's CERT_REQUIRED/
check_hostname=True invariant is a property of what `GmailImapProvider`/
`XingEmailCollector._connect()` PASS to `DeadlineIMAP4SSL`, not of
`DeadlineIMAP4SSL` itself, and is covered separately (see
tests/test_providers_email_imap.py and
tests/test_collectors_xing_email.py's `test_connect_passes_a_verifying_*`
tests).
"""

import imaplib
import multiprocessing
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.providers.email.imap_deadline import (
    DeadlineIMAP4SSL,
    ImapSessionDeadline,
    get_imap_makefile_reader,
)


def _listening_server() -> tuple[socket.socket, tuple[str, int]]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()


def test_exceeded_is_false_before_the_deadline_fires():
    deadline = ImapSessionDeadline(5.0)
    with deadline:
        assert deadline.exceeded is False


def test_deadline_does_not_fire_when_the_operation_completes_in_time():
    """A fast, normal exchange must not be disturbed by the watchdog, and
    the socket must remain open/usable afterward."""
    server, (host, port) = _listening_server()

    def _accept_and_reply():
        conn, _ = server.accept()
        conn.sendall(b"hello")
        conn.close()

    server_thread = threading.Thread(target=_accept_and_reply, daemon=True)
    server_thread.start()
    client_sock = socket.create_connection((host, port), timeout=5)
    try:
        deadline = ImapSessionDeadline(5.0)
        with deadline:
            deadline.bind_socket(client_sock)
            data = client_sock.recv(5)
        assert data == b"hello"
        assert deadline.exceeded is False
    finally:
        server.close()
        server_thread.join(timeout=3)
        client_sock.close()


def test_deadline_bounds_a_completely_silent_peer():
    """A peer that accepts the connection and then sends nothing at all.
    Proves the watchdog alone (no per-op socket timeout at all is set on
    this client socket) still unblocks a thread parked in recv()."""
    server, (host, port) = _listening_server()
    accepted = threading.Event()

    def _accept_and_stay_silent():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        accepted.set()
        time.sleep(5)
        conn.close()

    server_thread = threading.Thread(target=_accept_and_stay_silent, daemon=True)
    server_thread.start()
    # Deliberately no timeout= on this socket: proves ImapSessionDeadline
    # itself -- not some other per-op timeout -- is what bounds the hang.
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_sock.connect((host, port))
        assert accepted.wait(timeout=2), "test server never accepted the connection"

        deadline = ImapSessionDeadline(0.3)
        result: dict[str, object] = {}

        def _blocked_recv():
            try:
                result["data"] = client_sock.recv(4096)
            except OSError as exc:
                result["error"] = exc

        with deadline:
            deadline.bind_socket(client_sock)
            start = time.monotonic()
            worker = threading.Thread(target=_blocked_recv, daemon=True)
            worker.start()
            worker.join(timeout=2.0)
            elapsed = time.monotonic() - start

        assert not worker.is_alive(), "the blocked recv() thread was not unblocked by the deadline"
        assert elapsed < 2.0, f"the deadline did not bound the hang (took {elapsed:.2f}s)"
        assert deadline.exceeded is True
        assert "data" in result or "error" in result
    finally:
        server.close()
        server_thread.join(timeout=3)
        client_sock.close()


def test_deadline_bounds_a_slow_drip_peer_by_total_elapsed_time():
    """The core AUD-005 scenario: a peer that keeps sending a trickle of
    bytes fast enough that a naive per-read inactivity timeout would keep
    getting reset forever. Proves ImapSessionDeadline bounds the TOTAL
    elapsed time regardless of that ongoing activity."""
    server, (host, port) = _listening_server()
    accepted = threading.Event()
    stop_drip = threading.Event()

    def _accept_and_drip():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        accepted.set()
        try:
            while not stop_drip.is_set():
                conn.sendall(b"x")
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()

    server_thread = threading.Thread(target=_accept_and_drip, daemon=True)
    server_thread.start()
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_sock.connect((host, port))
        assert accepted.wait(timeout=2), "test server never accepted the connection"

        deadline = ImapSessionDeadline(0.3)
        result = {"total_received": 0}

        def _keep_reading():
            try:
                while True:
                    chunk = client_sock.recv(1)
                    if not chunk:
                        break
                    result["total_received"] += 1
            except OSError:
                pass

        with deadline:
            deadline.bind_socket(client_sock)
            start = time.monotonic()
            worker = threading.Thread(target=_keep_reading, daemon=True)
            worker.start()
            # The drip (every 0.05s) would keep a naive per-read
            # inactivity timeout alive forever. Give this join far more
            # time than the 0.3s total deadline to prove it's bounded by
            # TOTAL elapsed time, not reset by the ongoing activity.
            worker.join(timeout=3.0)
            elapsed = time.monotonic() - start

        stop_drip.set()
        assert not worker.is_alive(), (
            "slow-drip activity kept the reader thread alive past the total deadline"
        )
        assert elapsed < 1.5, (
            f"total elapsed time was not bounded by the deadline (took {elapsed:.2f}s)"
        )
        assert deadline.exceeded is True
        # The peer sent well beyond one byte during the drip window before
        # firing bounded it -- proves activity was really happening and
        # still didn't extend the deadline.
        assert result["total_received"] >= 1
    finally:
        stop_drip.set()
        server.close()
        server_thread.join(timeout=3)
        client_sock.close()


@pytest.mark.parametrize("total_seconds", [0.05])
def test_bind_socket_after_the_deadline_already_fired_closes_immediately(total_seconds):
    """A late bind_socket() call (e.g. a reconnect racing the watchdog)
    must still be force-closed if the deadline already fired -- the
    watchdog must not silently stop protecting a newly bound socket."""
    deadline = ImapSessionDeadline(total_seconds)
    with deadline:
        assert deadline.exceeded is False
        time.sleep(total_seconds * 4)
        assert deadline.exceeded is True

        late_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            deadline.bind_socket(late_sock)
            # A closed socket's fileno() is -1.
            assert late_sock.fileno() == -1
        finally:
            late_sock.close()


# ---------------------------------------------------------------------------
# DeadlineIMAP4SSL (AUD-005 narrow re-review): the deadline must be bound
# to the socket from inside imaplib.IMAP4_SSL's own constructor -- before
# IMAP4._connect() reads the greeting/CAPABILITY response -- not only
# after the constructor returns.
# ---------------------------------------------------------------------------


def _generate_self_signed_cert(tmp_path: Path) -> tuple[str, str] | None:
    """Generates a throwaway self-signed cert+key via the `openssl` CLI.
    Returns None (caller should skip) if `openssl` isn't on PATH.
    """
    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert_path), str(key_path)


def _relaxed_test_client_ssl_context() -> ssl.SSLContext:
    """A TEST-ONLY client context that trusts nothing and checks nothing
    -- deliberately NOT `ssl.create_default_context()` (AUD-001's
    verifying context, tested separately at the provider level). A
    throwaway self-signed cert wouldn't pass CERT_REQUIRED verification,
    and that's not what these tests are checking.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _start_tls_server(
    certfile: str, keyfile: str, handle_connection
) -> tuple[socket.socket, tuple[str, int], threading.Thread, threading.Event]:
    """Starts a background thread that accepts one TCP connection, TLS-wraps
    it server-side, and hands the live TLS socket to `handle_connection`.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    accepted = threading.Event()

    def _run():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            tls_conn = server_ctx.wrap_socket(conn, server_side=True)
        except OSError:
            return
        accepted.set()
        try:
            handle_connection(tls_conn)
        finally:
            try:
                tls_conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return server, (host, port), thread, accepted


def test_constructor_greeting_capability_slow_drip_is_bounded_by_the_total_deadline(tmp_path):
    """Reproduces the exact Codex-flagged gap: a peer that completes the
    TLS handshake and then trickles bytes of a NEVER-terminated IMAP
    greeting line (imaplib's greeting read blocks until it sees a
    trailing CRLF) slowly enough to stay under the per-op inactivity
    timeout forever. Before this fix, ImapSessionDeadline had no socket
    reference to close during this phase -- the constructor call
    (imaplib.IMAP4_SSL.__init__ -> IMAP4._connect() -> _get_response())
    could hang past the total deadline. DeadlineIMAP4SSL must bound it.
    """
    cert = _generate_self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("openssl CLI not available to generate a self-signed test certificate")
    certfile, keyfile = cert
    stop_drip = threading.Event()

    def _drip_unterminated_greeting(tls_conn: ssl.SSLSocket) -> None:
        while not stop_drip.is_set():
            try:
                tls_conn.send(b"x")
            except OSError:
                break
            time.sleep(0.05)

    server, (host, port), server_thread, accepted = _start_tls_server(
        certfile, keyfile, _drip_unterminated_greeting
    )
    try:
        # FINAL-003 (Astra R5A): DeadlineIMAP4SSL's construction now
        # ALSO includes DNS resolution via a genuinely terminable child
        # PROCESS (see ImapSessionDeadline.resolve_addrinfo_bounded) --
        # spawning that process costs real, measurable, somewhat
        # load-dependent wall-clock time on this machine (confirmed
        # empirically: ~150-400ms in isolation, more under load), which
        # a pre-FINAL-003 0.4s deadline did not need to budget for at
        # all (DNS resolution used to be effectively instant, delegated
        # straight to the OS resolver in-process). 2.0s leaves ample
        # margin for that spawn cost to complete BEFORE this test's own
        # intended scenario (the greeting slow-drip) even begins to
        # matter, so the deadline reliably fires during the DRIP, not
        # during DNS resolution itself.
        deadline = ImapSessionDeadline(2.0)
        result: dict[str, object] = {}

        def _construct() -> None:
            try:
                with deadline:
                    DeadlineIMAP4SSL(
                        host,
                        port,
                        ssl_context=_relaxed_test_client_ssl_context(),
                        timeout=5.0,
                        deadline=deadline,
                    )
            except Exception as exc:  # noqa: BLE001 -- captured for assertion, not swallowed
                result["error"] = exc

        start = time.monotonic()
        worker = threading.Thread(target=_construct, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        elapsed = time.monotonic() - start
        stop_drip.set()

        assert accepted.wait(timeout=2), "test TLS server never completed the handshake"
        assert not worker.is_alive(), (
            "the constructor (blocked reading the greeting/CAPABILITY response) "
            "was not unblocked by the total session deadline"
        )
        assert elapsed < 4.0, (
            f"construction was not bounded by the total deadline (took {elapsed:.2f}s)"
        )
        assert deadline.exceeded is True
        assert "error" in result, (
            "construction should have raised once the deadline force-closed the socket"
        )
    finally:
        stop_drip.set()
        server.close()
        server_thread.join(timeout=3)


def test_constructor_silent_peer_after_handshake_is_bounded_by_the_total_deadline(tmp_path):
    """Same constructor-time phase as the slow-drip test above, but the
    peer sends nothing at all after completing the handshake."""
    cert = _generate_self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("openssl CLI not available to generate a self-signed test certificate")
    certfile, keyfile = cert

    def _stay_silent(tls_conn: ssl.SSLSocket) -> None:
        time.sleep(5)

    server, (host, port), server_thread, accepted = _start_tls_server(
        certfile, keyfile, _stay_silent
    )
    try:
        # FINAL-003 (Astra R5A): see the identical comment in
        # test_constructor_greeting_capability_slow_drip_is_bounded_by_the_total_deadline
        # above -- DNS resolution now spawns a genuinely terminable
        # child process, which costs real wall-clock time a
        # pre-FINAL-003 0.4s deadline never had to budget for.
        deadline = ImapSessionDeadline(2.0)
        result: dict[str, object] = {}

        def _construct() -> None:
            try:
                with deadline:
                    DeadlineIMAP4SSL(
                        host,
                        port,
                        ssl_context=_relaxed_test_client_ssl_context(),
                        timeout=5.0,
                        deadline=deadline,
                    )
            except Exception as exc:  # noqa: BLE001 -- captured for assertion, not swallowed
                result["error"] = exc

        start = time.monotonic()
        worker = threading.Thread(target=_construct, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        elapsed = time.monotonic() - start

        assert accepted.wait(timeout=2), "test TLS server never completed the handshake"
        assert not worker.is_alive(), (
            "the constructor (blocked reading the greeting) was not unblocked "
            "by the total session deadline"
        )
        assert elapsed < 4.0, (
            f"construction was not bounded by the total deadline (took {elapsed:.2f}s)"
        )
        assert deadline.exceeded is True
        assert "error" in result
    finally:
        server.close()
        server_thread.join(timeout=3)


def test_deadline_imap4ssl_normal_handshake_and_greeting_still_succeeds(tmp_path):
    """A fast, well-behaved server (real handshake, real terminated
    greeting + CAPABILITY response) must not be disturbed by
    DeadlineIMAP4SSL's constructor-time binding."""
    cert = _generate_self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("openssl CLI not available to generate a self-signed test certificate")
    certfile, keyfile = cert

    def _reply_normally(tls_conn: ssl.SSLSocket) -> None:
        tls_conn.sendall(b"* OK IMAP4rev1 Service Ready\r\n")
        request = b""
        while not request.endswith(b"\r\n"):
            chunk = tls_conn.recv(4096)
            if not chunk:
                return
            request += chunk
        tag = request.split(b" ", 1)[0]
        tls_conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK CAPABILITY completed\r\n")
        # Let the client proceed to LOGOUT/close on its own; just keep the
        # connection open briefly so it doesn't see a premature EOF.
        time.sleep(0.2)

    server, (host, port), server_thread, accepted = _start_tls_server(
        certfile, keyfile, _reply_normally
    )
    try:
        deadline = ImapSessionDeadline(5.0)
        with deadline:
            client = DeadlineIMAP4SSL(
                host,
                port,
                ssl_context=_relaxed_test_client_ssl_context(),
                timeout=5.0,
                deadline=deadline,
            )
        assert accepted.wait(timeout=2)
        assert deadline.exceeded is False
        client.shutdown()
    finally:
        server.close()
        server_thread.join(timeout=3)


# ---------------------------------------------------------------------------
# Codex final review: (1) imaplib's makefile()-backed reader attribute is
# named `self.file` on CPython <= 3.13 and `self._file` on 3.14+ --
# get_imap_makefile_reader must not hard-code either one. (2) closing that
# reader BEFORE forcing the real socket close can itself deadlock the
# watchdog thread if a readline() is in flight through it -- the fix must
# prove it never does, not just reverse the two calls.
#
# This project's own runtime (see `sys.version_info` below) is what's
# actually running these tests; get_imap_makefile_reader's own branch
# selection is exercised synthetically against fake objects
# (TestGetImapMakefileReader below) for interpreter layouts other than
# whichever one is actually installed here. The end-to-end
# _LegacyAttributeDeadlineIMAP4SSL tests below are portable across BOTH
# known real imaplib layouts (see that class's own docstring): CI runs
# CPython 3.13.15, where imaplib.IMAP4.open() already assigns self.file
# directly (the native <=3.13 branch is exercised for real there); local
# dev runs CPython 3.14+, where self._file is assigned and renamed onto
# self.file to simulate the same <=3.13 layout. Either interpreter
# proves the same self.file-only code path end to end.
# ---------------------------------------------------------------------------


def test_running_interpreter_version_is_recorded_for_the_report() -> None:
    # Not an assertion about behavior -- just makes the actually-executed
    # interpreter version visible in -q/-v output for the compatibility
    # report this fix requires.
    print(f"imap_deadline tests executed under Python {sys.version}")
    assert sys.version_info >= (3, 11)


class TestGetImapMakefileReader:
    def test_prefers_the_cpython_3_14_style_private_attribute(self) -> None:
        class _Py314Style:
            _file = object()
            file = None  # a 3.14 IMAP4 instance has no `.file` at all;
            # explicit None here only to prove _file wins if both existed.

        reader = get_imap_makefile_reader(_Py314Style())
        assert reader is _Py314Style._file

    def test_falls_back_to_the_cpython_3_13_style_public_attribute(self) -> None:
        class _Py313Style:
            file = object()
            # No `_file` attribute at all -- mirrors a real <=3.13
            # imaplib.IMAP4 instance exactly (see this fix's report for
            # the confirmed upstream source on 3.11/3.13).

        reader = get_imap_makefile_reader(_Py313Style())
        assert reader is _Py313Style.file

    def test_returns_none_when_neither_attribute_exists(self) -> None:
        class _NeitherStyle:
            pass

        assert get_imap_makefile_reader(_NeitherStyle()) is None

    def test_returns_none_for_a_bare_object_without_makefile_at_all(self) -> None:
        # A test-injected fake IMAP client (see
        # tests/test_providers_email_imap.py's FakeImapClient) never has
        # a real socket or makefile()-backed reader.
        assert get_imap_makefile_reader(object()) is None


class _LegacyAttributeDeadlineIMAP4SSL(DeadlineIMAP4SSL):
    """Exercises the makefile()-backed reader path end-to-end (real
    socket, real TLS, real imaplib greeting parsing, real watchdog
    firing) using whichever attribute layout the ACTUAL running
    interpreter's `imaplib.IMAP4.open()` produces -- portable across both
    known layouts, not a simulation of one specific version:

    - CPython <=3.13 (e.g. this project's CI, which runs 3.13.15):
      `imaplib.IMAP4.open()` assigns `self.file` directly as a plain
      instance attribute. There is no `self._file` at all -- this is the
      REAL <=3.13 layout, exercised natively, no renaming needed.
    - CPython 3.14+ (this project's local dev runtime): `imaplib.IMAP4
      .open()` assigns `self._file` and exposes `IMAP4.file` only as a
      READ-ONLY property (an undocumented back-compat shim that proxies
      to `self._file`, emitting a RuntimeWarning -- confirmed by reading
      `imaplib.IMAP4.file.fget`'s source on this runtime). `open()`
      below detects `self._file`, moves it onto `self.file`, and deletes
      `self._file` -- simulating, on 3.14, the exact <=3.13 layout this
      class is named for.

    Either way, `get_imap_makefile_reader`'s `self.file` fallback branch
    is what's actually exercised afterward, never the `self._file`
    branch -- proving the fallback works, not just that it CAN find a
    `.file` attribute in isolation. `file = None` here shadows the
    inherited 3.14 read-only property with a plain class attribute so
    `self.file = ...` below is a normal instance assignment instead of
    hitting a "no setter" error; on <=3.13 there is no such property, so
    this class attribute is simply overwritten by imaplib's own instance
    assignment and does nothing.
    """

    file = None

    def open(
        self, host: str = "", port: int = imaplib.IMAP4_SSL_PORT, timeout: float | None = None
    ) -> None:
        imaplib.IMAP4.open(self, host, port, timeout)
        if hasattr(self, "_file"):
            # CPython 3.14-style layout: rename to mimic <=3.13's real
            # layout exactly -- self.file becomes a plain instance
            # attribute, self._file no longer exists.
            self.file = self._file
            del self._file
        else:
            # CPython <=3.13-style layout: imaplib.IMAP4.open() already
            # assigned self.file directly -- this IS the real <=3.13
            # layout, nothing to rename.
            assert hasattr(self, "file") and self.file is not None, (
                "this runtime's imaplib.IMAP4.open() layout changed -- neither "
                "self._file (3.14-style) nor a populated self.file (<=3.13-style) "
                "was produced"
            )
        self._imap_deadline.bind_socket(self.sock, extra_closable=get_imap_makefile_reader(self))


def test_legacy_file_attribute_layout_bounds_constructor_greeting_slow_drip(tmp_path):
    """Python-3.13-style layout, constructor phase: the greeting/
    CAPABILITY slow-drip regression, but with the makefile()-backed
    reader only reachable via `self.file` (never `self._file`)."""
    cert = _generate_self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("openssl CLI not available to generate a self-signed test certificate")
    certfile, keyfile = cert
    stop_drip = threading.Event()

    def _drip_unterminated_greeting(tls_conn: ssl.SSLSocket) -> None:
        while not stop_drip.is_set():
            try:
                tls_conn.send(b"x")
            except OSError:
                break
            time.sleep(0.05)

    server, (host, port), server_thread, accepted = _start_tls_server(
        certfile, keyfile, _drip_unterminated_greeting
    )
    try:
        # FINAL-003 (Astra R5A): see the identical comment in
        # test_constructor_greeting_capability_slow_drip_is_bounded_by_the_total_deadline
        # -- DNS resolution now spawns a genuinely terminable child
        # process, which costs real wall-clock time a pre-FINAL-003 0.4s
        # deadline never had to budget for.
        deadline = ImapSessionDeadline(2.0)
        result: dict[str, object] = {}

        def _construct() -> None:
            try:
                with deadline:
                    _LegacyAttributeDeadlineIMAP4SSL(
                        host,
                        port,
                        ssl_context=_relaxed_test_client_ssl_context(),
                        timeout=5.0,
                        deadline=deadline,
                    )
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        start = time.monotonic()
        worker = threading.Thread(target=_construct, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        elapsed = time.monotonic() - start
        stop_drip.set()

        assert accepted.wait(timeout=2)
        assert not worker.is_alive(), (
            "legacy self.file layout: constructor was not unblocked by the deadline"
        )
        assert elapsed < 4.0, f"not bounded by the deadline (took {elapsed:.2f}s)"
        assert deadline.exceeded is True
        assert "error" in result
    finally:
        stop_drip.set()
        server.close()
        server_thread.join(timeout=3)


def test_legacy_file_attribute_layout_bounds_post_constructor_slow_drip(tmp_path):
    """Python-3.13-style layout, post-constructor phase: construction
    succeeds normally (real greeting + CAPABILITY exchange), then a
    LATER read on the same, still-deadline-bound connection (standing in
    for a SELECT/SEARCH/FETCH response) is slow-dripped. Proves the
    `self.file`-only binding done in `open()` keeps protecting reads
    after construction, not just during it."""
    cert = _generate_self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("openssl CLI not available to generate a self-signed test certificate")
    certfile, keyfile = cert
    stop_drip = threading.Event()
    greeting_sent = threading.Event()

    def _greet_then_drip(tls_conn: ssl.SSLSocket) -> None:
        tls_conn.sendall(b"* OK IMAP4rev1 Service Ready\r\n")
        greeting_sent.set()
        while not stop_drip.is_set():
            try:
                tls_conn.send(b"x")
            except OSError:
                break
            time.sleep(0.05)

    server, (host, port), server_thread, accepted = _start_tls_server(
        certfile, keyfile, _greet_then_drip
    )
    try:
        # FINAL-003 (Astra R5A): see the identical comment in
        # test_constructor_greeting_capability_slow_drip_is_bounded_by_the_total_deadline
        # -- DNS resolution now spawns a genuinely terminable child
        # process, which costs real wall-clock time a pre-FINAL-003 0.5s
        # deadline never had to budget for. This test needs even MORE
        # margin than the others: construction must fully SUCCEED (real
        # DNS resolution + TCP connect + TLS handshake + greeting/
        # CAPABILITY exchange) BEFORE the post-constructor drip phase
        # this test actually exercises even begins.
        deadline = ImapSessionDeadline(2.5)
        result: dict[str, object] = {}

        def _run() -> None:
            try:
                with deadline:
                    client = _LegacyAttributeDeadlineIMAP4SSL(
                        host,
                        port,
                        ssl_context=_relaxed_test_client_ssl_context(),
                        timeout=5.0,
                        deadline=deadline,
                    )
                    assert not hasattr(client, "_file")
                    assert isinstance(get_imap_makefile_reader(client), object)
                    # Post-constructor read, standing in for a SELECT/
                    # SEARCH/FETCH response -- the greeting above already
                    # completed construction successfully; this second
                    # read is the slow-dripped, never-terminated line.
                    client.readline()
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        start = time.monotonic()
        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=6.0)
        elapsed = time.monotonic() - start
        stop_drip.set()

        assert accepted.wait(timeout=2)
        assert greeting_sent.wait(timeout=2)
        assert not worker.is_alive(), (
            "legacy self.file layout: post-constructor read was not unblocked by the deadline"
        )
        assert elapsed < 4.5, f"not bounded by the deadline (took {elapsed:.2f}s)"
        assert deadline.exceeded is True
        assert "error" in result
    finally:
        stop_drip.set()
        server.close()
        server_thread.join(timeout=3)


def test_watchdog_force_close_does_not_hang_on_an_in_flight_readline(tmp_path):
    """The exact Codex deadlock concern, tested directly: if a thread is
    blocked inside `self.file.readline()` (holding io.BufferedReader's
    internal lock for the whole blocking duration) when the watchdog
    fires, `ImapSessionDeadline._force_close` itself -- run on the
    watchdog's OWN thread -- must return promptly. It must NOT be the
    one left waiting on that same lock forever.
    """
    cert = _generate_self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("openssl CLI not available to generate a self-signed test certificate")
    certfile, keyfile = cert

    def _stay_silent(tls_conn: ssl.SSLSocket) -> None:
        time.sleep(5)

    server, (host, port), server_thread, accepted = _start_tls_server(
        certfile, keyfile, _stay_silent
    )
    try:
        raw = socket.create_connection((host, port), timeout=5.0)
        client_ctx = _relaxed_test_client_ssl_context()
        tls_client = client_ctx.wrap_socket(
            raw, server_hostname="localhost", do_handshake_on_connect=False
        )
        tls_client.do_handshake()
        assert accepted.wait(timeout=2)
        fileobj = tls_client.makefile("rb")  # mirrors imaplib <=3.13's self.file

        reader_result: dict[str, object] = {}

        def _blocked_readline() -> None:
            try:
                reader_result["data"] = fileobj.readline(1000)
            except OSError as exc:
                reader_result["error"] = exc

        reader = threading.Thread(target=_blocked_readline, daemon=True)
        reader.start()
        time.sleep(0.3)  # ensure readline() is genuinely blocked first

        deadline = ImapSessionDeadline(60.0)  # never fires on its own
        deadline.bind_socket(tls_client, extra_closable=fileobj)

        watchdog_result: dict[str, float] = {}

        def _simulate_watchdog_fire() -> None:
            start = time.monotonic()
            deadline._on_fire()
            watchdog_result["elapsed"] = time.monotonic() - start

        watchdog_thread = threading.Thread(target=_simulate_watchdog_fire, daemon=True)
        watchdog_thread.start()
        watchdog_thread.join(timeout=2.0)

        assert not watchdog_thread.is_alive(), (
            "the watchdog itself hung inside _force_close (blocked on "
            "extra_closable.close() behind the in-flight readline())"
        )
        assert watchdog_result["elapsed"] < 1.0, (
            f"_force_close took {watchdog_result['elapsed']:.2f}s -- "
            "too slow to be a safe watchdog callback"
        )

        reader.join(timeout=2.0)
        assert not reader.is_alive(), "the blocked readline() was never released"
        assert "error" in reader_result or reader_result.get("data") == b""
    finally:
        server.close()
        server_thread.join(timeout=3)


# ---------------------------------------------------------------------------
# FINAL-003 (Astra R5A, corrected after Codex re-review): the total
# session deadline must also bound DNS resolution -- the phase BEFORE any
# socket exists for bind_socket/_force_close to act on -- AND the worker
# actually PERFORMING that resolution must itself be genuinely bounded,
# not merely abandoned behind a timeout. The first version of this fix
# used a background daemon THREAD and was rejected: Python cannot
# forcibly interrupt a thread blocked inside a C-level blocking syscall,
# so "abandoning" it just left the real getaddrinfo() call running for
# however long the OS took -- an unbounded WORKER even though the
# CALLING thread was bounded. ImapSessionDeadline.resolve_addrinfo_bounded
# closes this via a genuinely terminable, isolated child PROCESS instead
# -- see its own docstring for the full rationale.
# ---------------------------------------------------------------------------


def _hang_forever_dns_worker(_host, _port, _conn) -> None:
    """Test-only worker target -- MUST be a real importable module-level
    function (never a lambda/closure): `multiprocessing`'s "spawn"
    context re-imports the target module fresh in the child process, so
    only a by-reference-picklable function works. Simulates a
    `getaddrinfo()` call that never returns (e.g. an unresponsive DNS
    server) by blocking on a real, long sleep -- deliberately never
    touching `_conn` at all, so the parent's `poll()` genuinely times
    out rather than being fed a result.
    """
    time.sleep(10_000)


def _raise_specific_error_dns_worker(_host, _port, conn) -> None:
    """Test-only worker target: deterministically reports a specific,
    recognizable `OSError` back to the parent -- used instead of
    resolving a real (possibly flaky, possibly differently-handled by
    different CI network environments) nonexistent hostname, so the
    "a genuine DNS failure propagates" regression is fully
    network-independent.
    """
    try:
        conn.send(("error", OSError("simulated getaddrinfo failure, deterministic for tests")))
    finally:
        conn.close()


class TestResolveAddrinfoBounded:
    def test_returns_the_result_when_dns_completes_in_time(self):
        """Normal resolution succeeds -- the common case must be
        unaffected by the bounding mechanism."""
        deadline = ImapSessionDeadline(5.0)
        with deadline:
            result = deadline.resolve_addrinfo_bounded("127.0.0.1", 993)
        assert result, "expected at least one resolved address"
        _family, _socktype, _proto, _canonname, sockaddr = result[0]
        assert sockaddr[0] == "127.0.0.1"
        assert sockaddr[1] == 993
        assert deadline.exceeded is False

    def test_reraises_whatever_getaddrinfo_itself_raises_when_it_completes_in_time(self):
        """A genuine DNS failure (not a timeout) must propagate to the
        caller exactly like calling `socket.getaddrinfo()` directly
        would -- never swallowed or reshaped into a generic timeout."""
        deadline = ImapSessionDeadline(5.0)
        with deadline, pytest.raises(OSError, match="simulated getaddrinfo failure"):
            deadline.resolve_addrinfo_bounded(
                "irrelevant.example", 993, _worker=_raise_specific_error_dns_worker
            )
        assert deadline.exceeded is False

    def test_caller_returns_by_the_deadline_when_dns_hangs(self):
        """FINAL-003's core requirement: the CALLING thread must never
        block past the deadline's own remaining time, even when the
        underlying DNS resolution itself never returns. The tightened
        `_WORKER_TERMINATION_JOIN_TIMEOUT_SECONDS` ceiling (0.5s, from
        this fix's own correction) keeps the total bounded close to the
        configured deadline -- not "eventually", but within a small,
        fixed, documented additive constant.
        """
        deadline = ImapSessionDeadline(0.3)
        start = time.monotonic()
        with deadline, pytest.raises(TimeoutError):
            deadline.resolve_addrinfo_bounded(
                "hangs.invalid", 993, _worker=_hang_forever_dns_worker
            )
        elapsed = time.monotonic() - start
        assert elapsed < 1.5, (
            f"did not return within a tight bound of the 0.3s deadline (took {elapsed:.2f}s) "
            "-- cleanup itself must never add seconds of latency"
        )
        assert deadline.exceeded is True

    def test_dns_worker_process_is_confirmed_dead_before_the_call_returns(self):
        """The actual Codex correction, proven directly (not just
        inferred from timing): the resolver WORKER PROCESS must itself
        be terminated and its death CONFIRMED before
        `resolve_addrinfo_bounded` returns/raises -- not merely asked to
        stop and left running. `multiprocessing.active_children()`
        reflects genuinely-still-alive child processes this test
        process has started (and reaps/prunes already-finished ones as
        a side effect of being called) -- if the worker were only
        abandoned (the pre-correction thread-based design's actual
        flaw), a process-based equivalent of that mistake would still
        show up here.
        """
        before = set(multiprocessing.active_children())

        deadline = ImapSessionDeadline(0.3)
        with deadline, pytest.raises(TimeoutError):
            deadline.resolve_addrinfo_bounded(
                "hangs.invalid", 993, _worker=_hang_forever_dns_worker
            )

        after = set(multiprocessing.active_children())
        lingering = after - before
        assert not lingering, (
            f"DNS resolution worker process(es) still alive after the deadline "
            f"fired and resolve_addrinfo_bounded returned: {lingering}"
        )

    def test_no_abandoned_resolver_process_remains_across_repeated_timeouts(self):
        """Same guarantee as above, proven across several consecutive
        timeouts -- guards against a subtler bug where cleanup happens
        to work on the first call (e.g. by luck of scheduling) but
        leaks on a later one."""
        before = set(multiprocessing.active_children())

        for _ in range(3):
            deadline = ImapSessionDeadline(0.2)
            with deadline, pytest.raises(TimeoutError):
                deadline.resolve_addrinfo_bounded(
                    "hangs.invalid", 993, _worker=_hang_forever_dns_worker
                )

        after = set(multiprocessing.active_children())
        assert not (after - before), "a DNS resolution worker leaked across repeated timeouts"


def test_deadline_imap4ssl_construction_is_bounded_when_dns_resolution_hangs(monkeypatch):
    """FINAL-003 (Astra R5A, corrected) integration-level proof:
    constructing `DeadlineIMAP4SSL` itself -- not just
    `ImapSessionDeadline.resolve_addrinfo_bounded` in isolation -- is
    bounded even when DNS resolution hangs, going through the REAL
    constructor path with no test-only parameter needed at that layer.
    Monkeypatching the MODULE-level `_resolve_addrinfo_worker` is picked
    up transparently (see `resolve_addrinfo_bounded`'s own docstring for
    why: the default is looked up from the module's current namespace at
    CALL time, not captured at function-definition time).
    """
    import app.providers.email.imap_deadline as imap_deadline_module

    monkeypatch.setattr(imap_deadline_module, "_resolve_addrinfo_worker", _hang_forever_dns_worker)

    before = set(multiprocessing.active_children())
    deadline = ImapSessionDeadline(0.3)
    start = time.monotonic()
    with deadline, pytest.raises(TimeoutError):
        DeadlineIMAP4SSL(
            "hangs.invalid",
            993,
            ssl_context=_relaxed_test_client_ssl_context(),
            timeout=5.0,
            deadline=deadline,
        )
    elapsed = time.monotonic() - start

    assert elapsed < 1.5, f"construction blocked past a tight bound (took {elapsed:.2f}s)"
    assert deadline.exceeded is True
    after = set(multiprocessing.active_children())
    assert not (after - before), "DNS resolution worker still alive after construction aborted"


def test_tcp_socket_is_registered_before_connect_and_normal_construction_still_succeeds(
    tmp_path, monkeypatch
):
    """Combines two FINAL-003 checklist items in one real end-to-end
    run: (1) the raw TCP socket must be registered with the deadline
    watchdog BEFORE `connect()` is even attempted on it -- otherwise a
    hang during connect() itself would never be interruptible; (2) the
    normal, real IMAP-over-TLS construction path (DNS resolution -> TCP
    connect -> TLS handshake -> greeting/CAPABILITY) must still succeed
    completely unaffected by any of this fix's changes.

    `socket.socket.connect` is spied on (not replaced -- the real
    connect still runs) purely to observe, at the exact moment connect()
    is invoked, whether the deadline already has THIS socket instance
    registered -- the precise ordering guarantee this test exists to
    prove.
    """
    cert = _generate_self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("openssl CLI not available to generate a self-signed test certificate")
    certfile, keyfile = cert

    def _reply_normally(tls_conn: ssl.SSLSocket) -> None:
        tls_conn.sendall(b"* OK IMAP4rev1 Service Ready\r\n")
        request = b""
        while not request.endswith(b"\r\n"):
            chunk = tls_conn.recv(4096)
            if not chunk:
                return
            request += chunk
        tag = request.split(b" ", 1)[0]
        tls_conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK CAPABILITY completed\r\n")
        time.sleep(0.2)

    server, (host, port), server_thread, accepted = _start_tls_server(
        certfile, keyfile, _reply_normally
    )

    holder: dict[str, ImapSessionDeadline] = {}
    observed: dict[str, bool] = {}
    real_connect = socket.socket.connect

    def _spying_connect(self, address):
        observed["registered_before_connect"] = holder["deadline"]._socket is self
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", _spying_connect)

    try:
        deadline = ImapSessionDeadline(5.0)
        holder["deadline"] = deadline
        with deadline:
            client = DeadlineIMAP4SSL(
                host,
                port,
                ssl_context=_relaxed_test_client_ssl_context(),
                timeout=5.0,
                deadline=deadline,
            )
        assert accepted.wait(timeout=2)
        assert deadline.exceeded is False
        client.shutdown()
    finally:
        server.close()
        server_thread.join(timeout=3)

    assert "registered_before_connect" in observed, "connect() spy was never invoked"
    assert observed["registered_before_connect"] is True, (
        "the raw TCP socket was not registered with the deadline watchdog BEFORE "
        "connect() was attempted -- a hang during connect() itself would be unbounded"
    )
