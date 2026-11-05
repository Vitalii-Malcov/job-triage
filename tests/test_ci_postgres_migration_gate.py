"""Static regression guarding the PostgreSQL CI gate (`scheduler-postgres` job).

What the workflow must guarantee, and what this module proves (`_gate_violations` is a pure
function of the workflow text; the mutation tests below show that each rule would really reject a
broken workflow -- this module does not start PostgreSQL itself):

- the schema is created by `alembic upgrade head` on an empty PostgreSQL BEFORE any test (never by
  `Base.metadata.create_all`), and the expected head is read from the script tree (exactly one
  head) -- NOT a revision literal that every new migration would have to edit;
- tests are DISCOVERED (`tests/integration`, no hand-maintained file list): modules that cycle
  Alembic down/up on the shared database carry the `pg_migration_cycle` marker and run in a second
  phase, everything else runs first, at head; `_marker_violations` checks that partition against
  the repository so a new module is classified, not forgotten;
- `TEST_POSTGRES_URL` is wired and required, and EVERY phase's JUnit XML is checked by the
  workflow's embedded zero-skip checker, which this module EXECUTES against pytest-shaped XML (so
  weakening its predicate fails here even if every string still matches).
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

MIGRATION_STEP_NAME = "- name: Alembic upgrade head against CI PostgreSQL"
PYTEST_STEP_NAME = "- name: PostgreSQL integration tests (zero skips)"
ZERO_SKIP_CHECK = "PostgreSQL integration gate requires zero skipped tests"
CYCLE_MARKER = "pg_migration_cycle"
PHASE_SHAPE = re.compile(
    rf'pytest -q -rs --junitxml=\S+ tests/integration -m ("not {CYCLE_MARKER}"|{CYCLE_MARKER})'
)
CHECKER_START = re.compile(r"^[ \t]*python - ([^<\n]+?) <<'PY'[ \t]*$", re.M)
CHECKER_END = re.compile(r"^[ \t]*PY[ \t]*$", re.M)
# ((tests, skipped) of each phase) -> must the checker let the job pass?
JUNIT_CONTRACT = {
    ((50, 0), (6, 0)): True,
    ((50, 0), (6, 1)): False,
    ((21, 21), (6, 0)): False,
    ((21, 1), (6, 0)): False,
    ((0, 0), (6, 0)): False,
    ((50, 0), (0, 0)): False,  # an EMPTY phase means a marker or selector broke
}


def _repository_head() -> str:
    heads = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"migration scripts must have exactly one head, found {heads}"
    return heads[0]


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


def _pytest_commands(step: str) -> list[str]:
    """The logical `pytest ...` command lines as bash reads them: comments stripped, only a
    trailing backslash joins lines."""
    code = "\n".join(line.split("#", 1)[0] for line in step.splitlines())
    return [
        line.strip()
        for line in re.sub(r"\\\n", " ", code).splitlines()
        if re.match(r"\s*pytest\b", line)
    ]


def _zero_skip_checker(step: str) -> tuple[list[str], str] | None:
    """(JUnit paths, Python source) of the step's `python - <paths> <<'PY'` heredoc."""
    start = CHECKER_START.search(step)
    if start is None:
        return None
    end = CHECKER_END.search(step, start.end())
    if end is None:
        return None
    # The YAML block scalar strips the step's indentation before bash runs it.
    return start.group(1).split(), textwrap.dedent(step[start.end() + 1 : end.start()])


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
def _checker_accepts(source: str, phases: tuple[tuple[int, int], ...]) -> bool:
    """Run the checker exactly as CI does (`python - <xml files>` on stdin); True iff exit 0."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for index, (tests, skipped) in enumerate(phases):
            path = Path(tmp) / f"phase-{index}.xml"
            path.write_text(_pytest_junit_xml(tests, skipped), encoding="utf-8")
            paths.append(str(path))
        result = subprocess.run(
            [sys.executable, "-", *paths],
            input=source,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    return result.returncode == 0


def _checker_violations(step: str, commands: list[str]) -> list[str]:
    junit_paths = [m.group(1) for c in commands for m in re.finditer(r"--junitxml=(\S+)", c)]
    checker = _zero_skip_checker(step)
    if len(junit_paths) != len(commands) or checker is None or checker[0] != junit_paths:
        return ["every pytest phase must write a JUnit XML that the zero-skip checker reads"]
    violations = []
    for phases, accept in JUNIT_CONTRACT.items():
        if _checker_accepts(checker[1], phases) != accept:
            verdict = "accept" if accept else "reject"
            violations.append(f"zero-skip checker must {verdict} phases {phases}")
    return violations


def _gate_violations(workflow: str) -> list[str]:
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
    if "alembic heads" not in migration or "-eq 1" not in migration:
        violations.append("migration step must prove the script tree has exactly one head")
    if not re.search(r'grep -q "\$\{head_id\} \(head\)"', migration):
        violations.append("migration step must verify the DATABASE is at the script tree's head")
    literal = re.findall(r"\b[0-9a-f]{12}\b", re.sub(r"#.*", "", migration))
    if literal:
        violations.append(f"migration step hard-codes a revision id: {sorted(set(literal))}")

    if not re.search(r"TEST_POSTGRES_URL:\s*postgresql\+psycopg://", pytest_step):
        violations.append("pytest step must provide TEST_POSTGRES_URL for the service")
    if 'test -n "${TEST_POSTGRES_URL:-}"' not in pytest_step:
        violations.append("pytest step must refuse to run without TEST_POSTGRES_URL")
    if ZERO_SKIP_CHECK not in pytest_step:
        violations.append("pytest step must fail on ANY skipped PostgreSQL test")
    if "pipefail" not in pytest_step or "pipefail" not in migration:
        violations.append("PostgreSQL steps must run with `set -euo pipefail`")

    commands = _pytest_commands(pytest_step)
    at_head = [c for c in commands if f"not {CYCLE_MARKER}" in c]
    cycling = [c for c in commands if re.search(rf'-m\s+"?{CYCLE_MARKER}"?(\s|$)', c)]
    if len(commands) != 2 or len(at_head) != 1 or len(cycling) != 1:
        violations.append("exactly two pytest phases are required: `not marker` then `marker`")
    elif commands.index(at_head[0]) > commands.index(cycling[0]):
        violations.append("the at-head phase must run BEFORE the migration-cycle phase")
    for command in commands:
        if not re.search(r"\btests/integration\b(?!/)", command):
            violations.append(
                f"phase must discover the whole tests/integration directory: {command}"
            )
        if re.search(r"tests/integration/\S+\.py", command):
            violations.append(f"phase must not list individual modules: {command}")
        if not PHASE_SHAPE.fullmatch(command):
            # no -k / --deselect / --ignore / extra paths: nothing may be silently left out
            violations.append(f"phase has an unexpected selector or option: {command}")
    violations.extend(_checker_violations(pytest_step, commands))
    return violations


def _marker_violations(directory: Path) -> list[str]:
    """The partition rule: a PostgreSQL module that downgrades Alembic must be marked
    `pg_migration_cycle`; a module that never downgrades must not be (it would run late for
    nothing and hide that it is order-independent)."""
    violations = []
    for path in sorted(directory.glob("test_*postgres*.py")):
        text = path.read_text(encoding="utf-8")
        cycles = re.search(r"\bdowngrade\(", text) is not None
        marked = f"pytest.mark.{CYCLE_MARKER}" in text
        if cycles and not marked:
            violations.append(f"{path.name} downgrades Alembic but is not marked {CYCLE_MARKER}")
        if marked and not cycles:
            violations.append(f"{path.name} is marked {CYCLE_MARKER} but never downgrades")
    return violations


def _workflow() -> str:
    return CI_WORKFLOW_PATH.read_text(encoding="utf-8")


# --- the real workflow and repository ------------------------------------------------


def test_real_workflow_satisfies_the_postgres_gate() -> None:
    assert _gate_violations(_workflow()) == []


def test_repository_has_a_single_linear_migration_head() -> None:
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1
    assert [r.revision for r in script.walk_revisions() if len(r.nextrev) > 1] == []


def test_module_markers_match_the_modules_that_cycle_alembic() -> None:
    assert _marker_violations(INTEGRATION_DIR) == []


def test_the_cycle_marker_is_registered() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"{CYCLE_MARKER}:' in pyproject


def test_every_integration_module_is_discoverable_by_the_phases() -> None:
    """The phases select tests/integration wholesale; every module there must be a test module
    that pytest collects (named test_*.py), so nothing is silently left out."""
    stray = [
        p.name
        for p in INTEGRATION_DIR.glob("*.py")
        if not p.name.startswith("test_") and p.name != "__init__.py"
    ]
    assert stray == []


def test_postgres_job_runs_alembic_upgrade_head_before_integration_tests() -> None:
    job = _job_text(_workflow())
    assert job.index(MIGRATION_STEP_NAME) < job.index(PYTEST_STEP_NAME)


def test_postgres_integration_fixtures_do_not_mask_migrations_with_create_all() -> None:
    offenders = [
        path.name
        for path in INTEGRATION_DIR.glob("test_*postgres*.py")
        if "Base.metadata.create_all(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], "create_all would mask a broken Alembic migration (NEW-007)"


def test_the_workflow_does_not_pin_the_current_head() -> None:
    assert _repository_head() not in _workflow()


# --- mutation tests: each rule must reject a broken workflow -------------------------


def _broken(substitution: tuple[str, str]) -> list[str]:
    old, new = substitution
    workflow = _workflow()
    assert old in workflow, old
    return _gate_violations(workflow.replace(old, new, 1))


@pytest.mark.parametrize(
    "substitution",
    [
        ('grep -q "${head_id} (head)" /tmp/alembic_current.txt', "true"),
        ("python -m alembic upgrade head\n", "true\n"),
        ("test \"$(grep -c '(head)' /tmp/alembic_heads.txt)\" -eq 1", "true"),
        ('test -n "${TEST_POSTGRES_URL:-}"', "true"),
        ("set -euo pipefail", "set -eu"),
    ],
)
def test_weakening_a_step_is_rejected(substitution: tuple[str, str]) -> None:
    assert _broken(substitution) != []


def test_a_hard_coded_revision_in_the_migration_step_is_rejected() -> None:
    violations = _broken(('"${head_id} (head)"', '"a2b7d4f9c1e3 (head)"'))
    assert any("hard-codes a revision id" in v or "script tree's head" in v for v in violations)


def test_listing_individual_modules_instead_of_discovery_is_rejected() -> None:
    violations = _broken(
        ('tests/integration -m "not pg_migration_cycle"', "tests/integration/test_a_postgres.py")
    )
    assert any("discover the whole tests/integration" in v for v in violations)


def test_dropping_a_phase_is_rejected() -> None:
    line = (
        "pytest -q -rs --junitxml=/tmp/postgres-migration-cycle.xml tests/integration "
        "-m pg_migration_cycle\n"
    )
    assert any("exactly two pytest phases" in v for v in _broken((line, "")))


def test_swapping_the_phase_order_is_rejected() -> None:
    workflow = _workflow()
    first = (
        'pytest -q -rs --junitxml=/tmp/postgres-at-head.xml tests/integration -m "not '
        'pg_migration_cycle"\n'
    )
    second = (
        "pytest -q -rs --junitxml=/tmp/postgres-migration-cycle.xml tests/integration "
        "-m pg_migration_cycle\n"
    )
    assert first in workflow and second in workflow
    swapped = workflow.replace(first, "@@A@@").replace(second, first).replace("@@A@@", second)
    assert any("BEFORE the migration-cycle" in v for v in _gate_violations(swapped))


def test_migration_after_tests_is_rejected() -> None:
    workflow = _workflow()
    job = _job_text(workflow)
    migration = _step(job, MIGRATION_STEP_NAME)
    pytest_step = _step(job, PYTEST_STEP_NAME)
    swapped = (
        workflow.replace(migration, "@@M@@")
        .replace(pytest_step, migration)
        .replace("@@M@@", pytest_step)
    )
    assert any("BEFORE the PostgreSQL tests" in v for v in _gate_violations(swapped))


def test_dropping_the_migration_step_is_rejected() -> None:
    workflow = _workflow()
    migration = _step(_job_text(workflow), MIGRATION_STEP_NAME)
    assert "missing Alembic upgrade-head migration step" in _gate_violations(
        workflow.replace(migration, "")
    )


@pytest.mark.parametrize(("phases", "accept"), list(JUNIT_CONTRACT.items()))
def test_real_zero_skip_checker_behavior(phases, accept) -> None:
    step = _step(_job_text(_workflow()), PYTEST_STEP_NAME)
    paths, source = _zero_skip_checker(step)
    assert len(paths) == 2
    assert _checker_accepts(source, phases) is accept


def test_dropping_the_skip_predicate_is_rejected() -> None:
    violations = _broken(("failed or tests == 0 or skipped > 0", "failed or tests == 0"))
    assert violations == [] or any("zero-skip checker" in v for v in violations)
    assert _broken(("tests == 0 or skipped > 0", "skipped > 0")) != []
    assert _broken(("tests == 0 or skipped > 0", "tests == 0")) != []


def test_a_checker_that_ignores_a_phase_is_rejected() -> None:
    assert (
        _broken(
            (
                "python - /tmp/postgres-at-head.xml /tmp/postgres-migration-cycle.xml <<'PY'",
                "python - /tmp/postgres-at-head.xml <<'PY'",
            )
        )
        != []
    )


# --- the marker partition rule -------------------------------------------------------


def test_an_unmarked_module_that_cycles_alembic_is_rejected(tmp_path) -> None:
    (tmp_path / "test_new_postgres.py").write_text(
        "def test_x():\n    downgrade(cfg, 'abc')\n", encoding="utf-8"
    )
    assert any("not marked" in v for v in _marker_violations(tmp_path))


def test_a_marked_module_that_never_cycles_is_rejected(tmp_path) -> None:
    (tmp_path / "test_new_postgres.py").write_text(
        "import pytest\npytestmark = pytest.mark.pg_migration_cycle\n", encoding="utf-8"
    )
    assert any("never downgrades" in v for v in _marker_violations(tmp_path))


def test_a_correctly_classified_new_module_passes(tmp_path) -> None:
    (tmp_path / "test_at_head_postgres.py").write_text(
        "def test_x():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "test_cycle_postgres.py").write_text(
        "import pytest\npytestmark = [pytest.mark.pg_migration_cycle]\n"
        "def test_x():\n    downgrade(cfg, 'abc')\n",
        encoding="utf-8",
    )
    assert _marker_violations(tmp_path) == []


@pytest.mark.parametrize(
    "extra", ['-k "not Legacy"', "--deselect x", "--ignore=tests/integration/y.py", "-x"]
)
def test_extra_selectors_that_silently_omit_tests_are_rejected(extra: str) -> None:
    violations = _broken(("-m pg_migration_cycle\n", f"-m pg_migration_cycle {extra}\n"))
    assert any("unexpected selector or option" in v for v in violations)
