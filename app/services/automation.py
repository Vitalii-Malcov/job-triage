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
from app.services.automation_follow_up import prepare_follow_up_proposals
from app.services.automation_gmail import prepare_gmail_response_drafts, prepare_gmail_sync
from app.services.automation_shortlist import prepare_shortlist_drafts
from app.services.collector_runner import (
    CollectorError,
    CollectorNotConfiguredError,
    TouchedJob,
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
    """S8A-002 (Codex re-review); NEW-004 (Astra R4A) extends WHEN this
    fires. Ownership of the run's lease was lost while
    `run_automation_cycle` was still executing — mapped to 409.

    NEW-004: this is now raised the INSTANT lease loss is observed,
    checked before every step/collector `run_automation_cycle` is about
    to launch (see `_raise_if_lease_lost`), not only once at the very
    end after every step had already run. A confirmed-lost lease means a
    replacement worker may already own this account's run; continuing
    to launch NEW collector calls, external IMAP/HTTP fetches, Telegram
    notifications, or research runs after that point risks genuine
    duplicate/overlapping execution against the same account, not just
    a bookkeeping race. Any step that had ALREADY fully run before loss
    was detected keeps its real results (jobs fetched/scored/persisted,
    messages synced, etc. — all committed independently by that step's
    own code, never rolled back by this error) — only this run's own
    summary bookkeeping row is abandoned, because some other process may
    now own it. Never automatically retried; a fresh `POST
    /automation/runs` starts a new run.
    """


async def _run_step(
    db: Session, step_name: str, step_callable, settings, touched_jobs: list[TouchedJob]
) -> dict:
    """Run exactly one coordinated step, translating its outcome into an
    `AutomationRunStepResult`-shaped dict — never lets an exception
    escape to the caller, so one step's failure can never prevent the
    next step from running or corrupt/roll back a PREVIOUS step's
    already-committed results (those were already committed by the
    step's own internal per-job transaction handling before it returned
    or raised).

    `touched_jobs` (S8C-POOL-001): one shared, run-scoped sink passed to
    every collector step so `run_automation_cycle` can hand Stage 8C the
    exact set of jobs THIS run's own collectors persisted — see
    `app.services.collector_runner.TouchedJob`.
    """
    try:
        counters = await step_callable(db, settings, touched_jobs=touched_jobs)
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

    # AUD-009 (Astra R2): `step_callable` returning normally only means it
    # didn't RAISE — `run_bundesagentur`/`run_xing` isolate per-job
    # failures internally (see their own docstrings) and report them via
    # `counters["failed"]` instead of propagating, so a return here can
    # still mean every single fetched job failed to score/persist. Mirror
    # `app.services.automation_shortlist.prepare_shortlist_drafts`'s own
    # S8C-STATUS-001 convention: the denominator is jobs actually
    # ATTEMPTED (created + updated + failed) — `skipped_invalid` jobs were
    # never attempted, so they must not count as failures, and zero
    # attempted (nothing fetched, or everything was skipped_invalid) is a
    # legitimate no-op "ok", never "failed".
    attempted = counters["created"] + counters["updated"] + counters["failed"]
    if attempted == 0 or counters["failed"] == 0:
        status = "ok"
    elif counters["failed"] == attempted:
        status = "failed"
    else:
        status = "partial"
    # NEW-001 (Astra R4A): a collector step whose own IMAP session
    # deadline fired before every candidate could be fetched (currently
    # only `run_xing` reports this key -- `run_bundesagentur` has no such
    # deadline and simply omits it, so `.get(..., False)` is the correct
    # default there) still has real work pending for this account, even
    # when every job it DID fetch scored/persisted cleanly (`status`
    # would otherwise be "ok" above). Reporting "ok" would claim this
    # source is fully caught up when it is not.
    if status == "ok" and counters.get("deadline_exceeded", False):
        status = "partial"
    return {"status": status, "counters": counters, "error_type": None}


async def _run_shortlist_drafts_step(db: Session, settings, touched_jobs: list[TouchedJob]) -> dict:
    """Stage 8C post-processing step — mirrors `_run_step`'s outer safety
    net (never lets an exception escape, so this step can never abort the
    run or block finalizing it) for a genuinely unexpected, STEP-level
    failure (e.g. the candidate-preselection query itself raising).
    Per-JOB failures within the step are already isolated and reported
    inside its own returned `items`/`failures`/`counters["failed"]` —
    `app.services.automation_shortlist.prepare_shortlist_drafts` never
    lets a single job's exception propagate this far. `touched_jobs`
    (S8C-POOL-001) is the exact set of jobs THIS run's own collector
    steps persisted, collected by `run_automation_cycle` below.
    """
    try:
        return await prepare_shortlist_drafts(db, touched_jobs=touched_jobs, settings=settings)
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
            "failures": None,
            "error_type": type(exc).__name__,
        }


async def _run_gmail_sync_step(db: Session, account_key: str, settings) -> dict:
    """Stage 8D post-processing step — mirrors `_run_step`'s outer safety
    net for a genuinely unexpected, STEP-level failure.
    `app.services.automation_gmail.prepare_gmail_sync` already isolates
    each mailbox's own failure internally (never lets it propagate this
    far); this only catches something even that function's own try/except
    didn't anticipate (e.g. the account-mismatch check itself raising).
    """
    try:
        return await prepare_gmail_sync(db, account_key=account_key, settings=settings)
    except Exception as exc:
        db.rollback()
        logger.warning(
            "automation_run_step_unexpected_error step=gmail_sync error_type=%s",
            type(exc).__name__,
        )
        return {"status": "failed", "counters": None, "error_type": type(exc).__name__}


async def _run_gmail_response_drafts_step(db: Session, account_key: str, settings) -> dict:
    """Stage 8D post-processing step — mirrors `_run_step`'s outer safety
    net. Per-MESSAGE failures within the step are already isolated and
    reported inside its own returned `items`/`failures`/
    `counters["failed"]` —
    `app.services.automation_gmail.prepare_gmail_response_drafts` never
    lets a single message's exception propagate this far.
    """
    try:
        return await prepare_gmail_response_drafts(db, account_key=account_key, settings=settings)
    except Exception as exc:
        db.rollback()
        logger.warning(
            "automation_run_step_unexpected_error step=gmail_response_drafts error_type=%s",
            type(exc).__name__,
        )
        return {
            "status": "failed",
            "counters": None,
            "items": None,
            "failures": None,
            "error_type": type(exc).__name__,
        }


async def _run_follow_up_proposals_step(db: Session, account_key: str, settings) -> dict:
    """Stage 8D post-processing step — mirrors `_run_step`'s outer safety
    net. Per-JOB failures within the step are already isolated and
    reported inside its own returned `items`/`failures`/
    `counters["failed"]` —
    `app.services.automation_follow_up.prepare_follow_up_proposals` never
    lets a single job's exception propagate this far.
    """
    try:
        return await prepare_follow_up_proposals(db, account_key=account_key, settings=settings)
    except Exception as exc:
        db.rollback()
        logger.warning(
            "automation_run_step_unexpected_error step=follow_up_proposals error_type=%s",
            type(exc).__name__,
        )
        return {
            "status": "failed",
            "counters": None,
            "items": None,
            "failures": None,
            "error_type": type(exc).__name__,
        }


def _compute_overall_status(step_results: dict[str, dict], core_step_names) -> str:
    """S8C-STATUS-001 (Codex review): core JOB COLLECTION success is
    authoritative for FAILED — a downstream Stage 8C `shortlist_drafts`
    step that happens to report "ok" (e.g. because zero candidate jobs
    were eligible, which is trivially "ok" on its own) must never promote
    a run whose actual collectors ALL failed/were-not-configured into
    PARTIAL. `core_step_names` is `AUTOMATION_STEPS`
    (`("bundesagentur", "xing")`) — the fixed Stage 8A collector list,
    unaffected by whether Stage 8C is enabled. If Stage 8C is disabled,
    `step_results` contains only those same core steps, so this reduces
    to exactly the previous all-ok/none-ok/mixed logic.

    AUD-009 (Astra R2): a core collector step's own `status` (see
    `_run_step`) can now be "partial" (some fetched jobs persisted, some
    failed) — that is still real, attributable business success, so it
    counts the same as "ok" for the FAILED-override check below; only
    "failed"/"not_configured" (zero jobs actually persisted) do not.
    Without this, a collector that partially succeeded would incorrectly
    make the WHOLE run report FAILED instead of PARTIAL.
    """
    core_success_count = sum(
        1
        for name in core_step_names
        if step_results.get(name, {}).get("status") in ("ok", "partial")
    )
    if core_success_count == 0:
        return "FAILED"
    ok_count = sum(1 for result in step_results.values() if result["status"] == "ok")
    if ok_count == len(step_results):
        return "COMPLETED"
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


def _raise_if_lease_lost(heartbeat: _RunLeaseHeartbeat, run_id: int, account_key: str) -> None:
    """NEW-004 (Astra R4A): the fail-closed gate `run_automation_cycle`
    calls before every collector/optional step and before returning to
    finalize the run — see `AutomationRunLeaseLostError`'s own docstring
    for the full rationale. Checking only ONCE at the very end (the old
    behavior) let a worker keep launching brand-new collector calls,
    IMAP/HTTP fetches, Telegram notifications, and research runs for an
    account it had ALREADY confirmed it no longer owned — a real risk of
    duplicate/overlapping execution against a replacement worker, not
    merely a bookkeeping inconsistency. A step already fully in flight
    when the heartbeat first detects loss is allowed to finish (this is
    checked BEFORE launching the NEXT one, never by interrupting one
    already running) — its own results remain valid and durable either
    way.
    """
    if heartbeat.lease_lost.is_set():
        raise AutomationRunLeaseLostError(
            f"Lost ownership of automation run id={run_id!r} for account_key="
            f"{account_key!r} before further work could safely proceed; refusing "
            "to launch new work."
        )


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
    # S8C-POOL-001: one run-scoped, in-memory sink shared by both collector
    # steps below -- the exact-attribution record of which jobs THIS run's
    # own collectors persisted, handed to Stage 8C instead of inferring
    # attribution from JobRecord.last_seen_at (which could also match a job
    # touched by a concurrent run for a different account, or a manual
    # endpoint call, in the same time window). Never persisted itself.
    touched_jobs: list[TouchedJob] = []
    try:
        step_results: dict[str, dict] = {}
        for step_name in AUTOMATION_STEPS:
            # NEW-004: checked before EVERY step, not only once at the
            # very end — see _raise_if_lease_lost's own docstring.
            _raise_if_lease_lost(heartbeat, run.id, account_key)
            step_results[step_name] = await _run_step(
                db, step_name, step_callables[step_name], settings, touched_jobs
            )

        # Stage 8C: opt-in only (Settings.automation_auto_prepare_enabled
        # defaults to False, independent of the scheduler being enabled —
        # see app.core.config.Settings). When disabled, AutomationRun.results
        # stays EXACTLY the Stage 8A/8B collector result shape — no fake
        # disabled/skipped step is ever added.
        if settings.automation_auto_prepare_enabled:
            _raise_if_lease_lost(heartbeat, run.id, account_key)
            step_results["shortlist_drafts"] = await _run_shortlist_drafts_step(
                db, settings, touched_jobs
            )

        # Stage 8D: two independent opt-in switches (off by default,
        # unrelated to every other automation flag above -- see
        # app.core.config.Settings). Disabled features add NO fake step
        # to results. gmail_response_drafts always runs when the Gmail
        # cycle is enabled, regardless of gmail_sync's own outcome (a
        # partial/failed sync must not block processing messages already
        # persisted from this or a previous run — see
        # app.services.automation_gmail's module docstring).
        if settings.automation_gmail_cycle_enabled:
            _raise_if_lease_lost(heartbeat, run.id, account_key)
            step_results["gmail_sync"] = await _run_gmail_sync_step(db, account_key, settings)
            _raise_if_lease_lost(heartbeat, run.id, account_key)
            step_results["gmail_response_drafts"] = await _run_gmail_response_drafts_step(
                db, account_key, settings
            )

        if settings.automation_follow_up_cycle_enabled:
            _raise_if_lease_lost(heartbeat, run.id, account_key)
            step_results["follow_up_proposals"] = await _run_follow_up_proposals_step(
                db, account_key, settings
            )

        overall_status = _compute_overall_status(step_results, AUTOMATION_STEPS)
        error_summary = _build_error_summary(step_results)

        # S8A-002: one last check immediately before the final write —
        # exclusivity for this run's own bookkeeping is no longer
        # provable once lost, so fail closed rather than risk overwriting
        # whatever a new owner has since recorded for this row. Every
        # step above already checked before ITS OWN launch (NEW-004); this
        # catches the remaining instant between the last step finishing
        # and this write.
        _raise_if_lease_lost(heartbeat, run.id, account_key)

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
