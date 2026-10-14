"""Stage 13B: real-PostgreSQL proof of the CandidateProfile-only transfer
(app/services/candidate_profile_transfer.py):

- a FRESH PostgreSQL database migrated through the real Alembic chain to
  head (never create_all);
- SQLite source -> snapshot -> PostgreSQL import; re-export reproduces the
  semantic digest; typed field-by-field equality; exact top-level trust,
  including a non-empty fact whose trust entry is ABSENT; nested provenance;
  scoring-skill projection and generation-trust decisions identical;
- a failure inside the import transaction leaves every profile table empty;
- a destination that already holds a profile is refused without mutation;
- every non-CandidateProfile table stays empty (no historical state);
- /api/v1/health, /api/v1/ready and an authenticated profile read against the
  destination, repeated after a full engine/client restart.

Each test gets its OWN database on the TEST_POSTGRES_URL server -- a
`CREATE DATABASE ... TEMPLATE` copy of one module-level database built only
by `alembic upgrade head`, dropped `WITH (FORCE)` afterwards -- so it neither
sees nor touches the profile other modules create in the shared CI database.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set (the role
must be allowed to CREATE DATABASE -- the postgres:16 image's POSTGRES_USER
is). **CI:** part of the `scheduler-postgres` job's zero-skip gate.
"""

import json
import os
import shutil
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db import candidate_profile_repository as repo
from app.db.session import get_db
from app.main import app
from app.models.candidate_profile import (
    TOP_LEVEL_TRUST_FIELDS,
    CandidateProfile,
    is_top_level_fact_usable_for_generation,
)
from app.security import rate_limit as rate_limit_module
from app.services import candidate_profile_transfer as transfer
from app.services import readiness
from app.services.readiness import build_readiness_engine
from scripts import candidate_profile_transfer_stage13b as cli
from tests.test_candidate_profile_transfer import SENTINEL, seed_rich_profile

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_KEY = "stage13b-test-api-key"


def _upgrade_head(url: str) -> None:
    from alembic.command import upgrade
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    upgrade(cfg, "head")


@pytest.fixture(scope="module")
def sqlite_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("s13b_pg_template") / "template.db"
    _upgrade_head(f"sqlite:///{path}")
    return path


@pytest.fixture()
def source_url(sqlite_template, tmp_path) -> str:
    path = tmp_path / "source.db"
    shutil.copyfile(sqlite_template, path)
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    with Session(engine) as db:
        seed_rich_profile(db)
    engine.dispose()
    return f"sqlite:///{path}"


def _admin_engine():
    return create_engine(TEST_POSTGRES_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _db_url(name: str) -> str:
    return make_url(TEST_POSTGRES_URL).set(database=name).render_as_string(hide_password=False)


def _drop(admin, name: str) -> None:
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@contextmanager
def _disposable_database(prefix: str, template: str | None = None) -> Iterator[str]:
    """CREATE a uniquely named database and DROP it afterwards. The CREATE is
    INSIDE the try: once the name is allocated, a failure at any later point
    -- including CREATE raising client-side after the server already created
    the database -- still reaches the `DROP ... IF EXISTS ... WITH (FORCE)`.
    (A `created = True` flag set after CREATE would miss exactly that case.)"""
    admin = _admin_engine()
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    template_clause = f' TEMPLATE "{template}"' if template else ""
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"{template_clause}'))
        yield name
    finally:
        try:
            _drop(admin, name)
        finally:
            admin.dispose()


def _database_exists(name: str) -> bool:
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            return (
                conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                ).first()
                is not None
            )
    finally:
        admin.dispose()


@pytest.fixture(scope="module")
def migrated_template():
    """ONE brand-new database per module, built only by `alembic upgrade head`
    (the full chain, never create_all). Each test then gets an identical,
    independent copy via CREATE DATABASE ... TEMPLATE -- the full chain per
    test is needlessly slow on fsync-heavy hosts."""
    with _disposable_database("stage13b_tmpl") as name:
        _upgrade_head(_db_url(name))
        yield name


@pytest.fixture()
def fresh_pg_url(migrated_template):
    """A brand-new database on the test server, at head, holding no rows."""
    with _disposable_database("stage13b", template=migrated_template) as name:
        yield _db_url(name)


class TestDisposableDatabaseCleanup:
    def test_database_is_dropped_when_setup_fails_after_create(self):
        with pytest.raises(RuntimeError, match="setup failed"):
            with _disposable_database("stage13b_cleanup") as name:
                assert _database_exists(name)
                raise RuntimeError("setup failed")
        assert not _database_exists(name)

    def test_database_is_dropped_when_create_raises_after_server_side_creation(self, monkeypatch):
        created: list[str] = []
        real_admin_engine = _admin_engine

        def failing_admin_engine():
            engine = real_admin_engine()

            @event.listens_for(engine, "after_cursor_execute")
            def fail_after_create(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith("CREATE DATABASE"):
                    created.append(statement.split('"')[1])
                    raise RuntimeError("client-side failure after CREATE")

            return engine

        monkeypatch.setattr(sys.modules[__name__], "_admin_engine", failing_admin_engine)
        with pytest.raises(RuntimeError, match="after CREATE"):
            with _disposable_database("stage13b_cleanup"):
                pytest.fail("body must not run when CREATE raised")
        monkeypatch.undo()
        assert len(created) == 1
        assert not _database_exists(created[0])


def _pg_session(url: str) -> Session:
    return Session(create_engine(url, poolclass=NullPool), autoflush=False)


def _snapshot(source_url: str) -> transfer.ParsedSnapshot:
    with cli.session_for(cli.read_only_engine(source_url)) as db:
        profile = transfer.export_profile(db)
    data = transfer.serialize_snapshot(
        transfer.build_snapshot(
            profile, source_dialect="sqlite", source_revision=transfer.application_head()
        )
    )
    return transfer.parse_snapshot(data)


def _import(url: str, snapshot: transfer.ParsedSnapshot) -> str:
    with cli.session_for(cli.writable_postgres_engine(url)) as db:
        return transfer.import_profile(db, snapshot)


def _domain_counts(url: str) -> dict[str, int]:
    with cli.session_for(cli.read_only_engine(url)) as db:
        return transfer.profile_domain_counts(db)


class TestFreshPostgresTransfer:
    def test_fresh_database_is_at_the_single_head_and_empty(self, fresh_pg_url):
        with cli.session_for(cli.read_only_engine(fresh_pg_url)) as db:
            assert transfer.require_head_revision(db) == transfer.application_head()
            assert set(transfer.profile_domain_counts(db).values()) == {0}
            assert set(transfer.historical_state_counts(db).values()) == {0}

    def test_import_reproduces_digest_trust_and_projections(self, source_url, fresh_pg_url):
        snapshot = _snapshot(source_url)
        assert _import(fresh_pg_url, snapshot) == snapshot.digest

        summary = cli.compare_databases(source_url, fresh_pg_url)
        assert summary["status"] == "PASS", summary["checks"]
        assert summary["source_semantic_digest"] == summary["target_semantic_digest"]
        assert summary["typed_mismatch_paths"] == []
        assert summary["historical_tables_populated"] == []
        assert summary["target_revision"] == transfer.application_head()

        with _pg_session(fresh_pg_url) as db:
            raw = db.execute(
                text("SELECT field_trust_json, profile_version FROM candidate_profiles")
            ).one()
            trust = json.loads(raw.field_trust_json)
            assert raw.profile_version == transfer.FRESH_PROFILE_VERSION
            assert "professional_title" not in trust
            assert trust["location_city"] == {"source": "IMPORTED", "confidence": "UNCONFIRMED"}
            skills = dict(
                db.execute(text("SELECT name, source || '/' || confidence FROM candidate_skills"))
                .tuples()
                .all()
            )
            assert skills[f"{SENTINEL}-Kubernetes"] == "INFERRED/UNCONFIRMED"
            assert repo.get_candidate_skills_for_scoring(db) == frozenset({f"{SENTINEL}-Python"})
            profile = transfer.read_profile_response(db)
            assert profile.professional_title == f"{SENTINEL}-Title-without-trust"
            assert not is_top_level_fact_usable_for_generation(profile, "professional_title")
            usable = {
                f
                for f in TOP_LEVEL_TRUST_FIELDS
                if is_top_level_fact_usable_for_generation(profile, f)
            }
            assert usable == {"first_name", "last_name", "career_goal"}

    def test_failure_inside_the_transaction_rolls_back_everything(
        self, source_url, fresh_pg_url, monkeypatch
    ):
        snapshot = _snapshot(source_url)
        original = transfer.build_records

        def broken(profile):
            record = original(profile)
            record.languages[-1].normalized_language = None  # NOT NULL violation at flush
            return record

        monkeypatch.setattr(transfer, "build_records", broken)
        with pytest.raises(Exception):  # noqa: B017 -- psycopg IntegrityError via SQLAlchemy
            _import(fresh_pg_url, snapshot)
        assert set(_domain_counts(fresh_pg_url).values()) == {0}

    def test_verification_mismatch_rolls_back_everything(
        self, source_url, fresh_pg_url, monkeypatch
    ):
        snapshot = _snapshot(source_url)
        real = transfer.payload_from_record
        monkeypatch.setattr(
            transfer,
            "payload_from_record",
            lambda record: real(record).model_copy(update={"career_goal": "drift"}),
        )
        with pytest.raises(transfer.ImportVerificationError):
            _import(fresh_pg_url, snapshot)
        assert set(_domain_counts(fresh_pg_url).values()) == {0}

    def test_non_empty_destination_is_refused_without_mutation(self, source_url, fresh_pg_url):
        snapshot = _snapshot(source_url)
        _import(fresh_pg_url, snapshot)
        with _pg_session(fresh_pg_url) as db:
            before = db.execute(text("SELECT updated_at FROM candidate_profiles")).scalar_one()
        with pytest.raises(transfer.DestinationNotEmptyError):
            _import(fresh_pg_url, snapshot)
        with _pg_session(fresh_pg_url) as db:
            assert (
                db.execute(text("SELECT updated_at FROM candidate_profiles")).scalar_one() == before
            )
        assert _domain_counts(fresh_pg_url)["candidate_profiles"] == 1

    def test_destination_with_an_empty_profile_is_refused(self, source_url, fresh_pg_url):
        with _pg_session(fresh_pg_url) as db:
            repo.get_or_create_candidate_profile(db)
        with pytest.raises(transfer.DestinationNotEmptyError):
            _import(fresh_pg_url, _snapshot(source_url))

    def test_cli_import_and_compare_end_to_end(
        self, source_url, fresh_pg_url, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv(cli.SOURCE_URL_ENV, source_url)
        monkeypatch.setenv(cli.TARGET_URL_ENV, fresh_pg_url)
        out = tmp_path / "snapshot.json"
        assert cli.main(["export", "--out", str(out)]) == 0
        assert cli.main(["import", "--snapshot", str(out)]) == 0
        assert cli.main(["import", "--snapshot", str(out)]) == 2  # second import refused
        assert cli.main(["compare"]) == 0
        captured = capsys.readouterr()
        assert SENTINEL not in captured.out + captured.err
        password = make_url(fresh_pg_url).password
        if password:
            assert password not in captured.out + captured.err


class TestApplicationSmokeAgainstDestination:
    def _client(self, url: str, monkeypatch) -> tuple[TestClient, list]:
        engine = create_engine(url, poolclass=NullPool)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        def override_get_db():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        settings = Settings(
            _env_file=None, api_key=API_KEY, rate_limit_requests=1000, rate_limit_window_seconds=60
        )
        monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
        monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)
        monkeypatch.setattr("app.api.routes.get_settings", lambda: settings)
        readiness_engine = build_readiness_engine(url)
        monkeypatch.setattr(readiness, "_default_readiness_engine", lambda: readiness_engine)
        app.dependency_overrides[get_db] = override_get_db
        rate_limit_module._requests.clear()
        # No `with`: the lifespan (Telegram bot start-up) is not part of this smoke.
        return TestClient(app), [engine, readiness_engine]

    def _smoke(self, client: TestClient) -> CandidateProfile:
        assert client.get("/api/v1/health").status_code == 200
        ready = client.get("/api/v1/ready")
        assert ready.status_code == 200 and ready.json() == {"status": "ready"}
        response = client.get("/api/v1/candidate-profile", headers={"X-API-Key": API_KEY})
        assert response.status_code == 200
        return CandidateProfile.model_validate(response.json())

    def test_health_ready_and_profile_read_survive_restart(
        self, source_url, fresh_pg_url, monkeypatch
    ):
        snapshot = _snapshot(source_url)
        _import(fresh_pg_url, snapshot)
        try:
            client, engines = self._client(fresh_pg_url, monkeypatch)
            first = self._smoke(client)
            for engine in engines:
                engine.dispose()
            client, engines = self._client(fresh_pg_url, monkeypatch)  # "restart"
            second = self._smoke(client)
            for engine in engines:
                engine.dispose()
        finally:
            app.dependency_overrides.clear()
            rate_limit_module._requests.clear()

        assert first == second
        assert first.profile_version == transfer.FRESH_PROFILE_VERSION
        assert "professional_title" not in first.field_trust
        assert len(first.skills) == 3
        with cli.session_for(cli.read_only_engine(fresh_pg_url)) as db:
            assert transfer.semantic_digest(transfer.export_profile(db)) == snapshot.digest
            assert set(transfer.historical_state_counts(db).values()) == {0}
