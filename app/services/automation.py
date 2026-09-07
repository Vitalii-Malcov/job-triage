"""Stage 8A orchestrator: one persisted, deterministic job-search cycle
per account, coordinating EXISTING collector/scoring components rather
than reimplementing any of their logic.

**Reuse, not duplication.** `run_automation_cycle` calls
`app.api.routes._run_bundesagentur`/`_run_xing` — the SAME two functions
already shared between `POST /collectors/{bundesagentur,xing}/run` and
the Telegram control center's `/run bundesagentur`/`/run xing` commands
(see those functions' own docstrings). Each already does fetch + score
(`app.services.email_matching`-adjacent skill extraction, `JobScorer`) +
persist (`app.db.repositories.upsert_job`/`get_job_by_fingerprint`,
dedup by fingerprint) + best-effort Telegram notification for
APPLY-recommended jobs — this module adds ZERO new
collection/scoring/dedup logic of its own, only run-level bookkeeping
(status, timing, per-step results) around calls to that existing code.
The import of `_run_bundesagentur`/`_run_xing` is deliberately deferred
into the function body below (not a module-level import) to avoid a
circular import: `app.api.routes` imports THIS module for the new
`/automation/runs` endpoints, so this module cannot import FROM
`app.api.routes` at module load time — exactly the same shape of
constraint `app.services.telegram_bot` already has with its own
module-level import of those two functions, except telegram_bot.py is
never imported BY routes.py, so it doesn't need to defer.

**No new side effects.** This module never sends email, never submits
an application, and never talks to Telegram directly — any Telegram
notification that happens is entirely `_run_bundesagentur`/`_run_xing`'s
own EXISTING, unchanged behavior (gated by their own settings, e.g.
`min_job_score_to_notify`). No scheduler, no background task, no retry
loop: `POST /automation/runs` awaits exactly one synchronous cycle and
returns once it's done.

**Per-step isolation (spec: "one collector failure must not corrupt the
run or erase successful results from another collector").** Each step
runs in its own try/except; `_run_bundesagentur`/`_run_xing` themselves
already isolate PER-JOB failures internally (their own per-job
try/except + `db.rollback()`, so one bad job never loses others already
committed in the same call) — the try/except here is one level up,
isolating a COLLECTOR-LEVEL failure (not configured, or the whole
upstream fetch failing) so it can never affect the OTHER collector's
already-committed results or abort the run early. A failing step is
recorded in its own `AutomationRunStepResult` and the run continues to
the next step regardless.

**Concurrency (fail-closed).** `app.db.automation_repository.create_running_run`
is a DB-enforced INSERT-only claim (partial unique index — see
`AutomationRunRecord`'s docstring); a concurrent duplicate run request
for the SAME account_key while one is already RUNNING is refused
immediately (`AutomationRunAlreadyInProgressError`, mapped to 409 by the
API layer) rather than queued, retried, or silently merged.
"""

import logging

from sqlalchemy.orm import Session

from app.db.automation_repository import create_running_run, finish_run
from app.db.models import AutomationRunRecord

logger = logging.getLogger(__name__)

__all__ = [
    "AUTOMATION_STEPS",
    "AutomationRunAlreadyInProgressError",
    "run_automation_cycle",
]

# Stage 8A's fixed, ordered step list — one entry per existing collector
# this orchestrator coordinates. Adding a new source later means adding
# one more (step_name, async_callable) pair here, never touching the
# run/status bookkeeping logic below.
AUTOMATION_STEPS: tuple[str, ...] = ("bundesagentur", "xing")


class AutomationRunAlreadyInProgressError(Exception):
    """A RUNNING automation run already exists for this `account_key` —
    mapped to 409. The new request is refused outright (fail-closed),
    never queued or silently merged into the in-progress run.
    """


async def _run_step(db: Session, step_name: str, step_callable, settings) -> dict:
    """Run exactly one coordinated step, translating its outcome into an
    `AutomationRunStepResult`-shaped dict — never lets an exception
    escape to the caller, so one step's failure can never prevent the
    next step from running or corrupt/roll back a PREVIOUS step's
    already-committed results (those were already committed by the
    step's own internal per-job transaction handling before it returned
    or raised).
    """
    # Deferred import — see this module's own docstring for why.
    from app.api.routes import CollectorError, CollectorNotConfiguredError

    try:
        counters = await step_callable(db, settings)
    except CollectorNotConfiguredError as exc:
        db.rollback()
        logger.info("automation_run_step_not_configured step=%s", step_name)
        return {"status": "not_configured", "counters": None, "error_type": type(exc).__name__}
    except CollectorError as exc:
        db.rollback()
        logger.warning(
            "automation_run_step_failed step=%s error_type=%s", step_name, type(exc).__name__
        )
        return {"status": "failed", "counters": None, "error_type": type(exc).__name__}
    except Exception as exc:
        # Isolates any TRULY unexpected failure (a bug, not a documented
        # CollectorError) the exact same way — one step's crash must
        # never corrupt the run or abort remaining steps. GMAIL-003-style
        # sanitization: only type(exc).__name__ is ever recorded, never
        # str(exc).
        db.rollback()
        logger.exception("automation_run_step_unexpected_error step=%s", step_name)
        return {"status": "failed", "counters": None, "error_type": type(exc).__name__}
    return {"status": "ok", "counters": counters, "error_type": None}


def _compute_overall_status(step_results: dict[str, dict]) -> str:
    ok_count = sum(1 for result in step_results.values() if result["status"] == "ok")
    if ok_count == len(step_results):
        return "COMPLETED"
    if ok_count == 0:
        return "FAILED"
    return "PARTIAL"


def _build_error_summary(step_results: dict[str, dict]) -> str | None:
    parts = [
        f"{step_name}: {result['status']}"
        + (f" ({result['error_type']})" if result["error_type"] else "")
        for step_name, result in step_results.items()
        if result["status"] != "ok"
    ]
    return "; ".join(parts) if parts else None


async def run_automation_cycle(db: Session, *, account_key: str, settings) -> AutomationRunRecord:
    """Coordinate exactly one job-search cycle for `account_key`:
    Bundesagentur, then XING (Stage 8A's fixed `AUTOMATION_STEPS` order —
    sequential, not parallel, since both share the SAME `db` Session and
    SQLAlchemy Sessions are not safe for concurrent use). Raises
    `AutomationRunAlreadyInProgressError` if a RUNNING run already exists
    for this account; otherwise always returns a finished
    (`COMPLETED`/`PARTIAL`/`FAILED`) `AutomationRunRecord`, never raises
    for an individual step's failure (see module docstring's "per-step
    isolation").
    """
    # Deferred import — see this module's own docstring for why.
    from app.api.routes import _run_bundesagentur, _run_xing

    step_callables = {"bundesagentur": _run_bundesagentur, "xing": _run_xing}

    run, created = create_running_run(db, account_key=account_key)
    if not created:
        raise AutomationRunAlreadyInProgressError(
            f"An automation run for account_key={account_key!r} is already RUNNING (id={run.id!r})"
        )

    step_results: dict[str, dict] = {}
    for step_name in AUTOMATION_STEPS:
        step_results[step_name] = await _run_step(
            db, step_name, step_callables[step_name], settings
        )

    overall_status = _compute_overall_status(step_results)
    error_summary = _build_error_summary(step_results)
    logger.info(
        "automation_run_finished run_id=%s account_key=%s status=%s",
        run.id,
        account_key,
        overall_status,
    )
    return finish_run(
        db, run, status=overall_status, results=step_results, error_summary=error_summary
    )
