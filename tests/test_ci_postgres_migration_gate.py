"""NEW-007 / S9D-CODEX-002: static regression guarding the PostgreSQL CI gate.

Codex finding (NEW-007): the `scheduler-postgres` CI job's PostgreSQL
integration fixtures used `Base.metadata.create_all(engine)` to build the
schema, which can silently diverge from what the real Alembic migration
chain produces -- a broken migration would never be caught in CI. The fix
(see `.github/workflows/ci.yml`) is to run `alembic upgrade head` against
the CI PostgreSQL service, against a genuinely empty database, before any
integration test executes, and to keep `create_all` out of the fixtures.

Codex finding (S9D-CODEX-002): the job still verified an OBSOLETE head
literal (this guard checked the same stale literal, so it kept passing),
and the required Stage 9C/9D PostgreSQL concurrency modules were never
run -- they skip without `TEST_POSTGRES_URL`. This guard now:

- derives the expected head from Alembic's own ScriptDirectory, so adding
  a migration fails here until CI verifies the new head;
- requires EVERY `tests/integration/*postgres*.py` module (explicitly
  including Stage 9C and 9D) to be invoked by the PostgreSQL job, so a
  required module cannot be silently omitted;
- requires `TEST_POSTGRES_URL` wiring plus the zero-skip enforcement, so
  the job cannot go green on environment-gated skips;
- (S9D-REREVIEW-001) EXECUTES the workflow's embedded zero-skip checker
  against pytest-shaped JUnit XML, so weakening its predicate (e.g.
  dropping `or skipped`) fails here even if every string still matches;
- keeps the HARD-008 migration-cycle module LAST.

This module does not spin up PostgreSQL itself (that's what the real
`scheduler-postgres` CI job does). `_gate_violations` is a pure function
of the workflow text, and the mutation tests below prove each rule would
actually reject a broken workflow.
"""

import functools
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
INTEGRATION_DIR = PROJECT_ROOT / "tests" / "integration"

OBSOLETE_ALEMBIC_HEAD = "a1b2c3d4e5f6"
STAGE_9C_PG_MODULE = "tests/integration/test_telegram_bewerbung_approval_postgres.py"
STAGE_9D_PG_MODULE = "tests/integration/test_gmail_application_draft_postgres.py"
HARD_008_PG_MODULE = "tests/integration/test_hard008_legacy_pending_migration_postgres.py"
MIGRATION_STEP_NAME = "- name: Alembic upgrade head against CI PostgreSQL"
PYTEST_STEP_NAME = "- name: PostgreSQL integration tests (zero skips)"
ZERO_SKIP_CHECK = "PostgreSQL integration gate requires zero skipped tests"
SKIP_PREDICATE = "if tests == 0 or skipped:"
CHECKER_START = re.compile(r"^[ \t]*python - (\S+) <<'PY'[ \t]*$", re.M)
CHECKER_END = re.compile(r"^[ \t]*PY[ \t]*$", re.M)
# (tests, skipped) -> must the checker let the job pass?
JUNIT_CONTRACT = {(50, 0): True, (21, 21): False, (21, 1): False, (0, 0): False}


def _repository_head() -> str:
    """The single head of the repository's migration scripts."""
    heads = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"migration scripts must have exactly one head, found {heads}"
    return heads[0]


def _postgres_modules() -> list[str]:
    """Every PostgreSQL integration module in the repository."""
    return sorted(
        f"tests/integration/{path.name}" for path in INTEGRATION_DIR.glob("test_*postgres*.py")
    )


def _job_text(workflow: str) -> str:
    """The `scheduler-postgres` job, up to the next top-level job."""
    start = workflow.index("\n  scheduler-postgres:")
    following = re.search(r"\n  [A-Za-z0-9_-]+:\n", workflow[start + 1 :])
    return workflow[start : start + 1 + following.start()] if following else workflow[start:]


def _step(job: str, name: str) -> str | None:
    """The text of the step called `name` (up to the next step)."""
    start = job.find(name)
    if start == -1:
        return None
    following = job.find("\n      - ", start + len(name))
    return job[start:] if following == -1 else job[start:following]


def _pytest_targets(step: str) -> list[str]:
    """The test module paths actually passed to `pytest` (comments ignored)."""
    code = "\n".join(line.split("#", 1)[0] for line in step.splitlines())
    match = re.search(r"\bpytest\b(.*?)(?:\n\s*\n|\n\s*python\b|$)", code, re.S)
    if match is None:
        return []
    return re.findall(r"tests/integration/[\w./-]+\.py", match.group(1))


def _zero_skip_checker(step: str) -> tuple[str, str] | None:
    """(JUnit path, Python source) of the step's `python - <path> <<'PY'` heredoc."""
    start = CHECKER_START.search(step)
    if start is None:
        return None
    end = CHECKER_END.search(step, start.end())
    if end is None:
        return None
    # The YAML block scalar strips the step's indentation before bash runs it.
    return start.group(1), textwrap.dedent(step[start.end() + 1 : end.start()])


def _pytest_junit_xml(tests: int, skipped: int) -> str:
    """JUnit XML in the exact shape pytest emits: testsuites > testsuite > testcase."""
    cases = "".join(
        f'<testcase classname="tests.integration.test_pg" name="test_{i}" time="0.001">'
        + (
            '<skipped type="pytest.skip" message="TEST_POSTGRES_URL not set">skip</skipped>'
            if i < skipped
            else ""
        )
        + "</testcase>"
        for i in range(tests)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="0" failures="0" skipped="{skipped}" '
        f'tests="{tests}" time="0.1" timestamp="2026-01-01T00:00:00" hostname="ci">'
        f"{cases}</testsuite></testsuites>"
    )


@functools.cache
def _checker_accepts(source: str, tests: int, skipped: int) -> bool:
    """Run the checker exactly as CI does (`python - <xml>` on stdin); True iff exit 0."""
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / "postgres-junit.xml"
        xml_path.write_text(_pytest_junit_xml(tests, skipped), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-", str(xml_path)],
            input=source,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    return result.returncode == 0


def _checker_violations(pytest_step: str) -> list[str]:
    junit = re.search(r"--junitxml=(\S+)", pytest_step)
    checker = _zero_skip_checker(pytest_step)
    if junit is None or checker is None or checker[0] != junit.group(1):
        return ["pytest step must check its own JUnit XML for skipped tests"]
    violations = []
    for (tests, skipped), accept in JUNIT_CONTRACT.items():
        if _checker_accepts(checker[1], tests, skipped) != accept:
            verdict = "accept" if accept else "reject"
            violations.append(f"zero-skip checker must {verdict} {tests} tests / {skipped} skipped")
    return violations


def _gate_violations(workflow: str, head: str, required_modules: list[str]) -> list[str]:
    violations: list[str] = []
    job = _job_text(workflow)
    if "image: postgres:16" not in job:
        violations.append("PostgreSQL job must use the postgres:16 service")

    migration = _step(job, MIGRATION_STEP_NAME)
    pytest_step = _step(job, PYTEST_STEP_NAME)
    if migration is None:
        return [*violations, "missing Alembic upgrade-head migration step"]
    if pytest_step is None:
        return [*violations, "missing PostgreSQL integration pytest step"]
    if job.index(MIGRATION_STEP_NAME) > job.index(PYTEST_STEP_NAME):
        violations.append("alembic upgrade head must run BEFORE the PostgreSQL tests")

    if "alembic upgrade head" not in migration:
        violations.append("migration step must run `alembic upgrade head`")
    if not re.search(r"DATABASE_URL:\s*postgresql\+psycopg://", migration):
        violations.append("migration step must target the PostgreSQL service via DATABASE_URL")
    if f'grep -q "{head} (head)"' not in migration:
        violations.append(f"migration step must verify the CURRENT repository head {head}")
    stale = set(re.findall(r"\b([0-9a-f]{12}) \(head\)", migration)) - {head}
    if stale:
        violations.append(f"migration step verifies a stale head: {sorted(stale)}")
    if "alembic heads" not in migration or "-eq 1" not in migration:
        violations.append("migration step must prove the script tree has exactly one head")

    if not re.search(r"TEST_POSTGRES_URL:\s*postgresql\+psycopg://", pytest_step):
        violations.append("pytest step must provide TEST_POSTGRES_URL for the service")
    if 'test -n "${TEST_POSTGRES_URL:-}"' not in pytest_step:
        violations.append("pytest step must refuse to run without TEST_POSTGRES_URL")
    if "--junitxml=" not in pytest_step or ZERO_SKIP_CHECK not in pytest_step:
        violations.append("pytest step must fail on ANY skipped PostgreSQL test")
    violations.extend(_checker_violations(pytest_step))
    if "pipefail" not in pytest_step or "pipefail" not in migration:
        violations.append("PostgreSQL steps must run with `set -euo pipefail`")

    targets = _pytest_targets(pytest_step)
    for module in required_modules:
        if module not in targets:
            violations.append(f"required PostgreSQL module not invoked: {module}")
    if targets and targets[-1] != HARD_008_PG_MODULE:
        violations.append("HARD-008 migration-cycle module must run LAST")
    return violations


def _workflow() -> str:
    return CI_WORKFLOW_PATH.read_text(encoding="utf-8")


def _required() -> list[str]:
    return _postgres_modules()


# --- the real workflow -------------------------------------------------------------


def test_required_modules_include_stage_9c_9d_and_hard008() -> None:
    required = _required()
    for module in (STAGE_9C_PG_MODULE, STAGE_9D_PG_MODULE, HARD_008_PG_MODULE):
        assert module in required
        assert (PROJECT_ROOT / module).exists()


def test_repository_head_is_the_stage_9d_head() -> None:
    # Moves with the next migration on purpose: that migration must also
    # update the CI literal (see test_real_workflow_satisfies_the_gate).
    assert _repository_head() == "f3b8d2a6c9e1"


def test_real_workflow_satisfies_the_postgres_gate() -> None:
    assert _gate_violations(_workflow(), _repository_head(), _required()) == []


def test_postgres_job_runs_alembic_upgrade_head_before_integration_tests() -> None:
    job = _job_text(_workflow())
    assert job.index(MIGRATION_STEP_NAME) < job.index(PYTEST_STEP_NAME)
    targets = _pytest_targets(_step(job, PYTEST_STEP_NAME))
    assert targets[-1] == HARD_008_PG_MODULE
    assert set(targets) == set(_required())


def test_postgres_integration_fixtures_do_not_mask_migrations_with_create_all() -> None:
    for module in _required():
        text = (PROJECT_ROOT / module).read_text(encoding="utf-8")
        assert "Base.metadata.create_all(" not in text, (
            f"{module} must not call Base.metadata.create_all -- it would mask a "
            "broken Alembic migration behind ORM metadata creation (NEW-007)"
        )


# --- mutations: each rule really rejects a broken workflow ----------------------------


def _mutated(old: str, new: str) -> list[str]:
    workflow = _workflow()
    assert old in workflow, f"mutation anchor not found: {old!r}"
    return _gate_violations(workflow.replace(old, new), _repository_head(), _required())


def test_obsolete_head_is_rejected() -> None:
    head = _repository_head()
    violations = _mutated(f'grep -q "{head} (head)"', f'grep -q "{OBSOLETE_ALEMBIC_HEAD} (head)"')
    assert any("CURRENT repository head" in v for v in violations)
    assert any(OBSOLETE_ALEMBIC_HEAD in v for v in violations)


def test_a_new_migration_head_fails_until_ci_is_updated() -> None:
    violations = _gate_violations(_workflow(), "0123456789ab", _required())
    assert any("CURRENT repository head 0123456789ab" in v for v in violations)


@pytest.mark.parametrize(
    "module", [STAGE_9C_PG_MODULE, STAGE_9D_PG_MODULE], ids=["stage-9c", "stage-9d"]
)
def test_deleting_a_required_module_invocation_fails(module: str) -> None:
    violations = _mutated(f"            {module} \\\n", "")
    assert f"required PostgreSQL module not invoked: {module}" in violations


@pytest.mark.parametrize(
    "module", [STAGE_9C_PG_MODULE, STAGE_9D_PG_MODULE], ids=["stage-9c", "stage-9d"]
)
def test_commenting_out_a_required_module_fails(module: str) -> None:
    violations = _mutated(f"            {module} \\\n", f"            # {module} \\\n")
    assert f"required PostgreSQL module not invoked: {module}" in violations


def test_a_new_postgres_module_must_be_wired_into_ci() -> None:
    required = [*_required(), "tests/integration/test_future_stage_postgres.py"]
    violations = _gate_violations(_workflow(), _repository_head(), required)
    assert violations == [
        "required PostgreSQL module not invoked: tests/integration/test_future_stage_postgres.py"
    ]


def test_missing_test_postgres_url_is_rejected() -> None:
    violations = _mutated(
        "          TEST_POSTGRES_URL: postgresql+psycopg://",
        "          OTHER_URL: postgresql+psycopg://",
    )
    assert "pytest step must provide TEST_POSTGRES_URL for the service" in violations


def test_environment_gated_skips_cannot_pass_the_job() -> None:
    assert "pytest step must fail on ANY skipped PostgreSQL test" in _mutated(
        f'sys.exit("{ZERO_SKIP_CHECK}")', 'print("skips tolerated")'
    )
    assert "pytest step must refuse to run without TEST_POSTGRES_URL" in _mutated(
        'test -n "${TEST_POSTGRES_URL:-}"', "true"
    )


def test_hard008_must_stay_last() -> None:
    workflow = _workflow()
    hard008 = f"            {HARD_008_PG_MODULE}\n"
    stage9d = f"            {STAGE_9D_PG_MODULE} \\\n"
    reordered = workflow.replace(hard008, "").replace(
        stage9d, f"            {HARD_008_PG_MODULE} \\\n" + stage9d.replace(" \\\n", "\n")
    )
    violations = _gate_violations(reordered, _repository_head(), _required())
    assert "HARD-008 migration-cycle module must run LAST" in violations


def test_migration_after_tests_is_rejected() -> None:
    workflow = _workflow()
    job = _job_text(workflow)
    migration = _step(job, MIGRATION_STEP_NAME)
    pytest_step = _step(job, PYTEST_STEP_NAME)
    swapped_job = job.replace(migration, "\x00").replace(pytest_step, migration)
    swapped_job = swapped_job.replace("\x00", pytest_step)
    violations = _gate_violations(
        workflow.replace(job, swapped_job), _repository_head(), _required()
    )
    assert "alembic upgrade head must run BEFORE the PostgreSQL tests" in violations


def test_dropping_the_migration_step_is_rejected() -> None:
    violations = _mutated(MIGRATION_STEP_NAME, "- name: something else")
    assert "missing Alembic upgrade-head migration step" in violations


# --- S9D-REREVIEW-001: the zero-skip checker's real behavior ------------------------


def _real_checker() -> str:
    step = _step(_job_text(_workflow()), PYTEST_STEP_NAME)
    checker = _zero_skip_checker(step)
    assert checker is not None
    assert checker[0] == "/tmp/postgres-junit.xml"
    return checker[1]


@pytest.mark.parametrize(
    ("tests", "skipped", "accept"),
    [(tests, skipped, accept) for (tests, skipped), accept in JUNIT_CONTRACT.items()],
    ids=["50-tests-0-skipped", "all-skipped", "one-skipped", "no-tests"],
)
def test_real_zero_skip_checker_behavior(tests: int, skipped: int, accept: bool) -> None:
    assert _checker_accepts(_real_checker(), tests, skipped) is accept


def test_dropping_the_skip_predicate_is_rejected() -> None:
    # Codex's exact mutation: every string the old guard looked for survives it.
    weakened = _real_checker().replace(SKIP_PREDICATE, "if tests == 0:")
    assert ZERO_SKIP_CHECK in weakened and SKIP_PREDICATE not in weakened
    assert _checker_accepts(weakened, 21, 21)  # why it is invalid: skips go green
    violations = _mutated(SKIP_PREDICATE, "if tests == 0:")
    assert "zero-skip checker must reject 21 tests / 21 skipped" in violations
    assert "zero-skip checker must reject 21 tests / 1 skipped" in violations


def test_dropping_the_empty_run_predicate_is_rejected() -> None:
    violations = _mutated(SKIP_PREDICATE, "if skipped:")
    assert violations == ["zero-skip checker must reject 0 tests / 0 skipped"]


def test_checker_must_read_the_xml_pytest_writes() -> None:
    violations = _mutated("python - /tmp/postgres-junit.xml", "python - /tmp/other.xml")
    assert "pytest step must check its own JUnit XML for skipped tests" in violations


def test_checker_failure_cannot_be_swallowed() -> None:
    violations = _mutated("<<'PY'\n", "<<'PY' || true\n")
    assert "pytest step must check its own JUnit XML for skipped tests" in violations
