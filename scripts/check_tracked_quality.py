"""Ruff lint + format check over exactly the files git tracks.

`ruff check .` / `ruff format --check .` also walk untracked local
artifacts (review notes, scratch directories some of which this machine
cannot even open). The merge candidate is the tracked tree, so this gate
asks git for the tracked file list and hands it to ruff with
`--force-exclude` (the pyproject excludes still apply). Windows-safe: no
shell, no xargs; paths are passed in chunks to stay under the command-line
length limit.

    python scripts/check_tracked_quality.py

Exit 0 only when both checks pass for every tracked file.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNK = 100


def tracked(*patterns: str) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return sorted(p for p in output.decode("utf-8").split("\0") if p)


def ruff(args: list[str], files: list[str]) -> int:
    worst = 0
    for start in range(0, len(files), CHUNK):
        chunk = files[start : start + CHUNK]
        result = subprocess.run(
            [sys.executable, "-m", "ruff", *args, "--force-exclude", *chunk], cwd=ROOT
        )
        worst = max(worst, result.returncode)
    return worst


def main() -> int:
    sources = tracked("*.py", "*.pyi", "*.ipynb")
    lint = ruff(["check"], sources)
    fmt = ruff(["format", "--check"], tracked("*.py", "*.pyi", "*.ipynb", "*.md"))
    print(f"tracked-source gate: files={len(sources)} lint_rc={lint} format_rc={fmt}")
    return 1 if lint or fmt else 0


if __name__ == "__main__":
    sys.exit(main())
