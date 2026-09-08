"""Pydantic DTOs for Stage 8A/8C automation runs.

`AutomationRun` is the read-facing shape of `app.db.models.AutomationRunRecord`
— see that model's docstring for the full status/concurrency contract.
`AutomationRunStepResult` is one entry of `AutomationRun.results`, keyed by
step name — `"bundesagentur"`/`"xing"` (see
app.services.automation.AUTOMATION_STEPS) plus, when Stage 8C's
`automation_auto_prepare_enabled` is on, `"shortlist_drafts"` (see
app.services.automation_shortlist.prepare_shortlist_drafts). `counters` is
passed through UNCHANGED from the step's own existing return shape
(`app.api.routes._run_bundesagentur`/`_run_xing`, both already
`dict[str, int]`; Stage 8C's own counters — candidate_jobs/matched/
shortlisted/cv_created/cv_reused/bewerbung_created/bewerbung_reused/failed
— are likewise `dict[str, int]`), never re-derived here. `items` is
`None` for every Stage 8A collector step (backwards compatible — existing
`bundesagentur`/`xing` results never populate it) and only ever populated
by the `shortlist_drafts` step.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

AutomationRunStatus = Literal["RUNNING", "COMPLETED", "PARTIAL", "FAILED"]
# "partial" (Stage 8C): the shortlist_drafts step itself had a mix of
# per-job successes and per-job failures — distinct from the overall
# AutomationRun.status PARTIAL (which already existed for Stage 8A
# collector-level mixes); see app.services.automation._compute_overall_status,
# unchanged, which already treats any non-"ok" step status uniformly.
AutomationStepStatus = Literal["ok", "not_configured", "failed", "partial"]


class ShortlistDraftItem(BaseModel):
    """Stage 8C: one shortlisted job's technical draft-preparation outcome
    — persisted in AutomationRunStepResult.items. Technical metadata
    ONLY (spec section 9) — never candidate name, CV/Bewerbung text, job
    description, email, or a raw exception message. `cv_draft_id`/
    `bewerbung_draft_id` are `None` when preparation failed for this job
    before reaching that step; `error_type` is only ever
    `type(exc).__name__`, mirroring `AutomationRunStepResult.error_type`'s
    own sanitized-logging contract.
    """

    job_id: int
    match_id: int
    match_score: int
    cv_draft_id: int | None = None
    bewerbung_draft_id: int | None = None
    cv_reused: bool = False
    bewerbung_reused: bool = False
    status: Literal["ok", "failed"]
    error_type: str | None = None


class AutomationRunStepResult(BaseModel):
    """One coordinated step's outcome within a run. `error_type` is only
    ever `type(exc).__name__` (see app.services.automation's module
    docstring) — never the exception's own message text, which could
    carry back sensitive/upstream-echoed detail (mirrors this project's
    GMAIL-003 convention).
    """

    status: AutomationStepStatus
    counters: dict[str, int] | None = None
    items: list[ShortlistDraftItem] | None = None
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
