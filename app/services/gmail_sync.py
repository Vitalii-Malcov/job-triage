"""Gmail Inbox Foundation sync orchestration (Stage 7A), extracted from
`app.api.routes` (Stage 8D) so `app.services.automation_gmail`'s
automated sync step can reuse it without importing the API layer --
`app.services` code must never import `app.api.routes` (routes.py
depends on services, never the reverse; see
`app.services.collector_runner`'s own module docstring for the identical
rule already established for Stage 8A's collectors).

**Zero behavior change for the manual endpoint.** `run_gmail_sync` below
is a verbatim relocation of what `app.api.routes` used to define
privately as `_run_gmail_sync`/`_sum_gmail_sync_results` -- same
configuration check, same `normalize_account_key` call, same provider
construction, same INBOX-then-Sent ordering, same
`trusted_outbound=False`/`True` split, same aggregate-counter return
shape. `POST /gmail/sync` now calls this function instead of retaining a
private duplicate.

**Preserves every Stage 7A security guarantee** (see
`app.providers.email.base`'s module docstring for the full hard
constraints this relies on, and `app.providers.email.imap.GmailImapProvider`
for their enforcement):

- read-only IMAP only (`BODY.PEEK[]`, never a bare `RFC822`/`BODY[]`
  fetch that would implicitly mark a message `\\Seen`) -- zero mailbox
  writes of any kind (no STORE/EXPUNGE/COPY/APPEND);
- INBOX is always INBOUND; the configured Sent mailbox
  (`settings.gmail_sent_mailbox`) is the ONLY trusted OUTBOUND source
  (S7E-001) -- direction is decided purely by WHICH mailbox a message
  was fetched from, never by its own forgeable `From` header;
- known-UID starvation protection (GMAIL-005/012): `get_known_uids` is
  bound into each provider via closure so already-persisted UIDs are
  skipped BEFORE `MAX_MESSAGES_PER_SYNC` is applied, in one bulk query
  per chunk rather than one query per UID;
- zero HTTP/link fetching of any kind from email content;
- account isolation (GMAIL-002): every persisted row and every
  known-UID lookup is scoped by `normalize_account_key(settings.gmail_username)`.

`sync_mailbox` is exposed separately (not just the combined
`run_gmail_sync`) so `app.services.automation_gmail`'s Stage 8D step can
sync INBOX and Sent independently, each in its own try/except -- a
partial-mailbox-failure tolerance the manual endpoint intentionally does
NOT have (a failure in either mailbox there still propagates as a single
502, unchanged from the original `_run_gmail_sync` behavior).
"""

from sqlalchemy.orm import Session

from app.collectors.base import CollectorNotConfiguredError, is_configured
from app.db.gmail_repository import get_known_uids
from app.models.gmail import GmailSyncResult
from app.providers.email.base import normalize_account_key
from app.providers.email.imap import GmailImapProvider
from app.services.gmail_inbox import GmailInboxService

__all__ = [
    "make_gmail_provider",
    "run_gmail_sync",
    "sync_mailbox",
]


def make_gmail_provider(
    db: Session, settings, account_key: str, mailbox: str, *, trusted_outbound: bool
) -> GmailImapProvider:
    """Construct one read-only `GmailImapProvider` for `mailbox`, with
    `get_known_uids` bound to `db`/`account_key`/`mailbox` via closure
    (the `_mailbox=mailbox` default-argument trick avoids a
    late-binding-closure bug across the two separate calls this function
    is used for -- INBOX then Sent).
    """
    return GmailImapProvider(
        imap_host=settings.gmail_imap_host,
        imap_port=settings.gmail_imap_port,
        username=settings.gmail_username,
        app_password=settings.gmail_app_password,
        mailbox=mailbox,
        lookback_days=settings.gmail_lookback_days,
        get_known_uids=lambda uid_validity, candidate_uids, _mailbox=mailbox: get_known_uids(
            db, account_key, _mailbox, uid_validity, candidate_uids
        ),
        trusted_outbound=trusted_outbound,
    )


async def sync_mailbox(
    db: Session, settings, account_key: str, mailbox: str, *, trusted_outbound: bool
) -> GmailSyncResult:
    """Fetch + persist one mailbox. Raises whatever
    `GmailInboxService.sync`/`GmailImapProvider.fetch` raises (e.g.
    `GmailAuthError`/`GmailConnectionError`) -- callers decide whether a
    failure here should abort the whole sync (the manual endpoint,
    unchanged) or be isolated per-mailbox (Stage 8D's automated step).
    """
    provider = make_gmail_provider(
        db, settings, account_key, mailbox, trusted_outbound=trusted_outbound
    )
    return await GmailInboxService().sync(db, provider)


def _sum_gmail_sync_results(a: GmailSyncResult, b: GmailSyncResult) -> GmailSyncResult:
    return GmailSyncResult(
        fetched=a.fetched + b.fetched,
        created=a.created + b.created,
        duplicates=a.duplicates + b.duplicates,
        skipped=a.skipped + b.skipped,
        failed=a.failed + b.failed,
    )


async def run_gmail_sync(db: Session, settings) -> GmailSyncResult:
    """Fetch (read-only IMAP) + persist one Gmail Inbox Foundation sync
    run -- used by `POST /gmail/sync` (unchanged behavior from before
    this Stage 8D extraction).

    The configuration check lives here (not inside `GmailInboxService`),
    mirroring `app.services.collector_runner.run_xing`/`run_bundesagentur`'s
    own split between "not configured" (503, see the route handler) and
    "upstream/provider failure" (502) -- `GmailInboxService` itself never
    fails closed on missing credentials, it just orchestrates
    fetch+persist for an already-constructed provider.

    S7E-001 (Codex remediation, HIGH): syncs BOTH the primary mailbox
    (`gmail_mailbox`, INBOUND -- `trusted_outbound=False`) and the real
    Sent-mail folder (`gmail_sent_mailbox`, `trusted_outbound=True`)
    every run, INBOX first. Only messages fetched from the Sent folder
    are ever trusted as OUTBOUND. Both mailboxes share the same
    account_key/dedup namespace (their (mailbox, uid_validity, uid)
    identities are independent, so no collision risk); a persistence
    error inside either sync is already isolated PER-MESSAGE by
    `GmailInboxService.sync` itself (counted as `failed`, never aborts
    that mailbox's own run) -- but a MAILBOX-level failure (e.g. the
    IMAP connection itself failing) still propagates out of this
    function unmodified, exactly as it always has; the SAME two mailbox
    syncs are re-used, isolated per-mailbox instead, by Stage 8D's own
    `app.services.automation_gmail.prepare_gmail_sync`.
    """
    if not is_configured(settings.gmail_username) or not is_configured(settings.gmail_app_password):
        raise CollectorNotConfiguredError(
            "Gmail inbox sync is not configured: set GMAIL_USERNAME and GMAIL_APP_PASSWORD."
        )

    account_key = normalize_account_key(settings.gmail_username)

    inbox_result = await sync_mailbox(
        db, settings, account_key, settings.gmail_mailbox, trusted_outbound=False
    )
    sent_result = await sync_mailbox(
        db, settings, account_key, settings.gmail_sent_mailbox, trusted_outbound=True
    )
    return _sum_gmail_sync_results(inbox_result, sent_result)
