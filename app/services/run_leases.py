"""Leaf module shared by the automation cycle and the collector exclusion: which account keys a
RUNNING slot is held under, and how long a lease is extended after an interruption.

It imports nothing from `app.services`, so both `app.services.automation` and
`app.services.collector_exclusion` can use it at module level (they used to reach each other
through function-level imports to avoid an import cycle).
"""

from app.providers.email.base import normalize_account_key
from app.providers.email.imap_deadline import IMAP_SESSION_DEADLINE_SECONDS

# After a cancellation or shutdown the leases are extended to cover the longest a still running
# IMAP worker thread can live, plus a margin.
CANCELLED_LEASE_EXTENSION_SECONDS = IMAP_SESSION_DEADLINE_SECONDS + 60.0


def lock_account_keys(settings) -> tuple[str, ...]:
    """The distinct normalized account keys whose RUNNING slot a manual run
    must hold (sorted); `("",)` when neither identity is configured."""
    # The scheduler persists its key exactly as configured (stripped, case
    # preserved) while the REST automation endpoint persists the normalized
    # Gmail identity: hold both ACTUAL keys so every holder is excluded.
    # The REST key is kept even when empty (Gmail unset): REST automation runs
    # under "" then and must stay excluded.
    keys = {normalize_account_key(settings.gmail_username or "")}
    scheduler_key = (settings.automation_scheduler_account_key or "").strip()
    if scheduler_key:
        keys.add(scheduler_key)
    return tuple(sorted(keys))
