"""Stage 8D: automated Gmail sync + response-draft preparation cycle,
triggered from `app.services.automation.run_automation_cycle` after the
existing Stage 8A/8C steps finish — opt-in via
`Settings.automation_gmail_cycle_enabled` (off by default; independent
of `automation_scheduler_enabled`/`automation_auto_prepare_enabled`, see
`app.core.config.Settings`).

**Reuse, not duplication.** All actual Gmail I/O and message processing
is the SAME service-layer logic the manual endpoints already use:
`app.services.gmail_sync.sync_mailbox` (Stage 7A, itself extracted
unchanged from the former `app.api.routes._run_gmail_sync`),
`app.services.gmail_message_analysis.analyze_gmail_message` (Stage 7B),
and `app.services.response_draft.generate_response_draft_for_message`
(Stage 7C). This module adds ONLY: (1) an account-identity fail-closed
check before ever syncing, (2) per-mailbox failure isolation for the
`gmail_sync` step (the manual endpoint intentionally stays all-or
-nothing), and (3) a persisted, crash-safe, at-least-once cursor over
already-stored `GmailMessageRecord`s for the `gmail_response_drafts`
step.

**Drafts only.** This module creates ONLY `GmailMessageRecord`
(read-only IMAP sync)/`GmailMessageAnalysisRecord`/`ResponseDraftRecord`
rows (all pre-existing Stage 7A/7B/7C tables) plus technical metadata
returned for persistence in `AutomationRun.results`. It never sends an
email, never creates a Gmail draft, never approves anything, and never
transitions a `JobRecord`'s status. It NEVER imports or calls
`app.services.response_draft_send.send_response_draft`/
`approve_or_reject_response_draft` or
`app.providers.email.smtp.GmailSmtpProvider` — those remain entirely
manual, unchanged from Stage 7D.

**Account isolation, fail-closed (spec section 5).** `account_key` here
is the SAME identity `run_automation_cycle` was invoked with (mirrors
GMAIL-002's convention — see `app.db.models.AutomationRunRecord`'s own
docstring: "the normalized GMAIL_USERNAME this run was scoped to"). Before
ever touching IMAP, `prepare_gmail_sync` verifies
`normalize_account_key(settings.gmail_username) == account_key` --  a
mismatch means this run's own account identity and the CURRENTLY
configured Gmail credentials disagree (e.g. `GMAIL_USERNAME` changed
between when the run was scheduled and when it executes), and syncing
under that ambiguity is refused outright (`"failed"`, never silently
proceeds) rather than risk reading one account's mailbox and
persisting/operating as though it belonged to another.
`gmail_response_drafts` never touches live IMAP at all -- its own
DB-level account scoping (`account_key` on every query) is the
account-isolation boundary there.

**Partial mailbox failure (spec section 6).** Unlike the manual
`POST /gmail/sync` endpoint (which stays all-or-nothing: either mailbox
raising aborts the whole call), `prepare_gmail_sync` syncs INBOX and Sent
in their OWN try/except blocks -- a successful INBOX persist is never
undone merely because the Sent sync later fails, and vice versa. The step
is `"ok"` only if both mailboxes synced cleanly, `"partial"` if exactly
one failed, `"failed"` if both did.

**Crash-safe Gmail cursor (spec sections 3/4/7/8/9).** See
`app.db.models.AutomationMailProgressRecord`'s docstring for the full
rationale. `gmail_after_message_id` anchors progress to
`GmailMessageRecord.id` (never an in-memory "messages touched this run"
list, which an IMAP provider's own known-UID skip logic could make lose
a message permanently across a crash). Every message's FULL pipeline
(analysis, then response-draft-or-NO_RESPONSE_RECOMMENDED) must succeed
before the cursor advances past it, via
`app.db.automation_mail_progress_repository.advance_gmail_cursor`'s CAS
-- advanced ONE message at a time (never batched at the end), so a crash
immediately after message N's cursor-advance loses at most the
in-progress message N+1, never N. If message N's pipeline fails (or the
CAS itself is lost to a newer owner), processing STOPS for this run --
no message after N is even attempted, and the cursor is never advanced
past N -- guaranteeing at-least-once retry on the next cycle, never a
silently skipped message. `NO_RESPONSE_RECOMMENDED` is a normal,
successfully-processed outcome (Stage 7C's own classification result),
not a failure -- the cursor advances past it exactly like a real
proposed draft.
"""

import logging

from sqlalchemy.orm import Session

from app.collectors.base import CollectorNotConfiguredError, is_configured
from app.db.automation_mail_progress_repository import (
    advance_gmail_cursor,
    get_or_create_mail_progress,
)
from app.db.gmail_repository import list_messages_by_id_after
from app.models.automation import AutomationMessageFailure, AutomationMessageItem
from app.models.gmail import GmailSyncResult
from app.providers.email.base import normalize_account_key
from app.services.gmail_message_analysis import analyze_gmail_message
from app.services.gmail_sync import sync_mailbox
from app.services.response_draft import generate_response_draft_for_message

logger = logging.getLogger(__name__)

__all__ = ["GmailAccountMismatchError", "prepare_gmail_response_drafts", "prepare_gmail_sync"]

_EMPTY_SYNC_RESULT = GmailSyncResult(fetched=0, created=0, duplicates=0, skipped=0, failed=0)


class GmailAccountMismatchError(Exception):
    """This AutomationRun's own `account_key` does not match
    `normalize_account_key(settings.gmail_username)` -- see module
    docstring's "Account isolation, fail-closed" section.
    """


async def prepare_gmail_sync(db: Session, *, account_key: str, settings) -> dict:
    """Runs the Stage 8D `gmail_sync` step. Returns an
    AutomationRunStepResult-shaped dict (`{"status", "counters",
    "error_type"}` -- no `items`/`failures`; nothing here is per-item
    technical metadata, only two mailbox-level outcomes already folded
    into `counters`/`status`).
    """
    if not is_configured(settings.gmail_username) or not is_configured(settings.gmail_app_password):
        exc = CollectorNotConfiguredError(
            "Gmail inbox sync is not configured: set GMAIL_USERNAME and GMAIL_APP_PASSWORD."
        )
        return {"status": "not_configured", "counters": None, "error_type": type(exc).__name__}

    normalized = normalize_account_key(settings.gmail_username)
    if normalized != account_key:
        mismatch = GmailAccountMismatchError(
            "automation account_key does not match normalize_account_key(settings.gmail_username)"
        )
        logger.warning("automation_gmail_sync_account_mismatch account_key=%s", account_key)
        return {"status": "failed", "counters": None, "error_type": type(mismatch).__name__}

    inbox_error_type: str | None = None
    inbox_result: GmailSyncResult | None = None
    try:
        inbox_result = await sync_mailbox(
            db, settings, account_key, settings.gmail_mailbox, trusted_outbound=False
        )
    except Exception as exc:
        db.rollback()
        inbox_error_type = type(exc).__name__
        logger.warning(
            "automation_gmail_inbox_sync_failed account_key=%s error_type=%s",
            account_key,
            inbox_error_type,
        )

    sent_error_type: str | None = None
    sent_result: GmailSyncResult | None = None
    try:
        sent_result = await sync_mailbox(
            db, settings, account_key, settings.gmail_sent_mailbox, trusted_outbound=True
        )
    except Exception as exc:
        db.rollback()
        sent_error_type = type(exc).__name__
        logger.warning(
            "automation_gmail_sent_sync_failed account_key=%s error_type=%s",
            account_key,
            sent_error_type,
        )

    if inbox_error_type is None and sent_error_type is None:
        status = "ok"
    elif inbox_error_type is not None and sent_error_type is not None:
        status = "failed"
    else:
        status = "partial"

    inbox_r = inbox_result or _EMPTY_SYNC_RESULT
    sent_r = sent_result or _EMPTY_SYNC_RESULT
    counters = {
        "fetched": inbox_r.fetched + sent_r.fetched,
        "created": inbox_r.created + sent_r.created,
        "duplicates": inbox_r.duplicates + sent_r.duplicates,
        "skipped": inbox_r.skipped + sent_r.skipped,
        "failed": inbox_r.failed + sent_r.failed,
        "inbox_fetched": inbox_r.fetched,
        "inbox_created": inbox_r.created,
        "inbox_duplicates": inbox_r.duplicates,
        "inbox_skipped": inbox_r.skipped,
        "inbox_failed": inbox_r.failed,
        "sent_fetched": sent_r.fetched,
        "sent_created": sent_r.created,
        "sent_duplicates": sent_r.duplicates,
        "sent_skipped": sent_r.skipped,
        "sent_failed": sent_r.failed,
    }

    logger.info(
        "automation_gmail_sync_finished account_key=%s status=%s fetched=%s created=%s "
        "duplicates=%s skipped=%s failed=%s",
        account_key,
        status,
        counters["fetched"],
        counters["created"],
        counters["duplicates"],
        counters["skipped"],
        counters["failed"],
    )

    return {
        "status": status,
        "counters": counters,
        "error_type": inbox_error_type or sent_error_type,
    }


async def prepare_gmail_response_drafts(db: Session, *, account_key: str, settings) -> dict:
    """Runs the Stage 8D `gmail_response_drafts` step. Returns an
    AutomationRunStepResult-shaped dict (`{"status", "counters", "items",
    "failures", "error_type"}`). Always runs the bounded scan over
    already-persisted `GmailMessageRecord`s for this account, regardless
    of whether the `gmail_sync` step (if it ran at all this cycle)
    succeeded, partially failed, or was not configured -- see module
    docstring's "Partial mailbox failure" section.
    """
    progress = get_or_create_mail_progress(db, account_key)
    cursor = progress.gmail_after_message_id
    messages = list_messages_by_id_after(
        db, account_key, after_id=cursor, limit=settings.automation_gmail_process_max_per_run
    )

    scanned = 0
    analyzed_created = 0
    analyzed_reused = 0
    draft_created = 0
    draft_reused = 0
    no_response_recommended = 0
    failed = 0
    items: list[AutomationMessageItem] = []
    failures: list[AutomationMessageFailure] = []

    for message in messages:
        scanned += 1
        try:
            analysis_record, analysis_was_created = analyze_gmail_message(
                db, account_key, message.id
            )
        except Exception as exc:
            db.rollback()
            failed += 1
            logger.warning(
                "automation_gmail_message_analysis_failed account_key=%s gmail_message_id=%s "
                "error_type=%s",
                account_key,
                message.id,
                type(exc).__name__,
            )
            failures.append(
                AutomationMessageFailure(
                    gmail_message_id=message.id, phase="analysis", error_type=type(exc).__name__
                )
            )
            items.append(
                AutomationMessageItem(
                    gmail_message_id=message.id,
                    status="failed",
                    phase="analysis",
                    error_type=type(exc).__name__,
                )
            )
            break  # at-least-once retry (spec section 9): stop, never skip ahead

        try:
            draft_record, draft_was_created = generate_response_draft_for_message(
                db, account_key, message.id
            )
        except Exception as exc:
            db.rollback()
            failed += 1
            logger.warning(
                "automation_gmail_response_draft_failed account_key=%s gmail_message_id=%s "
                "error_type=%s",
                account_key,
                message.id,
                type(exc).__name__,
            )
            failures.append(
                AutomationMessageFailure(
                    gmail_message_id=message.id,
                    phase="response_draft",
                    error_type=type(exc).__name__,
                )
            )
            # S8C-AUDIT-001-style truthful audit: the analysis above DID
            # durably commit -- report it, even though the draft failed.
            items.append(
                AutomationMessageItem(
                    gmail_message_id=message.id,
                    analysis_id=analysis_record.id,
                    analysis_created=analysis_was_created,
                    status="failed",
                    phase="response_draft",
                    error_type=type(exc).__name__,
                )
            )
            break

        if analysis_was_created:
            analyzed_created += 1
        else:
            analyzed_reused += 1

        if draft_record.status == "NO_RESPONSE_RECOMMENDED":
            no_response_recommended += 1
        elif draft_was_created:
            draft_created += 1
        else:
            draft_reused += 1

        items.append(
            AutomationMessageItem(
                gmail_message_id=message.id,
                analysis_id=analysis_record.id,
                response_draft_id=draft_record.id,
                response_status=draft_record.status,
                analysis_created=analysis_was_created,
                draft_created=draft_was_created,
                status="ok",
            )
        )

        advanced = advance_gmail_cursor(
            db, account_key, expected_cursor=cursor, new_cursor=message.id
        )
        if not advanced:
            # S8D-PROGRESS CAS lost: a newer owner already moved this
            # account's progress -- fail closed, never overwrite.
            logger.warning("automation_gmail_cursor_cas_lost account_key=%s", account_key)
            break
        cursor = message.id

    if scanned == 0 or failed == 0:
        status = "ok"
    elif failed == scanned:
        status = "failed"
    else:
        status = "partial"

    counters = {
        "scanned": scanned,
        "analyzed_created": analyzed_created,
        "analyzed_reused": analyzed_reused,
        "draft_created": draft_created,
        "draft_reused": draft_reused,
        "no_response_recommended": no_response_recommended,
        "failed": failed,
    }

    logger.info(
        "automation_gmail_response_drafts_finished account_key=%s status=%s scanned=%s "
        "analyzed_created=%s analyzed_reused=%s draft_created=%s draft_reused=%s "
        "no_response_recommended=%s failed=%s",
        account_key,
        status,
        scanned,
        analyzed_created,
        analyzed_reused,
        draft_created,
        draft_reused,
        no_response_recommended,
        failed,
    )

    return {
        "status": status,
        "counters": counters,
        "items": [item.model_dump() for item in items],
        "failures": [failure.model_dump() for failure in failures],
        "error_type": None,
    }
