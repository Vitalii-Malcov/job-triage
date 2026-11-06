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
# Task 23: the real web server refuses a second process; the suite starts many app lifespans.
os.environ["SINGLE_PROCESS_LOCK_ENABLED"] = "false"
# the suite baseline is the default in-memory backend whatever the shell exports
os.environ["SHARED_STATE_BACKEND"] = "memory"
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


@pytest.fixture(autouse=True)
def _reset_auth_failure_budget():
    """The failed-auth budget is per-process state shared by every TestClient
    (same "testclient" peer); clear it so tests cannot lock each other out."""
    from app.security import rate_limit

    rate_limit._auth_failure_requests.clear()
    yield
    rate_limit._auth_failure_requests.clear()


@pytest.fixture(scope="session")
def repository_head() -> str:
    """The single Alembic head of the migration scripts, read from the script tree itself, so no
    test (and no CI step) hard-codes a revision id that every new migration would have to edit."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    heads = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"migration scripts must have exactly one head, found {heads}"
    return heads[0]


@pytest.fixture(scope="session")
def imported_modules():
    """`imported_modules(module) -> set[str]`: every module name a Python module imports STATICALLY
    (import statements in its AST, relative imports resolved against its package; dynamic
    `importlib`/`__import__` calls are invisible), so formatting, comments and docstrings that
    merely MENTION a library never matter. A name is recorded dotted ("urllib.request") together
    with its parents ("urllib"), and `from a import b` records both "a" and "a.b"."""
    import ast
    import importlib.util
    import inspect

    def collect(module) -> set[str]:
        package = module.__package__ or module.__name__.rpartition(".")[0]
        names: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                found = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), package
                    )
                else:
                    base = node.module
                if not base:
                    continue
                found = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            else:
                continue
            for name in found:
                parts = name.split(".")
                names.update(".".join(parts[: i + 1]) for i in range(len(parts)))
        return names

    return collect
