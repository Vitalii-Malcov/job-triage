"""Stage 17A: the test suite never touches a real database.

Loaded by pytest before any test module is imported, so the import-time
engine in `app/db/session.py` and every `Settings()` built during the run
see these values instead of the operator's `.env` (which may point at the
pilot or, through a shell override, the production runtime database).
Tests that need another database still pass their own explicit URL or
`monkeypatch.setenv` it; `DATABASE_ROLE=test` then refuses the production,
pilot and backup databases at application startup. The operator's `.env` is
not read at all (local runs see exactly what CI sees).
"""

import os
import tempfile
from pathlib import Path

import pytest

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="jobtriage-pytest-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEST_DB_DIR / 'jobtriage_test.db').as_posix()}"
os.environ["DATABASE_ROLE"] = "test"
os.environ["POSTGRES_HOST"] = ""

# The operator's `.env` holds real credentials (Bundesagentur API key, IMAP,
# Telegram). CI has no `.env`; without this a local run of a test that calls
# a real collector step with `Settings()` would reach the live services.
# Tests that need a specific env file still pass `_env_file=...` explicitly.
from app.core.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None


@pytest.fixture(scope="session", autouse=True)
def _default_engine_is_isolated():
    """Fail the run if the shared application engine is not the isolated
    test database (e.g. something rebound it to the runtime database)."""
    from app.core.database_target import DatabaseKind, classify_database_url
    from app.db import session

    kind, _ = classify_database_url(session.engine.url.render_as_string(hide_password=True))
    assert kind in {DatabaseKind.OTHER_SQLITE, DatabaseKind.MEMORY}, kind
    yield
