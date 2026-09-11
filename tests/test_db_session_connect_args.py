"""HARD-011 (adversarial hardening r1) regression tests for
app.db.session's per-dialect connect_args. Live-verified during this
hardening pass: without an explicit connect_timeout, a connection
attempt to a stopped-but-network-reachable PostgreSQL host hung for
60+ seconds with no application-level bound at all -- confirmed via a
real Docker Postgres container stop/start cycle (see
docs/ADVERSARIAL_HARDENING_REPORT.md HARD-011 for the exact evidence).
These tests cover the pure `_connect_args_for` helper directly rather
than reimporting app.db.session with a different DATABASE_URL (which
would require process-level isolation, since the module computes its
real `engine`/`connect_args` once at import time from the actual
environment).
"""

from app.db.session import _connect_args_for


def test_sqlite_url_gets_check_same_thread_only():
    args = _connect_args_for("sqlite:///./job_search.db")
    assert args == {"check_same_thread": False}


def test_postgres_url_gets_a_bounded_connect_timeout():
    args = _connect_args_for("postgresql+psycopg://user:pass@host:5432/db")
    assert args == {"connect_timeout": 10}
    # Must be a real, finite bound -- not accidentally 0 (which some
    # drivers treat as "no timeout") or unset.
    assert args["connect_timeout"] > 0


def test_plain_postgres_scheme_without_driver_suffix_also_gets_timeout():
    args = _connect_args_for("postgresql://user:pass@host:5432/db")
    assert "connect_timeout" in args
    assert "check_same_thread" not in args
