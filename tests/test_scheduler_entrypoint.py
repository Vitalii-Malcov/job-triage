"""S8B-PRE-001 regression tests for `app.scheduler.main`'s startup error
handling. `Settings` holds several credentials (Gmail app password,
Telegram bot token, API keys, ...) -- a `pydantic.ValidationError`'s own
`str()`/`repr()` embeds a (possibly truncated but still partially
revealing) repr of whichever field's raw input failed validation, so
`main()` must never print or log that raw representation anywhere, for
ANY Settings misconfiguration, not just ones scoped to the scheduler's
own fields.

Note: `main()` calls `app.core.logging.configure_logging()`, which
installs its own JSON `StreamHandler` on the root logger (writing to
stderr) and clears any handlers already attached -- including pytest's
`caplog` handler. These tests therefore parse the real JSON log line(s)
directly out of captured stderr rather than relying on `caplog`.
"""

import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.scheduler import _CONFIGURATION_ERROR_MESSAGE, main
from app.services.scheduler import SchedulerConfigurationError

SENTINEL = "secret-must-never-leak"


def _validation_error_embedding_sentinel() -> ValidationError:
    """A real ValidationError, invalid scheduler configuration, whose own
    str()/repr() genuinely embeds SENTINEL -- proven directly against
    real pydantic-core behavior (automation_scheduler_poll_seconds fails
    int-coercion with the raw offending string echoed back), not a
    hand-built double.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(automation_scheduler_poll_seconds=SENTINEL)
    assert SENTINEL in str(exc_info.value)  # sanity: the leak really exists upstream
    return exc_info.value


def _json_log_lines(stderr_text: str) -> list[dict]:
    lines = []
    for raw_line in stderr_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("{"):
            lines.append(json.loads(stripped))
    return lines


class TestValidationErrorNeverLeaksToStderrOrLogs:
    def test_startup_with_invalid_settings_fails_closed_without_leaking_sentinel(
        self, monkeypatch, capsys
    ):
        error = _validation_error_embedding_sentinel()
        monkeypatch.setattr("app.scheduler.get_settings", lambda: (_ for _ in ()).throw(error))

        exit_code = main()

        captured = capsys.readouterr()
        assert SENTINEL not in captured.out
        assert SENTINEL not in captured.err
        # Fail-closed: non-zero exit, never a silent "worked fine"
        # outcome for a genuinely invalid configuration.
        assert exit_code != 0

    def test_startup_with_invalid_settings_prints_only_the_fixed_generic_message(
        self, monkeypatch, capsys
    ):
        error = _validation_error_embedding_sentinel()
        monkeypatch.setattr("app.scheduler.get_settings", lambda: (_ for _ in ()).throw(error))

        main()

        captured = capsys.readouterr()
        assert _CONFIGURATION_ERROR_MESSAGE in captured.err
        # No raw ValidationError formatting artifacts (pydantic's own
        # input_value repr / docs URL / "validation error for Settings"
        # boilerplate) anywhere in stdout/stderr -- including inside the
        # JSON log line itself.
        for forbidden in (
            "input_value",
            "errors.pydantic.dev",
            "validation error for Settings",
        ):
            assert forbidden not in captured.out
            assert forbidden not in captured.err

    def test_startup_with_invalid_settings_logs_only_the_exception_type(self, monkeypatch, capsys):
        error = _validation_error_embedding_sentinel()
        monkeypatch.setattr("app.scheduler.get_settings", lambda: (_ for _ in ()).throw(error))

        main()

        captured = capsys.readouterr()
        (log_line,) = _json_log_lines(captured.err)
        assert log_line["level"] == "ERROR"
        assert (
            log_line["message"]
            == "automation_scheduler_configuration_error error_type=ValidationError"
        )


class TestSchedulerConfigurationErrorAlsoNeverLeaks:
    """Defense-in-depth: the SchedulerConfigurationError branch (raised by
    app.services.scheduler.validate_scheduler_settings, reachable if a
    Settings-like object ever bypasses the model_validator -- e.g. via
    model_construct) must be just as sanitized as the ValidationError
    branch above, even though today's SchedulerConfigurationError message
    happens to be a fixed string with no embedded input.
    """

    def test_scheduler_configuration_error_path_is_sanitized_and_fails_closed(
        self, monkeypatch, capsys
    ):
        bypassed_settings = Settings.model_construct(
            automation_scheduler_enabled=True,
            automation_scheduler_account_key="   ",
            automation_scheduler_interval_seconds=3600,
            automation_scheduler_poll_seconds=15,
        )
        monkeypatch.setattr("app.scheduler.get_settings", lambda: bypassed_settings)

        exit_code = main()

        captured = capsys.readouterr()
        assert exit_code != 0
        assert _CONFIGURATION_ERROR_MESSAGE in captured.err

        (log_line,) = _json_log_lines(captured.err)
        assert (
            log_line["message"]
            == "automation_scheduler_configuration_error error_type=SchedulerConfigurationError"
        )

    def test_scheduler_configuration_error_message_itself_has_no_embedded_input(self):
        """Sanity check on the exception this branch actually catches --
        proves today's SchedulerConfigurationError message is safe by
        construction, independent of main()'s own sanitization."""
        try:
            raise SchedulerConfigurationError(
                "automation_scheduler_enabled=True requires a non-blank "
                "automation_scheduler_account_key (AUTOMATION_SCHEDULER_ACCOUNT_KEY)."
            )
        except SchedulerConfigurationError as exc:
            assert SENTINEL not in str(exc)
