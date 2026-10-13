"""Stage 13A: static guards on the deployment artifacts, so a fail-closed
deployment default cannot be weakened without a failing test.

- compose pins OUTBOUND_SENDING_ENABLED, TELEGRAM_GMAIL_DRAFT_ENABLED and
  ALEMBIC_AUTO_UPGRADE to "false" in every app service (web + scheduler);
- the web port is published on loopback only, and PostgreSQL is never
  published;
- the web healthcheck targets /api/v1/ready (not the DB-free /health);
- the container runs one Uvicorn worker with proxy-header rewriting off;
- `.env` and every `.env.*` variant stay out of git and the Docker build
  context, while the committed templates stay trackable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_SERVICES = ("web", "scheduler")
PINNED_FALSE = ("OUTBOUND_SENDING_ENABLED", "TELEGRAM_GMAIL_DRAFT_ENABLED", "ALEMBIC_AUTO_UPGRADE")
ENV_TEMPLATES = (".env.example", ".env.stage10.example")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dockerfile_cmd() -> list[str]:
    import json

    lines = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    (cmd,) = [line for line in lines if line.startswith("CMD ")]
    return json.loads(cmd[len("CMD ") :])


@pytest.mark.parametrize("service", APP_SERVICES)
@pytest.mark.parametrize("flag", PINNED_FALSE)
def test_safety_flags_are_pinned_false_in_every_app_service(compose, service, flag):
    environment = compose["services"][service]["environment"]
    assert environment[flag] == "false"  # explicit string, overrides env_file: .env


def test_web_port_is_published_on_loopback_only(compose):
    ports = compose["services"]["web"]["ports"]
    assert ports == ["127.0.0.1:${WEB_PORT:-8000}:8000"]


def test_database_is_never_published(compose):
    assert "ports" not in compose["services"]["db"]


def test_web_healthcheck_targets_readiness(compose):
    test = " ".join(compose["services"]["web"]["healthcheck"]["test"])
    assert "/api/v1/ready" in test
    assert "/api/v1/health" not in test


def test_scheduler_stays_opt_in(compose):
    scheduler = compose["services"]["scheduler"]
    assert scheduler["profiles"] == ["scheduler"]
    assert "deploy" not in scheduler  # no replicas override


def test_uvicorn_runs_one_worker_without_proxy_header_rewriting(dockerfile_cmd):
    assert dockerfile_cmd[0] == "uvicorn"
    assert "--no-proxy-headers" in dockerfile_cmd
    assert "--proxy-headers" not in dockerfile_cmd
    assert "--forwarded-allow-ips" not in " ".join(dockerfile_cmd)
    assert dockerfile_cmd[dockerfile_cmd.index("--workers") + 1] == "1"


def test_env_files_are_excluded_from_the_docker_build_context():
    entries = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in entries
    assert ".env.*" in entries


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_env_variants_are_git_ignored_but_templates_stay_trackable():
    def ignored(path: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", path],
            cwd=PROJECT_ROOT,
            check=False,
        )
        return result.returncode == 0

    for path in (".env", ".env.before-stage11", ".env.local", ".env.production"):
        assert ignored(path), path
    for path in ENV_TEMPLATES:
        assert not ignored(path), path
