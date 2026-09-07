"""Pydantic DTOs for Stage 8A automation runs.

`AutomationRun` is the read-facing shape of `app.db.models.AutomationRunRecord`
— see that model's docstring for the full status/concurrency contract.
`AutomationRunStepResult` is one entry of `AutomationRun.results`, keyed by
step name (currently `"bundesagentur"`/`"xing"` — see
app.services.automation.AUTOMATION_STEPS); `counters` is passed through
UNCHANGED from the step's own existing return shape
(`app.api.routes._run_bundesagentur`/`_run_xing`, both already
`dict[str, int]`), never re-derived here.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

AutomationRunStatus = Literal["RUNNING", "COMPLETED", "PARTIAL", "FAILED"]
AutomationStepStatus = Literal["ok", "not_configured", "failed"]


class AutomationRunStepResult(BaseModel):
    """One coordinated step's outcome within a run. `error_type` is only
    ever `type(exc).__name__` (see app.services.automation's module
    docstring) — never the exception's own message text, which could
    carry back sensitive/upstream-echoed detail (mirrors this project's
    GMAIL-003 convention).
    """

    status: AutomationStepStatus
    counters: dict[str, int] | None = None
    error_type: str | None = None


class AutomationRun(BaseModel):
    """GET /automation/runs/{id} and the elements of
    GET /automation/runs — one persisted orchestration run."""

    id: int
    account_key: str
    status: AutomationRunStatus
    started_at: datetime
    finished_at: datetime | None
    results: dict[str, AutomationRunStepResult]
    error_summary: str | None
    created_at: datetime
