"""`jobtriage` -- one stable command line for the operator scripts.

The scripts under `scripts/` carry the stage in which they were built in their file
names (`stage22_refresh_ba.py`), so daily operations depended on development history.
This dispatcher gives every script a stable, descriptive command name:

    python -m app.cli --list
    python -m app.cli refresh-ba --database runtime/job_triage_runtime.db --limit 20
    jobtriage candidate-config show --database ...        # after `pip install -e .`

It adds no behaviour: it loads the script file and calls its own entry point with the
remaining arguments, so exit codes, option names and safety checks (database target
refusal, dry-run defaults, ...) are exactly the script's. The old paths
(`python scripts/stage22_refresh_ba.py ...`) keep working unchanged -- existing runbooks and
automation need no edit. The scripts are operator tools of a repository checkout and are not
part of the installed package or the Docker image; without them this command says so.
"""

import importlib.util
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


@dataclass(frozen=True)
class Command:
    name: str
    script: str  # file name under scripts/
    summary: str
    # The script's command-line function, when it is not the default `run` then `main`
    # (candidate_profile_transfer_stage13b.run takes a parsed Namespace, its `main(argv)`
    # is the command line).
    entry: str | None = None


COMMANDS: tuple[Command, ...] = (
    Command(
        "send-readiness", "stage14a_send_readiness.py", "readiness report before enabling sends"
    ),
    Command(
        "canonical-runtime",
        "stage14d_canonical_runtime.py",
        "preflight / serve the canonical runtime",
    ),
    Command("applications", "stage15_application_tracker.py", "application lifecycle tracker"),
    Command(
        "import-application", "stage15_import_ald_application.py", "import one existing application"
    ),
    Command("telegram-operator", "stage16_telegram_operator.py", "Telegram operator control"),
    Command(
        "candidate-config",
        "stage17_candidate_config.py",
        "show / apply the candidate search configuration",
    ),
    Command("job-fit", "stage17_job_fit.py", "commute and fit assessment of vacancies"),
    Command("assess-vacancies", "stage21_assess_vacancies.py", "fill the vacancy fit cache"),
    Command("backup", "backup_sqlite.py", "verified consistent backup of the SQLite database"),
    Command(
        "match-vacancies", "stage22_match_vacancies.py", "match vacancies against the candidate"
    ),
    Command("refresh-ba", "stage22_refresh_ba.py", "refresh stored Bundesagentur vacancies"),
    Command("ba-liveness", "ba_liveness.py", "check whether stored BA vacancies are still online"),
    Command(
        "backfill-source-reference", "backfill_source_reference.py", "fill jobs.source_reference"
    ),
    Command(
        "vacancy-identity-report",
        "vacancy_identity_report.py",
        "report vacancy identity collisions",
    ),
    Command("operator-ui", "run_operator_ui.py", "start the read-only operator UI"),
    Command("presubmit-check", "presubmit_check.py", "mandatory pre-submit check for a Bewerbung"),
    Command(
        "profile-transfer",
        "candidate_profile_transfer_stage13b.py",
        "export / import the candidate profile",
        entry="main",
    ),
    Command("rescore-offline", "offline_rescore_stage12.py", "offline re-score preview"),
    Command(
        "build-common-stems", "build_job_ad_common_stems.py", "rebuild the job-ad common-stem list"
    ),
    Command(
        "strip-review-attribution",
        "strip_review_attribution.py",
        "remove review provenance from comments/docstrings of app/",
    ),
    Command("check-quality", "check_tracked_quality.py", "ruff gate over tracked sources"),
)
BY_NAME = {command.name: command for command in COMMANDS}


def _load(command: Command):
    path = SCRIPTS_DIR / command.script
    if not path.is_file():
        raise FileNotFoundError(
            f"{command.script} not found under {SCRIPTS_DIR}: the operator scripts belong to a "
            "repository checkout (they are not part of the installed package or the image)"
        )
    spec = importlib.util.spec_from_file_location(f"jobtriage_script_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entry(module, preferred: str | None = None) -> tuple[Callable, bool]:
    """(callable, takes_argv): the command's declared entry point, else `run(argv)`, then
    `main(argv)`, then a bare `main()`."""
    for name in (preferred,) if preferred else ("run", "main"):
        function = getattr(module, name, None)
        if callable(function):
            parameters = list(inspect.signature(function).parameters.values())
            takes_argv = bool(parameters) and parameters[0].kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            return function, takes_argv
    raise AttributeError(f"{module.__name__} has no usable command-line entry point")


def _exit_code(value) -> int:
    """Like the interpreter: None -> 0, int -> itself, anything else is printed to stderr
    (the diagnostic of `raise SystemExit("reason")`) and exits 1."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    print(value, file=sys.stderr)
    return 1


def dispatch(command: Command, argv: list[str]) -> int:
    module = _load(command)
    function, takes_argv = _entry(module, command.entry)
    if takes_argv:
        try:
            return _exit_code(function(argv))
        except SystemExit as exc:
            return _exit_code(exc.code)
    saved = sys.argv
    sys.argv = [command.script, *argv]
    try:
        return _exit_code(function())
    except SystemExit as exc:
        return _exit_code(exc.code)
    finally:
        sys.argv = saved


def _print_list(stream) -> None:
    width = max(len(command.name) for command in COMMANDS)
    print("jobtriage commands (old script paths keep working):", file=stream)
    for command in COMMANDS:
        print(
            f"  {command.name.ljust(width)}  {command.summary}  [scripts/{command.script}]",
            file=stream,
        )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "--list", "list"}:
        _print_list(sys.stdout)
        return 0
    command = BY_NAME.get(args[0])
    if command is None:
        print(f"unknown command: {args[0]!r}", file=sys.stderr)
        _print_list(sys.stderr)
        return 2
    try:
        return dispatch(command, args[1:])
    except FileNotFoundError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
