"""Stage 8D: automated follow-up proposal cycle, triggered from
`app.services.automation.run_automation_cycle` after the existing Stage
8A/8C/8D-Gmail steps finish — opt-in via
`Settings.automation_follow_up_cycle_enabled` (off by default;
independent of every other automation flag, see `app.core.config.Settings`).

**Reuse, not duplication.** All eligibility/recipient/generation logic is
the SAME service-layer function the manual endpoint already uses:
`app.services.follow_up.evaluate_follow_up_for_job` (Stage 7E) — this
module never re-derives eligibility, never re-implements recipient
derivation, never re-implements follow-up generation. It NEVER imports or
calls `app.services.follow_up_send.send_follow_up`/
`approve_or_reject_follow_up` — sending/approving remain entirely manual,
unchanged from Stage 7E.

**Round-robin, not a one-directional scan (spec sections 12/13).** Unlike
Stage 8D's Gmail message cursor (which only ever moves forward — new
messages always get larger ids), a job's follow-up eligibility can become
due purely because TIME passed, with no new activity to "wake" it. A
one-directional cursor would eventually stop discovering anything new
once it caught up to the newest APPLIED job. `follow_up_after_job_id`
(see `app.db.models.AutomationMailProgressRecord`'s own docstring)
therefore WRAPS: once one bounded scan reaches the end of the
currently-APPLIED job list, it resets back to NULL (via the same CAS
primitive used to advance it) so the NEXT `AutomationRun` starts over
from the oldest currently-APPLIED job — never immediately re-scanning in
the SAME run (that would defeat the whole point of bounding the scan per
cycle).

**Failure rule (spec section 14).** `evaluate_follow_up_for_job` already
converts a recipient-invalid condition into a normal `NOT_ELIGIBLE`
result internally (see that function's own docstring) — that is NOT a
system failure and never stops the scan. Only a genuinely UNEXPECTED
exception (e.g. a bug, a DB error) is treated as a failure: rolled back,
recorded as `job_id`/`phase="follow_up"`/`type(exc).__name__` only
(never `str(exc)`/`repr(exc)`/a traceback), and the scan STOPS for this
run without advancing the cursor past the failed job — the next cycle
retries it, exactly mirroring the Gmail cursor's own at-least-once
contract.

**Proposal reuse is already handled upstream.** `evaluate_follow_up_for_job`
itself is idempotent — reusing an existing `FollowUpProposalRecord` when
its `(account_key, anchor_gmail_message_id, input_fingerprint)` identity
is unchanged (see `app.db.follow_up_repository.get_or_create_follow_up_proposal`).
This module only reads `result.created` to report the outcome; it never
adds a second layer of reuse logic on top.
"""

import logging

from sqlalchemy.orm import Session

from app.db.automation_mail_progress_repository import (
    advance_follow_up_cursor,
    get_or_create_mail_progress,
)
from app.db.repositories import list_jobs_by_status_after_id
from app.models.application_status import ApplicationStatus
from app.models.automation import AutomationFollowUpItem, AutomationJobFailure
from app.services.follow_up import evaluate_follow_up_for_job

logger = logging.getLogger(__name__)

__all__ = ["prepare_follow_up_proposals"]


async def prepare_follow_up_proposals(db: Session, *, account_key: str, settings) -> dict:
    """Runs the Stage 8D `follow_up_proposals` step. Returns an
    AutomationRunStepResult-shaped dict (`{"status", "counters", "items",
    "failures", "error_type"}`). Never sends, never approves, never
    mutates `JobRecord.status` — see module docstring.
    """
    progress = get_or_create_mail_progress(db, account_key)
    cursor = progress.follow_up_after_job_id
    jobs = list_jobs_by_status_after_id(
        db,
        ApplicationStatus.APPLIED,
        after_id=cursor,
        limit=settings.automation_follow_up_job_max_per_run,
    )

    scanned = 0
    eligible = 0
    not_eligible = 0
    proposal_created = 0
    proposal_reused = 0
    failed = 0
    cursor_wrapped = 0
    interrupted = False
    items: list[AutomationFollowUpItem] = []
    failures: list[AutomationJobFailure] = []

    for job in jobs:
        scanned += 1
        try:
            result = evaluate_follow_up_for_job(db, account_key, job.id, settings=settings)
        except Exception as exc:
            db.rollback()
            failed += 1
            interrupted = True
            logger.warning(
                "automation_follow_up_evaluation_failed account_key=%s job_id=%s error_type=%s",
                account_key,
                job.id,
                type(exc).__name__,
            )
            failures.append(
                AutomationJobFailure(
                    job_id=job.id, phase="follow_up", error_type=type(exc).__name__
                )
            )
            break  # at-least-once retry: stop, never skip ahead past the failed job

        if result.eligibility == "ELIGIBLE":
            eligible += 1
            if result.created:
                proposal_created += 1
            else:
                proposal_reused += 1
            proposal_id = result.proposal.id if result.proposal is not None else None
        else:
            not_eligible += 1
            proposal_id = None

        items.append(
            AutomationFollowUpItem(
                job_id=job.id,
                eligibility=result.eligibility,
                proposal_id=proposal_id,
                proposal_created=result.created,
            )
        )

        advanced = advance_follow_up_cursor(
            db, account_key, expected_cursor=cursor, new_cursor=job.id
        )
        if not advanced:
            # S8D-PROGRESS CAS lost: a newer owner already moved this
            # account's progress -- fail closed, never overwrite.
            interrupted = True
            logger.warning("automation_follow_up_cursor_cas_lost account_key=%s", account_key)
            break
        cursor = job.id

    if not interrupted:
        # Reached the end of this bounded batch without any failure/CAS
        # loss -- check whether that also means the end of the
        # currently-APPLIED job list, and if so wrap the cursor back to
        # the start for the NEXT run (never re-scanning within this one).
        remaining = list_jobs_by_status_after_id(
            db, ApplicationStatus.APPLIED, after_id=cursor, limit=1
        )
        if not remaining:
            wrapped = advance_follow_up_cursor(
                db, account_key, expected_cursor=cursor, new_cursor=None
            )
            if wrapped:
                cursor_wrapped = 1

    if scanned == 0 or failed == 0:
        status = "ok"
    elif failed == scanned:
        status = "failed"
    else:
        status = "partial"

    counters = {
        "scanned": scanned,
        "eligible": eligible,
        "not_eligible": not_eligible,
        "proposal_created": proposal_created,
        "proposal_reused": proposal_reused,
        "failed": failed,
        "cursor_wrapped": cursor_wrapped,
    }

    logger.info(
        "automation_follow_up_proposals_finished account_key=%s status=%s scanned=%s eligible=%s "
        "not_eligible=%s proposal_created=%s proposal_reused=%s failed=%s cursor_wrapped=%s",
        account_key,
        status,
        scanned,
        eligible,
        not_eligible,
        proposal_created,
        proposal_reused,
        failed,
        cursor_wrapped,
    )

    return {
        "status": status,
        "counters": counters,
        "items": [item.model_dump() for item in items],
        "failures": [failure.model_dump() for failure in failures],
        "error_type": None,
    }
