"""Pydantic DTOs for Stage 8A/8C/8D automation runs.

`AutomationRun` is the read-facing shape of `app.db.models.AutomationRunRecord`
— see that model's docstring for the full status/concurrency contract.
`AutomationRunStepResult` is one entry of `AutomationRun.results`, keyed by
step name — `"bundesagentur"`/`"xing"` (see
app.services.automation.AUTOMATION_STEPS) plus, when Stage 8C's
`automation_auto_prepare_enabled` is on, `"shortlist_drafts"` (see
app.services.automation_shortlist.prepare_shortlist_drafts), plus, when
Stage 8D's `automation_gmail_cycle_enabled`/`automation_follow_up_cycle_enabled`
are on, `"gmail_sync"`/`"gmail_response_drafts"`/`"follow_up_proposals"`
(see app.services.automation_gmail/app.services.automation_follow_up).
`counters` is passed through UNCHANGED from each step's own existing
return shape (`dict[str, int]` in every case), never re-derived here.
`items`/`failures` are `None` for every Stage 8A collector step
(backwards compatible — existing `bundesagentur`/`xing` results never
populate them) and only ever populated by a Stage 8C/8D step.
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
    """S8C-AUDIT-002 (Codex review), extended by Stage 8D: a bounded,
    privacy-safe technical failure record — persisted in
    AutomationRunStepResult.failures so a future consumer (Stage 8E's
    digest) can answer "which job failed, at which phase, with which
    exception TYPE" without parsing logs. Distinct from
    `ShortlistDraftItem`: a `"match"`-phase failure means the job never
    became a shortlist candidate at all (no match_id/score to report), so
    it is recorded HERE, never as a `ShortlistDraftItem`. A `"cv"`/
    `"bewerbung"`-phase failure for an already-shortlisted job gets BOTH
    a `failures` entry (for this uniform, phase-searchable view) AND its
    own `ShortlistDraftItem` (for the job's full draft context, per
    S8C-AUDIT-001 above) — same underlying failure, two complementary
    views, never conflicting. `"follow_up"` (Stage 8D) is used by
    `app.services.automation_follow_up.prepare_follow_up_proposals` for
    an unexpected `evaluate_follow_up_for_job` exception — distinct from
    the normal `NOT_ELIGIBLE` outcome, which is not a failure at all.
    `error_type` only — NEVER `str(exc)`/`repr(exc)`/a traceback.
    """

    job_id: int
    phase: Literal["match", "cv", "bewerbung", "follow_up"]
    error_type: str


class AutomationMessageItem(BaseModel):
    """Stage 8D: one Gmail message's technical processing outcome —
    persisted in AutomationRunStepResult.items for the
    `gmail_response_drafts` step. Technical metadata ONLY — never
    subject/body/addresses/display names/response content. Mirrors
    `ShortlistDraftItem`'s truthful-on-partial-failure shape
    (S8C-AUDIT-001): if analysis succeeds but response-draft generation
    fails, `analysis_id`/`analysis_created` still report the real,
    already-committed analysis row. `response_status` is the existing
    Stage 7C `ResponseDraftStatus` value (`"PROPOSED"`/
    `"NO_RESPONSE_RECOMMENDED"`) when a draft step completed, else
    `None`.
    """

    gmail_message_id: int
    analysis_id: int | None = None
    response_draft_id: int | None = None
    response_status: str | None = None
    analysis_created: bool = False
    draft_created: bool = False
    status: Literal["ok", "failed"]
    phase: Literal["analysis", "response_draft"] | None = None
    error_type: str | None = None


class AutomationMessageFailure(BaseModel):
    """Stage 8D: a bounded, privacy-safe technical failure record for one
    Gmail message — persisted in AutomationRunStepResult.failures,
    mirroring `AutomationJobFailure`'s shape but keyed by
    `gmail_message_id` instead of `job_id` (a Gmail message is never a
    `JobRecord`). `error_type` only — NEVER `str(exc)`/`repr(exc)`/a
    traceback.
    """

    gmail_message_id: int
    phase: Literal["analysis", "response_draft"]
    error_type: str


class AutomationFollowUpItem(BaseModel):
    """Stage 8D: one job's technical follow-up evaluation outcome —
    persisted in AutomationRunStepResult.items for the
    `follow_up_proposals` step. Technical metadata ONLY — never
    subject/body/recipient/follow-up text. `proposal_id`/
    `proposal_created` are only populated when `eligibility ==
    "ELIGIBLE"` (mirrors `FollowUpEvaluationResult.proposal`/`created`,
    both `None` when NOT_ELIGIBLE — an entirely normal outcome, not a
    failure).
    """

    job_id: int
    eligibility: Literal["ELIGIBLE", "NOT_ELIGIBLE"]
    proposal_id: int | None = None
    proposal_created: bool | None = None
    status: Literal["ok"] = "ok"


class AutomationRunStepResult(BaseModel):
    """One coordinated step's outcome within a run. `error_type` is only
    ever `type(exc).__name__` (see app.services.automation's module
    docstring) — never the exception's own message text, which could
    carry back sensitive/upstream-echoed detail (mirrors this project's
    GMAIL-003 convention). `items`/`failures` are `None` for every Stage
    8A collector step and for historical rows persisted before
    S8C-AUDIT-002/Stage 8D (backwards compatible — old AutomationRun rows
    still deserialize); each Stage 8C/8D step always populates them with
    a (possibly empty) list once it runs. `items`/`failures` accept any
    of the Stage 8C/8D item/failure shapes — pydantic discriminates by
    matching required fields, since `job_id` vs `gmail_message_id` are
    mutually exclusive across the two families.
    """

    status: AutomationStepStatus
    counters: dict[str, int] | None = None
    items: list[ShortlistDraftItem | AutomationMessageItem | AutomationFollowUpItem] | None = None
    failures: list[AutomationJobFailure | AutomationMessageFailure] | None = None
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
