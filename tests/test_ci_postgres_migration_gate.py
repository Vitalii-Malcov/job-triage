"""NEW-007: static regression guarding the PostgreSQL CI migration gate.

Codex finding: the `scheduler-postgres` CI job's PostgreSQL integration
fixtures used `Base.metadata.create_all(engine)` to build the schema,
which can silently diverge from what the real Alembic migration chain
produces -- a broken migration would never be caught in CI. The fix
(see `.github/workflows/ci.yml` and both
`tests/integration/test_scheduler_postgres_concurrency.py` /
`tests/integration/test_gmail_watermark_postgres_concurrency.py`) is to
run `alembic upgrade head` against the CI PostgreSQL service, against a
genuinely empty database, before any integration test executes, and to
remove the `create_all` fallback from those fixtures entirely.

This module does not spin up PostgreSQL itself (that's what the real
`scheduler-postgres` CI job does) -- it statically proves the workflow
YAML and the fixture files still enforce that arrangement, so a future
edit cannot silently reintroduce the `create_all` masking or drop/reorder
the migration step without a test failing.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
INTEGRATION_DIR = PROJECT_ROOT / "tests" / "integration"

EXPECTED_ALEMBIC_HEAD = "b4f6a1c9e7d2"
# The actual `run:` step invocation, not just any mention of the path --
# the job's own explanatory comments reference this path too, earlier in
# the file, which would otherwise produce a false "before" ordering.
POSTGRES_INTEGRATION_PYTEST_STEP = (
    "- run: pytest -q tests/integration/test_scheduler_postgres_concurrency.py"
)


def _scheduler_postgres_job_text() -> str:
    text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    start = text.index("scheduler-postgres:")
    return text[start:]


def test_postgres_ci_job_runs_alembic_upgrade_head_before_integration_tests() -> None:
    job_text = _scheduler_postgres_job_text()

    migration_index = job_text.find("alembic upgrade head")
    assert migration_index != -1, (
        "scheduler-postgres CI job must run `alembic upgrade head` against "
        "the PostgreSQL service before integration tests (NEW-007)"
    )

    pytest_index = job_text.find(POSTGRES_INTEGRATION_PYTEST_STEP)
    assert pytest_index != -1, "expected the PostgreSQL integration pytest step"
    assert migration_index < pytest_index, (
        "alembic upgrade head must run BEFORE the PostgreSQL integration tests, "
        "otherwise a broken migration could still be masked by whatever created "
        "the schema first"
    )

    verify_index = job_text.find(f"{EXPECTED_ALEMBIC_HEAD} (head)")
    assert verify_index != -1, (
        "the CI migration step must verify the resulting Alembic head matches "
        f"the expected revision ({EXPECTED_ALEMBIC_HEAD}), not just that "
        "`upgrade head` exited 0"
    )
    assert migration_index < verify_index < pytest_index

    database_url_index = job_text.find("DATABASE_URL:")
    assert database_url_index != -1, (
        "alembic upgrade head must target the CI PostgreSQL service via DATABASE_URL"
    )
    assert migration_index < database_url_index < pytest_index
    line_end = job_text.index("\n", database_url_index)
    database_url_line = job_text[database_url_index:line_end]
    assert "postgres" in database_url_line.lower()


def test_postgres_integration_fixtures_do_not_mask_migrations_with_create_all() -> None:
    for name in (
        "test_scheduler_postgres_concurrency.py",
        "test_gmail_watermark_postgres_concurrency.py",
    ):
        path = INTEGRATION_DIR / name
        assert path.exists(), f"expected {path} to exist"
        text = path.read_text(encoding="utf-8")
        assert "Base.metadata.create_all(" not in text, (
            f"{name} must not call Base.metadata.create_all -- it would mask a "
            "broken Alembic migration behind ORM metadata creation (NEW-007)"
        )
