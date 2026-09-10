"""Gmail Inbox Foundation sync orchestration (Stage 7A).

Fetches messages via a read-only `GmailImapProvider` (injected by the
caller — see app/api/routes.py's `_run_gmail_sync`, which owns
configuration checks and provider construction, mirroring
`_run_xing`/`_run_bundesagentur`) and persists each one idempotently via
app.db.gmail_repository. Returns only aggregate counts (spec section 14)
— this service never returns or logs message content.

**Zero LLM, zero classification, zero job/application linkage.** This
service never reads or writes JobRecord/ApplicationStatus, never triggers
candidate-job matching, and calls no LLM/provider of any kind — purely
deterministic fetch + persist infrastructure (CLAUDE.md's Stage 7A
constraints).

**Privacy (GMAIL-003).** Logging here never includes subject/body/
addresses/display names, and never logs a caught exception's message
text or traceback (`exc_info`) — only `type(exc).__name__` and safe
counters. A persistence-layer error (e.g. a driver-level IntegrityError)
can in principle embed row content in its own message string; this
module never risks serializing that into a log line. One bad message's
persistence failure is isolated (rolled back and counted as `failed`)
and never aborts the rest of the sync run, mirroring `_run_xing`'s
per-job isolation.
"""

import logging

from sqlalchemy.orm import Session

from app.db.gmail_repository import record_permanent_skips, upsert_message
from app.models.gmail import GmailSyncResult
from app.providers.email.imap import GmailImapProvider

logger = logging.getLogger(__name__)


class GmailInboxService:
    async def sync(self, db: Session, provider: GmailImapProvider) -> GmailSyncResult:
        fetch_result = await provider.fetch()

        created = 0
        duplicates = 0
        failed = 0
        for parsed in fetch_result.messages:
            try:
                _, was_created = upsert_message(db, parsed)
            except Exception as exc:
                # Isolate one bad message's persistence failure — the rest
                # of an otherwise-successful sync must not be lost. A later
                # sync will see the same message again (its IMAP UID is
                # unaffected by our own failed write) and retry it.
                #
                # GMAIL-003: log only the exception's type, never its
                # message text or traceback (no exc_info) — a driver-level
                # error message can embed row content (subject/body/
                # addresses), which must never reach the logs.
                db.rollback()
                failed += 1
                logger.warning("gmail_message_persist_failed error_type=%s", type(exc).__name__)
                continue

            if was_created:
                created += 1
            else:
                duplicates += 1

        # FINAL-004 (Astra R5A): durably record every UID this run
        # determined is permanently unfetchable (message content itself
        # is the problem — see GmailFetchResult.permanently_skipped's
        # docstring) so a future sync's candidate list excludes it,
        # exactly like an already-persisted message already is — closing
        # a starvation bug where a prefix of such messages at least as
        # large as MAX_MESSAGES_PER_SYNC would otherwise consume every
        # future sync's entire budget forever. Best-effort: never allowed
        # to turn an otherwise-successful sync (the created/duplicates/
        # failed counts above) into a failure — see
        # `record_permanent_skips`'s own per-row isolation.
        if fetch_result.permanently_skipped:
            assert fetch_result.uid_validity is not None
            try:
                record_permanent_skips(
                    db,
                    provider.account_key,
                    provider.mailbox,
                    fetch_result.uid_validity,
                    fetch_result.permanently_skipped,
                )
            except Exception as exc:
                # GMAIL-003: type only, never the exception's own message
                # text (a driver-level error can embed row content).
                db.rollback()
                logger.warning(
                    "gmail_permanent_skip_persist_failed error_type=%s", type(exc).__name__
                )

        logger.info(
            "gmail_sync_run fetched=%s created=%s duplicates=%s skipped=%s failed=%s "
            "deadline_exceeded=%s",
            len(fetch_result.messages),
            created,
            duplicates,
            fetch_result.skipped_count,
            failed,
            fetch_result.deadline_exceeded,
        )

        return GmailSyncResult(
            fetched=len(fetch_result.messages),
            created=created,
            duplicates=duplicates,
            skipped=fetch_result.skipped_count,
            failed=failed,
            deadline_exceeded=fetch_result.deadline_exceeded,
        )
