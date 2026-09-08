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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.automation_repository import list_runs, to_automation_run
from app.db.models import (
    FollowUpApprovalRecord,
    FollowUpProposalRecord,
    ResponseDraftApprovalRecord,
    ResponseDraftRecord,
)
from app.models.automation import AutomationRunStepResult
from app.providers.email.base import normalize_account_key

# Telegram's hard cap is 4096 chars -- kept well below it (matches the
# soft-limit convention already used by app.services.telegram_bot's own
# JOBS_REPLY_SOFT_LIMIT/RESEARCH_REPLY_SOFT_LIMIT) so there is headroom
# for a final truncation marker without ever risking the send itself
# raising on an oversized payload.
DIGEST_REPLY_SOFT_LIMIT = 3500

# Codex Stage 8E MEDIUM finding (PENDING COUNTS): bounds how many
# pending-approval ids this module will ever DISPLAY -- a pending
# backlog must never turn one /digest call (or one scheduled send) into
# an unbounded message. The COUNT itself is never bounded/inferred from
# this limit -- see `_pending_response_draft_count`/
# `_pending_follow_up_count` below, which run a separate, unbounded
# `SELECT COUNT(*)` so the reported total stays exact even when it
# exceeds this display limit by an arbitrary amount.
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


def resolve_digest_account_key(settings) -> str:
    """The ONE shared rule for "which account_key does the digest read"
    -- used by BOTH the manual `/digest` command
    (app.services.telegram_bot.cmd_digest) and the automatic daily
    digest (app.scheduler, which passes the result to
    `app.services.scheduler.run_due_digest_if_claimed`). Codex Stage 8E
    HIGH finding (ACCOUNT SCOPE): the two callers must never be able to
    silently read different namespaces because one normalizes/falls
    back differently than the other.

    If `automation_scheduler_account_key` is configured (non-blank
    after stripping), it is authoritative for BOTH callers -- the daily
    digest already requires it whenever
    `telegram_daily_digest_enabled=True` (see
    `Settings._validate_daily_digest_requires_account_key_when_enabled`),
    so using the SAME stripped value for the manual command keeps them
    reading identical rows even as the digest is toggled on/off.
    Otherwise (scheduler/digest never configured), the manual command
    falls back to the normalized `GMAIL_USERNAME` -- the same identity
    every other manual Gmail/API read endpoint already scopes itself to
    (GMAIL-002, see `app.api.routes._current_gmail_account_key`).

    **Does NOT change Stage 8B's own automation-cycle account_key
    semantics.** `app.scheduler`'s poll loop still passes
    `settings.automation_scheduler_account_key` UNCHANGED to
    `run_due_cycle_if_claimed` -- this helper is only ever used for the
    digest (both call sites), never for the automation cycle itself.
    """
    scheduler_account_key = settings.automation_scheduler_account_key.strip()
    if scheduler_account_key:
        return scheduler_account_key
    return normalize_account_key(settings.gmail_username)


def _format_id_list(ids: list[int], total_count: int) -> str:
    """Renders the (already display-bounded) `ids` list, with a
    "+N more" suffix computed from the REAL total (`total_count`, from
    a separate unbounded `COUNT(*)`), never from `len(ids)` itself --
    `len(ids)` is only ever `min(total_count, PENDING_IDS_DISPLAY_LIMIT)`,
    which would silently under-report how many more exist once the
    total exceeds the display limit.
    """
    if not ids:
        return "none"
    remaining = total_count - len(ids)
    suffix = f" (+{remaining} more)" if remaining > 0 else ""
    return ", ".join(f"#{i}" for i in ids) + suffix


_RESPONSE_DRAFT_PENDING_FILTER = (
    ResponseDraftRecord.status == "PROPOSED",
    ResponseDraftApprovalRecord.id.is_(None),
)
_FOLLOW_UP_PENDING_FILTER = (
    FollowUpProposalRecord.status == "PROPOSED",
    FollowUpApprovalRecord.id.is_(None),
)


def _pending_response_draft_count(db: Session, account_key: str) -> int:
    """Codex Stage 8E MEDIUM finding (PENDING COUNTS): the EXACT total
    of response drafts still awaiting a human decision -- `PROPOSED`
    and with no `ResponseDraftApprovalRecord` yet (see that model's
    docstring: a decision is permanent and insert-only, so "no row" is
    the exact, unambiguous definition of "still pending"). A separate,
    UNBOUNDED `SELECT COUNT(*)` -- never inferred from the display-
    bounded id list in `_pending_response_draft_display_ids`, which
    would silently under-report the true total once it exceeds
    `PENDING_IDS_DISPLAY_LIMIT`.
    """
    stmt = (
        select(func.count(ResponseDraftRecord.id))
        .select_from(ResponseDraftRecord)
        .outerjoin(
            ResponseDraftApprovalRecord,
            ResponseDraftApprovalRecord.response_draft_id == ResponseDraftRecord.id,
        )
        .where(ResponseDraftRecord.account_key == account_key, *_RESPONSE_DRAFT_PENDING_FILTER)
    )
    return db.scalar(stmt) or 0


def _pending_response_draft_display_ids(
    db: Session, account_key: str, limit: int = PENDING_IDS_DISPLAY_LIMIT
) -> list[int]:
    """A SEPARATE, bounded query for the ids actually rendered in the
    message -- deliberately never used to derive the count above (see
    `_pending_response_draft_count`'s docstring)."""
    stmt = (
        select(ResponseDraftRecord.id)
        .outerjoin(
            ResponseDraftApprovalRecord,
            ResponseDraftApprovalRecord.response_draft_id == ResponseDraftRecord.id,
        )
        .where(ResponseDraftRecord.account_key == account_key, *_RESPONSE_DRAFT_PENDING_FILTER)
        .order_by(ResponseDraftRecord.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def _pending_follow_up_count(db: Session, account_key: str) -> int:
    """Mirrors `_pending_response_draft_count` exactly, for
    `FollowUpProposalRecord`/`FollowUpApprovalRecord` instead -- the
    EXACT total, an unbounded `COUNT(*)`, never inferred from a
    display-bounded id list."""
    stmt = (
        select(func.count(FollowUpProposalRecord.id))
        .select_from(FollowUpProposalRecord)
        .outerjoin(
            FollowUpApprovalRecord,
            FollowUpApprovalRecord.follow_up_proposal_id == FollowUpProposalRecord.id,
        )
        .where(FollowUpProposalRecord.account_key == account_key, *_FOLLOW_UP_PENDING_FILTER)
    )
    return db.scalar(stmt) or 0


def _pending_follow_up_display_ids(
    db: Session, account_key: str, limit: int = PENDING_IDS_DISPLAY_LIMIT
) -> list[int]:
    """Mirrors `_pending_response_draft_display_ids` exactly."""
    stmt = (
        select(FollowUpProposalRecord.id)
        .outerjoin(
            FollowUpApprovalRecord,
            FollowUpApprovalRecord.follow_up_proposal_id == FollowUpProposalRecord.id,
        )
        .where(FollowUpProposalRecord.account_key == account_key, *_FOLLOW_UP_PENDING_FILTER)
        .order_by(FollowUpProposalRecord.id.asc())
        .limit(limit)
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

    pending_response_count = _pending_response_draft_count(db, account_key)
    pending_response_ids = _pending_response_draft_display_ids(db, account_key)
    sections.append(
        f"Pending response-draft approvals: {pending_response_count} "
        f"({_format_id_list(pending_response_ids, pending_response_count)})"
    )

    pending_follow_up_count = _pending_follow_up_count(db, account_key)
    pending_follow_up_ids = _pending_follow_up_display_ids(db, account_key)
    sections.append(
        f"Pending follow-up approvals: {pending_follow_up_count} "
        f"({_format_id_list(pending_follow_up_ids, pending_follow_up_count)})"
    )

    text = "\n".join(sections)
    while len(text) > DIGEST_REPLY_SOFT_LIMIT and len(sections) > 1:
        sections.pop()
        text = "\n".join(sections) + "\n...(truncated)"
    return text[:DIGEST_REPLY_SOFT_LIMIT]
