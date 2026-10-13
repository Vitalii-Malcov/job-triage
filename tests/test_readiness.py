"""Stage 13A: GET /api/v1/health (liveness) vs GET /api/v1/ready (readiness).

SQLite level; tests/integration/test_readiness_postgres.py proves the
PostgreSQL-only bounds (statement_timeout, connect_timeout) for real.
"""

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.main import app
from app.services import readiness
from app.services.readiness import (
    build_readiness_engine,
    check_readiness,
    expected_alembic_heads,
    readiness_url,
)

HEAD = expected_alembic_heads()[0]
SECRET_SENTINEL = "stage13a-secret-sentinel"


def _sqlite_db(path: Path, revisions: list[str] | None) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
    if revisions is not None:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.executemany("INSERT INTO alembic_version VALUES (?)", [(r,) for r in revisions])
    conn.commit()
    conn.close()
    return path


def _engine(path: Path) -> Engine:
    return build_readiness_engine(f"sqlite:///{path}")


@pytest.fixture()
def client():
    # No `with`: the app lifespan (logging reconfiguration, Telegram bot
    # start-up from the ambient Settings) is not part of what /ready does.
    return TestClient(app)


def _use_engine(monkeypatch, engine) -> None:
    monkeypatch.setattr(readiness, "_default_readiness_engine", lambda: engine)


class TestLiveness:
    def test_health_never_touches_the_database(self, client, monkeypatch):
        def _forbidden(*_args, **_kwargs):
            raise AssertionError("/health must not use the database")

        monkeypatch.setattr(Engine, "connect", _forbidden)
        monkeypatch.setattr(readiness, "check_readiness", _forbidden)
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestReadyEndpoint:
    def test_migrated_database_at_head_is_ready(self, client, monkeypatch, tmp_path):
        from alembic.command import upgrade
        from alembic.config import Config

        db_path = tmp_path / "ready.db"
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        upgrade(cfg, "head")
        _use_engine(monkeypatch, _engine(db_path))

        response = client.get("/api/v1/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    @pytest.mark.parametrize(
        "revisions",
        [
            pytest.param(["e8b1c4f7a2d9"], id="behind"),
            pytest.param(["ffffffffffff"], id="ahead-or-unknown"),
            pytest.param([], id="no-revision-row"),
            pytest.param([HEAD, HEAD], id="duplicate-revision-rows"),
            pytest.param([HEAD, "e8b1c4f7a2d9"], id="multiple-revision-rows"),
            pytest.param(None, id="missing-alembic-version-table"),
        ],
    )
    def test_schema_not_exactly_at_head_is_not_ready(
        self, client, monkeypatch, tmp_path, revisions
    ):
        _use_engine(monkeypatch, _engine(_sqlite_db(tmp_path / "db.db", revisions)))
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}

    def test_unreachable_database_is_not_ready_and_never_created(
        self, client, monkeypatch, tmp_path
    ):
        missing = tmp_path / "does-not-exist.db"
        _use_engine(monkeypatch, _engine(missing))
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
        assert not missing.exists()  # read-only open: never creates the file

    def test_failure_never_exposes_database_error_text(self, client, monkeypatch, caplog):
        class _ExplodingEngine:
            def connect(self):
                raise RuntimeError(f"connection to postgresql://u:{SECRET_SENTINEL}@db failed")

        _use_engine(monkeypatch, _ExplodingEngine())
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.text == '{"status":"not_ready"}'
        assert SECRET_SENTINEL not in caplog.text
        assert "error_type=RuntimeError" in caplog.text

    def test_multiple_application_heads_fail_closed_without_connecting(self, client, monkeypatch):
        class _UnusedEngine:
            def connect(self):
                raise AssertionError("must not connect with an ambiguous head")

        _use_engine(monkeypatch, _UnusedEngine())
        monkeypatch.setattr(readiness, "expected_alembic_heads", lambda: ("aaa", "bbb"))
        assert client.get("/api/v1/ready").status_code == 503
        monkeypatch.setattr(readiness, "expected_alembic_heads", lambda: ())
        assert client.get("/api/v1/ready").status_code == 503


class TestReadOnlyAndBounded:
    def test_readiness_never_mutates_the_database(self, tmp_path):
        db_path = _sqlite_db(tmp_path / "db.db", [HEAD])
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        engine = _engine(db_path)
        for _ in range(3):
            assert check_readiness(engine) is True
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
        assert not Path(f"{db_path}-journal").exists()

    def test_readiness_engine_refuses_writes(self, tmp_path):
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError

        engine = _engine(_sqlite_db(tmp_path / "db.db", [HEAD]))
        with pytest.raises(OperationalError):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO marker (id) VALUES (1)"))

    def test_a_locked_database_fails_fast_instead_of_hanging(self, tmp_path):
        db_path = _sqlite_db(tmp_path / "db.db", [HEAD])
        holder = sqlite3.connect(db_path)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            assert check_readiness(_engine(db_path)) is False
            assert time.monotonic() - started < 4.0  # under the 5 s healthcheck timeout
        finally:
            holder.rollback()
            holder.close()

    def test_in_memory_database_is_never_reported_ready(self):
        assert check_readiness(build_readiness_engine("sqlite://")) is False


class TestReadinessUrl:
    def test_postgres_bounds_every_stage(self):
        _url, args = readiness_url("postgresql+psycopg://u:p@db:5432/app")
        assert args["connect_timeout"] == 2
        assert "statement_timeout=1500" in args["options"]
        assert args["tcp_user_timeout"] == 1500
        assert args["keepalives"] == 1

    def test_sqlite_opens_read_only(self, tmp_path):
        url, args = readiness_url(f"sqlite:///{tmp_path / 'x.db'}")
        assert url.query["mode"] == "ro"
        assert url.query["uri"] == "true"
        assert args["timeout"] <= 1.0

    def test_readiness_uses_no_shared_pool(self, tmp_path):
        from sqlalchemy.pool import NullPool

        assert isinstance(_engine(tmp_path / "x.db").pool, NullPool)
