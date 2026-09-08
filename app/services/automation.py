"""Stage 8A orchestrator: one persisted, deterministic job-search cycle
per account, coordinating EXISTING collector/scoring components rather
than reimplementing any of their logic.

**Reuse, not duplication (S8A-003, Codex re-review, layering fix).**
`run_automation_cycle` calls `app.services.collector_runner.run_bundesagentur`/
`run_xing` — the SAME two functions shared with
`POST /collectors/{bundesagentur,xing}/run` and the Telegram control
center's `/run bundesagentur`/`/run xing` commands (see that module's own
docstring). Each already does fetch + score (skill extraction,
`JobScorer`) + persist (`app.db.repositories.upsert_job`/
`get_job_by_fingerprint`, dedup by fingerprint) + best-effort Telegram
notification for APPLY-recommended jobs — this module adds ZERO new
collection/scoring/dedup logic of its own, only run-level bookkeeping
(status, timing, per-step results) around calls to that existing code.
`app.services.collector_runner` is a plain module-level import here —
unlike an earlier version of this module, which had to defer importing
these two functions from `app.api.routes` into the function body to
avoid a circular import (routes.py imported this module for the
`/automation/runs` endpoints). Moving the shared logic to
`app.services.collector_runner` — which never imports `app.api.routes` —
removes that circularity entirely: `app.services` code must never import
`app.api.routes` (routes.py depends on services, never the reverse).

**No new side effects.** This module never sends email, never submits
an application, and never talks to Telegram directly — any Telegram
notification that happens is entirely `run_bundesagentur`/`run_xing`'s
own EXISTING, unchanged behavior (gated by their own settings, e.g.
`min_job_score_to_notify`). No scheduler, no background task, no retry
loop: `POST /automation/runs` awaits exactly one synchronous cycle and
returns once it's done.

**Per-step isolation (spec: "one collector failure must not corrupt the
run or erase successful results from another collector").** Each step
runs in its own try/except; `run_bundesagentur`/`run_xing` themselves
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
for the SAME account_key while one is already RUNNING (a LIVE lease) is
refused immediately (`AutomationRunAlreadyInProgressError`, mapped to
409 by the API layer) rather than queued, retried, or silently merged.

**Crash recovery via an ownership-aware lease (S8A-002, Codex
re-review).** A `RUNNING` row alone cannot distinguish "a run is
genuinely still executing" from "a process crashed while one was
executing" — before this fix, the latter would block that account's
automation forever. `create_running_run` now also claims a
`lease_holder`/`lease_expires_at` pair (mirrors Stage 7E's
`GmailThreadRecord` lock exactly — see
`app.db.gmail_repository`/`app.db.automation_repository`'s own
docstrings): a LIVE lease still fails closed exactly as before; an
EXPIRED one is atomically reconciled to `FAILED` before a fresh run is
claimed. `_RunLeaseHeartbeat` (below) renews the lease periodically for
as long as `run_automation_cycle` is actually executing, via the
DEDICATED `renew_run_lease` CAS — never `create_running_run`'s own claim
path, which would wrongly treat an already-expired lease as
free-for-the-taking during a renewal (see `renew_run_lease`'s
docstring). If the lease is ever lost while the run is still executing
(the heartbeat's renewal failed), `run_automation_cycle` fails closed:
it raises `AutomationRunLeaseLostError` instead of finalizing the run's
status, so it can never overwrite whatever a NEW owner has since done
with that row.

**Stage 8C — optional shortlist + CV/Bewerbung draft preparation.** After
the fixed `AUTOMATION_STEPS` collector loop finishes, if
`settings.automation_auto_prepare_enabled` is on (off by default),
`app.services.automation_shortlist.prepare_shortlist_drafts` runs as one
more step (`"shortlist_drafts"`) inside this SAME try/finally — no second
lease, no second orchestration loop; Stage 8A's existing heartbeat
already covers it for as long as it takes. It reuses the SAME
match/CV/Bewerbung service logic the manual endpoints call
(`app.services.candidate_preparation`), creates drafts only (never sends,
approves, or transitions a `JobRecord`'s status), and — like every other
step here — can never itself abort the run or corrupt another step's
results; see that module's own docstring for the full policy.
"""

import logging
import threading

from sqlalchemy.orm import Session, sessionmaker

from app.db.automation_repository import (
    AUTOMATION_RUN_LEASE_TTL_SECONDS,
    create_running_run,
    finish_run,
    new_run_lease_holder_token,
    renew_run_lease,
)
from app.db.models import AutomationRunRecord
from app.services.automation_shortlist import prepare_shortlist_drafts
from app.services.collector_runner import (
    CollectorError,
    CollectorNotConfiguredError,
    run_bundesagentur,
    run_xing,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AUTOMATION_STEPS",
    "AutomationRunAlreadyInProgressError",
    "AutomationRunLeaseLostError",
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


class AutomationRunLeaseLostError(Exception):
    """S8A-002 (Codex re-review): ownership of the run's lease was lost
    while `run_automation_cycle` was still executing (or in the instant
    right before its final write) — mapped to 409. The collector steps
    themselves already ran to completion and their real results (jobs
    fetched/scored/persisted) are unaffected — this error means only
    that THIS run's own bookkeeping row could not be safely finalized,
    because some other process may now own it. Never automatically
    retried; a fresh `POST /automation/runs` starts a new run.
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
        # never corrupt the run or abort remaining steps.
        #
        # S8A-004 (Codex re-review, sanitized logging): deliberately
        # `logger.warning(...)`, NEVER `logger.exception(...)` — the
        # latter attaches `exc_info=True`, which logs the full traceback
        # INCLUDING the exception's own str(exc) message (its final
        # "ExceptionType: message" line and any chained/nested exception
        # text) — exactly the raw, potentially upstream/data-bearing
        # detail this project's GMAIL-003 convention (see
        # app/providers/email/base.py's GmailProviderError docstring)
        # forbids logging or persisting. Only `step_name` and
        # `type(exc).__name__` are ever recorded, matching the
        # CollectorNotConfiguredError/CollectorError branches above and
        # `AutomationRunStepResult.error_type`'s own contract.
        db.rollback()
        logger.warning(
            "automation_run_step_unexpected_error step=%s error_type=%s",
            step_name,
            type(exc).__name__,
        )
        return {"status": "failed", "counters": None, "error_type": type(exc).__name__}
    return {"status": "ok", "counters": counters, "error_type": None}


async def _run_shortlist_drafts_step(db: Session, settings, run_started_at) -> dict:
    """Stage 8C post-processing step — mirrors `_run_step`'s outer safety
    net (never lets an exception escape, so this step can never abort the
    run or block finalizing it) for a genuinely unexpected, STEP-level
    failure (e.g. the candidate-pool query itself raising). Per-JOB
    failures within the step are already isolated and reported inside
    its own returned `items`/`counters["failed"]` —
    `app.services.automation_shortlist.prepare_shortlist_drafts` never
    lets a single job's exception propagate this far.
    """
    try:
        return await prepare_shortlist_drafts(db, run_started_at=run_started_at, settings=settings)
    except Exception as exc:
        db.rollback()
        logger.warning(
            "automation_run_step_unexpected_error step=shortlist_drafts error_type=%s",
            type(exc).__name__,
        )
        return {
            "status": "failed",
            "counters": None,
            "items": None,
            "error_type": type(exc).__name__,
        }


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


class _RunLeaseHeartbeat:
    """S8A-002 (Codex re-review): periodically renews an
    `AutomationRunRecord`'s lease on `holder`'s behalf for as long as
    `run_automation_cycle` is executing — mirrors
    `app.services.follow_up_send._ThreadLockHeartbeat` almost exactly
    (see that class's own docstring for the full rationale, including
    why it runs on its OWN `Session`, in its OWN daemon thread).

    `lease_lost` (a `threading.Event`) is set the moment a renewal
    attempt fails — either because
    `app.db.automation_repository.renew_run_lease` reports the lease was
    no longer live for `holder` at renewal time, OR because the renewal
    attempt itself raised (e.g. a DB error) — and the heartbeat stops
    trying immediately afterward in both cases. The caller MUST check
    `lease_lost` before trusting that it may still finalize this run's
    outcome.

    An exception from `renew_run_lease` must never be allowed to just
    kill this thread silently while `run_automation_cycle` carries on
    assuming it still owns the lease — that would defeat the entire
    point of the heartbeat. Any such exception is therefore treated
    exactly like a failed renewal (`lease_lost` set, thread stops), and
    is logged sanitized only — step/event name and
    `type(exc).__name__` — never the raw exception text or a traceback,
    since it may echo upstream/data-bearing detail (S8A-004's same
    sanitized-logging convention).
    """

    def __init__(
        self,
        db: Session,
        run_id: int,
        *,
        holder: str,
        ttl_seconds: float,
        interval_seconds: float,
    ) -> None:
        self._session_factory = sessionmaker(bind=db.get_bind())
        self._run_id = run_id
        self._holder = holder
        self._ttl_seconds = ttl_seconds
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self.lease_lost = threading.Event()
        self._thread = threading.Thread(target=self._run, name="automation-run-lease-heartbeat")
        self._thread.daemon = True

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        session = self._session_factory()
        try:
            while not self._stop_event.wait(self._interval_seconds):
                try:
                    renewed = renew_run_lease(
                        session, self._run_id, holder=self._holder, ttl_seconds=self._ttl_seconds
                    )
                except Exception as exc:
                    session.rollback()
                    logger.warning(
                        "automation_run_lease_heartbeat_renewal_error run_id=%s error_type=%s",
                        self._run_id,
                        type(exc).__name__,
                    )
                    self.lease_lost.set()
                    return
                if not renewed:
                    logger.warning(
                        "automation_run_lease_heartbeat_lost_ownership run_id=%s",
                        self._run_id,
                    )
                    self.lease_lost.set()
                    return
        finally:
            session.close()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self._interval_seconds + 1.0)


async def run_automation_cycle(
    db: Session,
    *,
    account_key: str,
    settings,
    lease_ttl_seconds: float = AUTOMATION_RUN_LEASE_TTL_SECONDS,
    heartbeat_interval_seconds: float | None = None,
) -> AutomationRunRecord:
    """Coordinate exactly one job-search cycle for `account_key`:
    Bundesagentur, then XING (Stage 8A's fixed `AUTOMATION_STEPS` order —
    sequential, not parallel, since both share the SAME `db` Session and
    SQLAlchemy Sessions are not safe for concurrent use). Raises
    `AutomationRunAlreadyInProgressError` if a LIVE run already exists
    for this account, or `AutomationRunLeaseLostError` if ownership of
    this run's own lease was lost before its outcome could be safely
    recorded (see module docstring's "Crash recovery via an
    ownership-aware lease"); otherwise always returns a finished
    (`COMPLETED`/`PARTIAL`/`FAILED`) `AutomationRunRecord`, never raises
    for an individual step's failure (see "per-step isolation").
    `lease_ttl_seconds`/`heartbeat_interval_seconds` default to the safe
    production values — overridable only so tests can exercise the
    lease-expiry boundary quickly, exactly like
    `app.services.follow_up_send.send_follow_up`'s identical parameters.
    """
    step_callables = {"bundesagentur": run_bundesagentur, "xing": run_xing}

    holder = new_run_lease_holder_token(f"automation_run:{account_key}")
    run, created = create_running_run(
        db, account_key=account_key, holder=holder, ttl_seconds=lease_ttl_seconds
    )
    if not created:
        raise AutomationRunAlreadyInProgressError(
            f"An automation run for account_key={account_key!r} is already RUNNING (id={run.id!r})"
        )

    effective_heartbeat_interval = (
        heartbeat_interval_seconds
        if heartbeat_interval_seconds is not None
        else lease_ttl_seconds / 3
    )
    heartbeat = _RunLeaseHeartbeat(
        db,
        run.id,
        holder=holder,
        ttl_seconds=lease_ttl_seconds,
        interval_seconds=effective_heartbeat_interval,
    )
    heartbeat.start()
    try:
        step_results: dict[str, dict] = {}
        for step_name in AUTOMATION_STEPS:
            step_results[step_name] = await _run_step(
                db, step_name, step_callables[step_name], settings
            )

        # Stage 8C: opt-in only (Settings.automation_auto_prepare_enabled
        # defaults to False, independent of the scheduler being enabled —
        # see app.core.config.Settings). When disabled, AutomationRun.results
        # stays EXACTLY the Stage 8A/8B collector result shape — no fake
        # disabled/skipped step is ever added.
        if settings.automation_auto_prepare_enabled:
            step_results["shortlist_drafts"] = await _run_shortlist_drafts_step(
                db, settings, run.started_at
            )

        overall_status = _compute_overall_status(step_results)
        error_summary = _build_error_summary(step_results)

        if heartbeat.lease_lost.is_set():
            # S8A-002: exclusivity for this run's own bookkeeping is no
            # longer provable — fail closed rather than risk overwriting
            # whatever a new owner has since recorded for this row.
            raise AutomationRunLeaseLostError(
                f"Lost ownership of automation run id={run.id!r} for account_key="
                f"{account_key!r} while it was executing; its outcome could not be "
                "safely finalized."
            )

        finished = finish_run(
            db,
            run,
            holder=holder,
            status=overall_status,
            results=step_results,
            error_summary=error_summary,
        )
        if finished is None:
            # Lost the lease in the instant between the check above and
            # this write — the same fail-closed contract, just caught at
            # the last possible moment instead of slightly earlier.
            raise AutomationRunLeaseLostError(
                f"Lost ownership of automation run id={run.id!r} for account_key="
                f"{account_key!r} immediately before its outcome could be recorded; "
                "another process may now own it."
            )

        logger.info(
            "automation_run_finished run_id=%s account_key=%s status=%s",
            run.id,
            account_key,
            overall_status,
        )
        return finished
    finally:
        heartbeat.stop()
