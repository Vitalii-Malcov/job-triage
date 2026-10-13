"""Stage 13A: real-PostgreSQL proof of GET /api/v1/ready's readiness check
(app.services.readiness) -- the parts SQLite cannot show:

- at head -> ready; an application/database revision mismatch and a missing
  `alembic_version` table -> not ready.
- `statement_timeout`: a migration-style ACCESS EXCLUSIVE lock on
  `alembic_version` makes the query wait; the check still ends (not ready)
  within the 5 s Docker healthcheck timeout instead of hanging.
- `connect_timeout`: an address that never answers ends within the same
  bound.
- the check writes nothing (pg_stat_user_tables deltas).

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first.
**CI:** part of the `scheduler-postgres` job's zero-skip gate.
"""

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.main import app
from app.services import readiness
from app.services.readiness import build_readiness_engine, check_readiness, readiness_url

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

HEALTHCHECK_TIMEOUT_SECONDS = 5.0


def _write_counters(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT COALESCE(SUM(n_tup_ins + n_tup_upd + n_tup_del), 0) "
                "FROM pg_stat_user_tables"
            )
        ).scalar_one()


class TestReadinessOnPostgres:
    def test_database_at_head_is_ready_via_the_endpoint(self, monkeypatch):
        engine = build_readiness_engine(TEST_POSTGRES_URL)
        monkeypatch.setattr(readiness, "_default_readiness_engine", lambda: engine)
        response = TestClient(app).get("/api/v1/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    def test_revision_mismatch_is_not_ready(self, monkeypatch):
        monkeypatch.setattr(readiness, "expected_alembic_heads", lambda: ("000000000000",))
        assert check_readiness(build_readiness_engine(TEST_POSTGRES_URL)) is False

    def test_missing_alembic_version_table_is_not_ready(self):
        url, args = readiness_url(TEST_POSTGRES_URL)
        args = {**args, "options": args["options"] + " -c search_path=stage13a_no_such_schema"}
        engine = create_engine(url, connect_args=args, poolclass=NullPool)
        assert check_readiness(engine) is False

    def test_readiness_writes_nothing(self):
        stats_engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
        before = _write_counters(stats_engine)
        engine = build_readiness_engine(TEST_POSTGRES_URL)
        for _ in range(3):
            assert check_readiness(engine) is True
        time.sleep(1.0)  # stats collector flush
        assert _write_counters(stats_engine) == before
        stats_engine.dispose()

    def test_lock_wait_is_bounded_by_statement_timeout(self):
        holder_engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
        locked = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with holder_engine.connect() as conn:
                conn.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
                locked.set()
                release.wait(timeout=30)
                conn.rollback()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            assert locked.wait(timeout=10)
            started = time.monotonic()
            assert check_readiness(build_readiness_engine(TEST_POSTGRES_URL)) is False
            assert time.monotonic() - started < HEALTHCHECK_TIMEOUT_SECONDS
        finally:
            release.set()
            holder.join(timeout=30)
            holder_engine.dispose()
        assert check_readiness(build_readiness_engine(TEST_POSTGRES_URL)) is True

    def test_unanswered_connection_is_bounded_by_connect_timeout(self):
        # 192.0.2.1 is TEST-NET-1 (RFC 5737): reserved, never a real host.
        unreachable = make_url(TEST_POSTGRES_URL).set(host="192.0.2.1", port=5432)
        started = time.monotonic()
        assert (
            check_readiness(
                build_readiness_engine(unreachable.render_as_string(hide_password=False))
            )
            is False
        )
        assert time.monotonic() - started < HEALTHCHECK_TIMEOUT_SECONDS
