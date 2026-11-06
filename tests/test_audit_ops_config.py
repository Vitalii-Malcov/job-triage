"""Audit ARC-03 (scheduler account key), OPS-01 (LF entrypoint), OPS-05/TEST-06 (CI limits)."""

import subprocess
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.services.scheduler import SchedulerConfigurationError, validate_scheduler_settings

ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        automation_scheduler_enabled=True,
        automation_scheduler_account_key="operator@example.com",
        gmail_username="operator@example.com",
        gmail_app_password="x" * 16,
    )
    values.update(overrides)
    return Settings(**values)


def test_matching_keys_pass_even_with_the_gmail_cycle_enabled():
    validate_scheduler_settings(_settings(automation_gmail_cycle_enabled=True))
    validate_scheduler_settings(_settings(automation_follow_up_cycle_enabled=True))


@pytest.mark.parametrize(
    "cycle", ["automation_gmail_cycle_enabled", "automation_follow_up_cycle_enabled"]
)
def test_a_case_only_difference_is_refused_at_startup(cycle):
    settings = _settings(automation_scheduler_account_key="Operator@Example.com", **{cycle: True})
    with pytest.raises(SchedulerConfigurationError) as excinfo:
        validate_scheduler_settings(settings)
    assert "operator@example.com" not in str(excinfo.value).lower().replace(
        "must equal gmail_username", ""
    )
    assert "Example.com" not in str(excinfo.value)  # neither value is echoed


def test_the_check_only_applies_when_a_gmail_step_is_enabled():
    # Case-different key but no Gmail/follow-up cycle: nothing compares the two, so no refusal.
    validate_scheduler_settings(_settings(automation_scheduler_account_key="Operator@Example.com"))


def test_a_different_mailbox_is_refused_too():
    with pytest.raises(SchedulerConfigurationError):
        validate_scheduler_settings(
            _settings(
                automation_scheduler_account_key="someone-else@example.com",
                automation_gmail_cycle_enabled=True,
            )
        )


def test_gitattributes_pins_shell_scripts_to_lf():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes
    assert "docker/entrypoint.sh text eol=lf" in attributes


def test_the_committed_entrypoint_has_no_carriage_returns():
    blob = subprocess.run(
        ["git", "show", "HEAD:docker/entrypoint.sh"], cwd=ROOT, capture_output=True, check=False
    )
    if blob.returncode != 0:  # not a git checkout (sdist): nothing to check
        pytest.skip("not a git checkout")
    assert b"\r" not in blob.stdout


def test_ci_workflow_limits_permissions_and_job_time():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "timeout-minutes" in job, f"job {name} has no timeout"
    steps = " ".join(str(step.get("run", "")) for step in workflow["jobs"]["test"]["steps"])
    assert "--timeout=" in steps and "--cov=app" in steps
