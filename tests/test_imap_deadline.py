"""Tests for app.providers.email.imap_deadline.ImapSessionDeadline (AUD-005).

Uses real, unencrypted TCP sockets (no imaplib/TLS involved) -- the
deadline mechanism itself is socket-agnostic and this keeps these tests
fast and deterministic while still exercising real blocking I/O and real
cross-thread socket closure, not a mock.
"""

import socket
import threading
import time

import pytest

from app.providers.email.imap_deadline import ImapSessionDeadline


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
