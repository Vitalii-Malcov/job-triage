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

import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.providers.email.imap_deadline import DeadlineIMAP4SSL, ImapSessionDeadline


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
        deadline = ImapSessionDeadline(0.4)
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
        worker.join(timeout=3.0)
        elapsed = time.monotonic() - start
        stop_drip.set()

        assert accepted.wait(timeout=2), "test TLS server never completed the handshake"
        assert not worker.is_alive(), (
            "the constructor (blocked reading the greeting/CAPABILITY response) "
            "was not unblocked by the total session deadline"
        )
        assert elapsed < 2.0, (
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
        deadline = ImapSessionDeadline(0.4)
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
        worker.join(timeout=3.0)
        elapsed = time.monotonic() - start

        assert accepted.wait(timeout=2), "test TLS server never completed the handshake"
        assert not worker.is_alive(), (
            "the constructor (blocked reading the greeting) was not unblocked "
            "by the total session deadline"
        )
        assert elapsed < 2.0, (
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
