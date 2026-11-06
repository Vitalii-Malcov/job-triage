# CI (`.github/workflows/ci.yml`)

Three jobs. The workflow file is deliberately short; the reasoning lives here.

| Job | What it proves |
| --- | --- |
| `test` | `ruff check`, `ruff format --check`, the full `pytest -q` (SQLite) |
| `scheduler-postgres` | a real `postgres:16`: the Alembic chain on an empty database, then every PostgreSQL integration test, with **zero skips** |
| `docker` | the image builds and `compose.yaml` resolves (no container is started) |

Local runs of `pytest -q` skip the PostgreSQL modules (they need `TEST_POSTGRES_URL`); only this job executes them.
A green local run is therefore **not** evidence that the PostgreSQL job passes.

## The PostgreSQL job

1. **Migrate.** `alembic upgrade head` against the empty service database — never `Base.metadata.create_all`, which would hide
   a broken migration. The step then checks that the script tree has exactly one head and that the database is at **that** head;
   the head id is read from `alembic heads`, not written in the YAML (adding a migration needs no CI edit).
2. **Discover.** Both pytest phases select the whole `tests/integration` directory; there is no file list to maintain.
   A new `test_*postgres*.py` module is picked up automatically.
3. **Two phases.** A module that downgrades/upgrades Alembic on the shared database carries
   `pytest.mark.pg_migration_cycle` (registered in `pyproject.toml`) and restores head in a `finally`.
   Phase 1 (`-m "not pg_migration_cycle"`) runs everything else at head; phase 2 (`-m pg_migration_cycle`) runs the cycling
   modules. Within a phase the order does not matter.
4. **Zero skips.** Each phase writes a JUnit XML; an embedded checker fails the job if any phase ran no tests or skipped one
   (an unset `TEST_POSTGRES_URL` would otherwise turn the whole suite into a green skip).

## Guard: `tests/test_ci_postgres_migration_gate.py`

It parses the workflow and fails if a rule is weakened (mutation tests prove each rule rejects a broken workflow), **executes** the
embedded zero-skip checker against pytest-shaped XML, and checks the repository itself: every module that calls `downgrade(` is
marked `pg_migration_cycle` and no other is, the marker is registered, and there is a single linear migration head.
Tests no longer pin the head id either (`repository_head` fixture in `tests/conftest.py`).

## Adding things

* **A migration:** nothing to edit in CI or in the head-pin tests.
* **A PostgreSQL test module:** name it `tests/integration/test_<x>_postgres.py` and skip it without `TEST_POSTGRES_URL`
  (see any sibling). If it cycles Alembic, add `pytest.mark.pg_migration_cycle` to its `pytestmark` — the guard test tells you.

Limits: the guard proves the workflow *text* and the marker partition; the PostgreSQL tests themselves only run on GitHub Actions
(or against a local server: `TEST_POSTGRES_URL=... alembic upgrade head && pytest tests/integration`).
