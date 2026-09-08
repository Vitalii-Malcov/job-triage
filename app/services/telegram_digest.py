"""Stage 8E: bounded, privacy-safe Telegram digest content -- shared by
the manual `/digest` command (app.services.telegram_bot.cmd_digest) and
the optional automatic daily digest
(app.services.scheduler.run_due_digest_if_claimed). Both call
`build_digest_text` with whatever account_key their own caller already
uses -- this module never decides which account to report on.

**Privacy boundary (hard requirement, Stage 8E spec).** Every value this
module ever reads and formats is already one of: an integer id, an
integer count, a fixed status string, or `type(exc).__name__` -- never
an email address, a message subject/body, a response/follow-up draft's
text, a job title/company, or any credential. This is possible because
every source table Stage 8E reads from was ALREADY built to this exact
"technical metadata only" contract by Stage 8C/8D (see
app.models.automation's module docstring, and
ResponseDraftRecord/FollowUpProposalRecord's own "information only"
docstrings in app.db.models) -- this module adds no new privacy
reasoning of its own, it only assembles what was already safe to show.
`account_key` itself (the normalized Gmail address, GMAIL-002) is
deliberately NEVER included in the rendered text, even though every
query here is scoped by it.

Read-only. Nothing in this module writes to any table, sends email,
approves anything, or mutates Job.status.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.automation_repository import list_runs, to_automation_run
from app.db.models import (
    FollowUpApprovalRecord,
    FollowUpProposalRecord,
    ResponseDraftApprovalRecord,
    ResponseDraftRecord,
)
from app.models.automation import AutomationRunStepResult

# Telegram's hard cap is 4096 chars -- kept well below it (matches the
# soft-limit convention already used by app.services.telegram_bot's own
# JOBS_REPLY_SOFT_LIMIT/RESEARCH_REPLY_SOFT_LIMIT) so there is headroom
# for a final truncation marker without ever risking the send itself
# raising on an oversized payload.
DIGEST_REPLY_SOFT_LIMIT = 3500

# Bounds how many pending-approval ids this module will ever query, let
# alone display -- a pending backlog must never turn one /digest call
# (or one scheduled send) into an unbounded table scan.
PENDING_IDS_QUERY_LIMIT = 200
PENDING_IDS_DISPLAY_LIMIT = 10

# Fixed, deterministic order -- never dict iteration order, which is
# insertion-order-dependent on whatever steps a given run happened to
# populate.
_STEP_ORDER = (
    "bundesagentur",
    "xing",
    "shortlist_drafts",
    "gmail_response_drafts",
    "follow_up_proposals",
)
_STEP_LABELS = {
    "bundesagentur": "Bundesagentur collector",
    "xing": "XING collector",
    "shortlist_drafts": "Shortlist drafts",
    "gmail_response_drafts": "Gmail response drafts",
    "follow_up_proposals": "Follow-up proposals",
}


def _format_id_list(ids: list[int], limit: int = PENDING_IDS_DISPLAY_LIMIT) -> str:
    if not ids:
        return "none"
    shown = ids[:limit]
    suffix = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
    return ", ".join(f"#{i}" for i in shown) + suffix


def _count_label(ids: list[int], query_limit: int = PENDING_IDS_QUERY_LIMIT) -> str:
    if len(ids) >= query_limit:
        return f"{query_limit}+"
    return str(len(ids))


def _pending_response_draft_ids(db: Session, account_key: str) -> list[int]:
    """Response drafts still awaiting a human decision -- `PROPOSED` and
    with no `ResponseDraftApprovalRecord` yet (see that model's
    docstring: a decision is permanent and insert-only, so "no row" is
    the exact, unambiguous definition of "still pending")."""
    stmt = (
        select(ResponseDraftRecord.id)
        .outerjoin(
            ResponseDraftApprovalRecord,
            ResponseDraftApprovalRecord.response_draft_id == ResponseDraftRecord.id,
        )
        .where(
            ResponseDraftRecord.account_key == account_key,
            ResponseDraftRecord.status == "PROPOSED",
            ResponseDraftApprovalRecord.id.is_(None),
        )
        .order_by(ResponseDraftRecord.id.asc())
        .limit(PENDING_IDS_QUERY_LIMIT)
    )
    return list(db.scalars(stmt).all())


def _pending_follow_up_ids(db: Session, account_key: str) -> list[int]:
    """Follow-up proposals still awaiting a human decision -- mirrors
    `_pending_response_draft_ids` exactly, for `FollowUpProposalRecord`/
    `FollowUpApprovalRecord` instead."""
    stmt = (
        select(FollowUpProposalRecord.id)
        .outerjoin(
            FollowUpApprovalRecord,
            FollowUpApprovalRecord.follow_up_proposal_id == FollowUpProposalRecord.id,
        )
        .where(
            FollowUpProposalRecord.account_key == account_key,
            FollowUpProposalRecord.status == "PROPOSED",
            FollowUpApprovalRecord.id.is_(None),
        )
        .order_by(FollowUpProposalRecord.id.asc())
        .limit(PENDING_IDS_QUERY_LIMIT)
    )
    return list(db.scalars(stmt).all())


def _format_step_line(step_name: str, step: AutomationRunStepResult) -> str:
    label = _STEP_LABELS.get(step_name, step_name)
    parts = [f"{label}: {step.status}"]
    if step.counters:
        parts.append(" ".join(f"{key}={value}" for key, value in step.counters.items()))
    if step.failures:
        parts.append(f"failures={len(step.failures)}")
    if step.error_type:
        parts.append(f"error={step.error_type}")
    return " | ".join(parts)


def build_digest_text(db: Session, account_key: str) -> str:
    """Bounded, privacy-safe digest content for `account_key` -- shared
    assembly for both the manual `/digest` command and the optional
    automatic daily digest. Never raises for "nothing to report yet" (a
    brand-new account with no `AutomationRun` ever still produces a
    valid, if short, message) -- only a genuine DB failure propagates,
    exactly like every other read path in this project.

    Deterministically truncated to stay under Telegram's 4096-char cap
    with headroom (`DIGEST_REPLY_SOFT_LIMIT`) -- mirrors
    app.services.telegram_bot._build_jobs_reply's own "pop the
    least-essential section from the end" strategy rather than risking
    an oversized `reply_text()`/`sendMessage` call.
    """
    sections: list[str] = ["Automation digest"]

    latest_runs = list_runs(db, account_key, limit=1)
    if not latest_runs:
        sections.append("Latest run: none yet")
    else:
        run = to_automation_run(latest_runs[0])
        finished = run.finished_at.isoformat() if run.finished_at else "running"
        sections.append(f"Latest run: #{run.id} status={run.status} finished={finished}")
        for step_name in _STEP_ORDER:
            step = run.results.get(step_name)
            if step is not None:
                sections.append(_format_step_line(step_name, step))

    pending_response_ids = _pending_response_draft_ids(db, account_key)
    sections.append(
        f"Pending response-draft approvals: {_count_label(pending_response_ids)} "
        f"({_format_id_list(pending_response_ids)})"
    )

    pending_follow_up_ids = _pending_follow_up_ids(db, account_key)
    sections.append(
        f"Pending follow-up approvals: {_count_label(pending_follow_up_ids)} "
        f"({_format_id_list(pending_follow_up_ids)})"
    )

    text = "\n".join(sections)
    while len(text) > DIGEST_REPLY_SOFT_LIMIT and len(sections) > 1:
        sections.pop()
        text = "\n".join(sections) + "\n...(truncated)"
    return text[:DIGEST_REPLY_SOFT_LIMIT]
