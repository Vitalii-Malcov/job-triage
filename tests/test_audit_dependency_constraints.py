"""Audit DEP-01: constraints.txt pins every declared dependency to a version its bounds allow."""

import re
import tomllib
from collections import Counter
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    names: list[str] = []
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        assert version, f"constraints.txt must pin exactly (==): {line!r}"
        names.append(str(canonicalize_name(name)))
        pins[str(canonicalize_name(name))] = version
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    assert not duplicates, f"duplicate pins: {duplicates}"
    return pins


def _declared() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    return [Requirement(text) for text in project["dependencies"] + extras["postgres"]]


def test_every_runtime_dependency_is_pinned_exactly():
    pins = _pins()
    missing = [
        str(canonicalize_name(r.name)) for r in _declared() if canonicalize_name(r.name) not in pins
    ]
    assert not missing, f"not pinned in constraints.txt: {missing}"
    assert all(re.fullmatch(r"\d+(\.\d+)*", version) for version in pins.values())


def test_each_pin_satisfies_the_declared_specifier():
    pins = _pins()
    for requirement in _declared():
        pinned = pins[str(canonicalize_name(requirement.name))]
        assert requirement.specifier.contains(pinned, prereleases=True), (requirement.name, pinned)


def test_the_important_transitives_are_pinned_too():
    pins = _pins()
    for name in (
        "starlette",
        "anyio",
        "h11",
        "httpcore",
        "pydantic-core",
        "psycopg-binary",
        "mako",
    ):
        assert name in pins, name


def test_dockerfile_installs_with_the_constraints_file():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY pyproject\.toml constraints\.txt", dockerfile, re.MULTILINE)
    assert "pip install -c constraints.txt" in dockerfile


def test_every_ci_dependency_install_uses_the_constraints_file():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = [line for line in workflow.splitlines() if "pip install" in line]
    project_installs = [line for line in installs if ".[" in line]
    assert project_installs
    assert all("-c constraints.txt" in line for line in project_installs)


def test_docker_build_context_keeps_the_constraints_file():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "constraints.txt" not in [line.strip() for line in ignored]
