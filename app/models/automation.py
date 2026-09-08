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
    description, email, or a raw exception message.

    **S8C-AUDIT-001 (Codex review): truthful even on partial failure.**
    A job that reached the shortlist always gets ONE item here, whatever
    happens next. If CV preparation itself fails, `cv_draft_id` is `None`
    (no draft exists). If CV preparation SUCCEEDS but the LATER Bewerbung
    step fails, `cv_draft_id`/`cv_reused` still report the real, already
    -durably-committed CV draft — the Bewerbung failure's `db.rollback()`
    only discards Bewerbung's own uncommitted attempt, never the earlier,
    separately-committed CV row, and this item must not lie about that.
    `phase` names WHERE within this job's pipeline a failure happened
    (`None` when `status="ok"`). `error_type` is only ever
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
    phase: Literal["cv", "bewerbung"] | None = None
    error_type: str | None = None


class AutomationJobFailure(BaseModel):
    """S8C-AUDIT-002 (Codex review): a bounded, privacy-safe technical
    failure record — persisted in AutomationRunStepResult.failures so a
    future consumer (Stage 8E's digest) can answer "which job failed, at
    which phase, with which exception TYPE" without parsing logs.
    Distinct from `ShortlistDraftItem`: a `"match"`-phase failure means
    the job never became a shortlist candidate at all (no match_id/score
    to report), so it is recorded HERE, never as a `ShortlistDraftItem`.
    A `"cv"`/`"bewerbung"`-phase failure for an already-shortlisted job
    gets BOTH a `failures` entry (for this uniform, phase-searchable
    view) AND its own `ShortlistDraftItem` (for the job's full draft
    context, per S8C-AUDIT-001 above) — same underlying failure, two
    complementary views, never conflicting. `error_type` only —
    NEVER `str(exc)`/`repr(exc)`/a traceback.
    """

    job_id: int
    phase: Literal["match", "cv", "bewerbung"]
    error_type: str


class AutomationRunStepResult(BaseModel):
    """One coordinated step's outcome within a run. `error_type` is only
    ever `type(exc).__name__` (see app.services.automation's module
    docstring) — never the exception's own message text, which could
    carry back sensitive/upstream-echoed detail (mirrors this project's
    GMAIL-003 convention). `failures` is `None` for every Stage 8A
    collector step and for historical rows persisted before S8C-AUDIT-002
    (backwards compatible — old AutomationRun rows still deserialize);
    the `shortlist_drafts` step always populates it with a (possibly
    empty) list once it runs.
    """

    status: AutomationStepStatus
    counters: dict[str, int] | None = None
    items: list[ShortlistDraftItem] | None = None
    failures: list[AutomationJobFailure] | None = None
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
