"""Architecture 3: operator scripts get stable command names (app/cli.py) without
breaking their old paths."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import cli

ROOT = Path(__file__).resolve().parents[1]


def test_every_operator_script_has_exactly_one_stable_command():
    scripts = {p.name for p in (ROOT / "scripts").glob("*.py") if p.name != "__init__.py"}
    registered = [command.script for command in cli.COMMANDS]
    assert sorted(registered) == sorted(set(registered))  # no script twice
    assert set(registered) == scripts  # a new script must be registered here


def test_command_names_are_unique_and_do_not_carry_a_development_stage():
    names = [command.name for command in cli.COMMANDS]
    assert len(names) == len(set(names))
    assert not [n for n in names if re.search(r"stage\d|\d", n)]


def test_every_script_resolves_to_a_callable_entry_point():
    for command in cli.COMMANDS:
        module = cli._load(command)
        function, _takes_argv = cli._entry(module)
        assert callable(function), command.name


def test_list_prints_every_command_with_its_script_and_exits_zero(capsys):
    assert cli.main(["--list"]) == 0
    out = capsys.readouterr().out
    for command in cli.COMMANDS:
        assert command.name in out and f"scripts/{command.script}" in out


def test_unknown_command_is_refused_with_the_list(capsys):
    assert cli.main(["no-such-command"]) == 2
    err = capsys.readouterr().err
    assert "unknown command" in err and "refresh-ba" in err


def test_real_script_arguments_pass_through_untouched(capsys):
    assert cli.main(["refresh-ba", "--help"]) == 0  # argparse's own help, exit 0
    assert "--database" in capsys.readouterr().out
    assert cli.main(["candidate-config", "show", "--help"]) == 0


# `check-quality` runs ruff over the whole tree and takes no options; every other script
# is a real argparse program, so `--help` proves that the registered entry point is the
# command line (profile-transfer's `run()` takes a Namespace and used to crash here).
_HELP_COMMANDS = [c.name for c in cli.COMMANDS if c.name != "check-quality"]


@pytest.mark.parametrize("name", _HELP_COMMANDS)
def test_every_registered_command_line_prints_its_help(name, capsys):
    assert cli.main([name, "--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_a_missing_scripts_directory_is_reported_not_crashed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "SCRIPTS_DIR", tmp_path)
    assert cli.main(["refresh-ba"]) == 2
    assert "REFUSED" in capsys.readouterr().err


@pytest.fixture()
def fake_scripts(tmp_path, monkeypatch):
    sources = {
        "run_argv.py": "def run(argv=None):\n    print('got', argv)\n    return 3\n",
        "main_argv.py": (
            "import sys\ndef main():\n    print('sys.argv', sys.argv[1:])\n    sys.exit(4)\n"
        ),
        "returns_none.py": "def run(argv=None):\n    return None\n",
        "exits_text.py": "def run(argv=None):\n    raise SystemExit('refused')\n",
        "main_with_argv.py": "def main(argv=None):\n    return 5 if argv == ['x'] else 6\n",
    }
    for name, text in sources.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(cli, "SCRIPTS_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("script", "argv", "expected"),
    [
        ("run_argv.py", ["a", "--b"], 3),
        ("main_argv.py", ["p", "q"], 4),
        ("returns_none.py", [], 0),
        ("exits_text.py", [], 1),
        ("main_with_argv.py", ["x"], 5),
    ],
)
def test_dispatch_forwards_argv_and_maps_exit_codes(fake_scripts, capsys, script, argv, expected):
    assert cli.dispatch(cli.Command("t", script, ""), argv) == expected


def test_a_textual_system_exit_keeps_its_diagnostic(fake_scripts, capsys):
    assert cli.dispatch(cli.Command("t", "exits_text.py", ""), []) == 1
    assert "refused" in capsys.readouterr().err


def test_an_explicit_entry_name_overrides_run_then_main(fake_scripts):
    (fake_scripts / "two_entries.py").write_text(
        "def run(namespace):\n    raise AssertionError('wrong entry')\n"
        "def main(argv=None):\n    return 7\n",
        encoding="utf-8",
    )
    assert cli.dispatch(cli.Command("t", "two_entries.py", "", entry="main"), []) == 7


def test_argv_is_forwarded_to_entry_points_that_read_sys_argv(fake_scripts, capsys):
    cli.dispatch(cli.Command("t", "main_argv.py", ""), ["p", "q"])
    assert "sys.argv ['p', 'q']" in capsys.readouterr().out
    assert sys.argv[0] != "main_argv.py"  # restored afterwards


def test_module_entry_and_the_old_script_paths_both_work():
    listed = subprocess.run(
        [sys.executable, "-I", "-m", "app.cli", "--list"], cwd=ROOT, capture_output=True, text=True
    )
    assert listed.returncode == 0 and "refresh-ba" in listed.stdout
    old = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stage17_candidate_config.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert old.returncode == 0 and "usage" in old.stdout.lower()
