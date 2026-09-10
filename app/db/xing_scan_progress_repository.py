"""Codex gate follow-up (Astra R4A, XING starvation MEDIUM): persistence
for `XingScanProgressRecord` -- the durable IMAP UID watermark that lets
`app.services.collector_runner.run_xing` skip a confirmed-handled prefix
of a mailbox WITHOUT any per-message IMAP round trip on later runs. See
`app.db.models.XingScanProgressRecord`'s own docstring for the full
rationale, including why an IMAP UID is a safe watermark where AUD-004
already proved a PostgreSQL row id is not.

Mirrors `app.db.automation_mail_progress_repository`'s INSERT +
IntegrityError-catch idiom for first-row creation. Unlike that module's
CAS primitives, `advance_xing_scan_progress` does not need an
`expected_cursor` compare-and-swap: XING has exactly one configured
mailbox and `run_xing` is never invoked concurrently for it within this
project's automation model (unlike Gmail's per-account fan-out), and the
update itself is monotonic-safe by construction (see below) even under
an unexpected overlap -- it can only ever move `confirmed_upto_uid`
forward or reset it on a genuine `UIDVALIDITY` change, never backward.
"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import XingScanProgressRecord

XING_SCAN_PROGRESS_SOURCE = "xing"


def compute_mailbox_scope(
    imap_host: str, imap_port: int, username: str, mailbox: str = "INBOX"
) -> str:
    """Deterministic, non-secret key identifying the actual configured
    mailbox a scan-progress row belongs to (Codex gate follow-up, Astra
    R4A MEDIUM: mailbox scope).

    **The problem this closes.** `XingScanProgressRecord` was previously
    keyed by `source` alone, and XING has exactly one row for that source
    ("xing") no matter which mailbox is actually configured. `UIDVALIDITY`
    is only guaranteed unique WITHIN one mailbox across its own history --
    two different mailboxes (different host, account, or folder) can
    coincidentally report the same `UIDVALIDITY` value. If an operator
    repoints `XING_MAILBOX_*` at a different mailbox that happens to share
    a `UIDVALIDITY` with the old one, the stored watermark would pass the
    existing `uid_validity == expected_uid_validity` check and skip a UID
    prefix in the NEW mailbox that was never actually scanned there --
    silently losing messages, exactly the class of bug `uid_validity`
    itself exists to prevent for a single mailbox recreated in place.

    Scoping every progress row by `(source, mailbox_scope)` instead closes
    that gap: changing host/account/mailbox always changes this key, so a
    new mailbox always starts fresh regardless of any UIDVALIDITY
    coincidence.

    Hashed (never the raw host/account string) so no mailbox identity
    (which, for `username`, is normally an email address) is stored in
    the clear in this table -- deliberately NOT a secret (unlike the App
    Password, which this module never touches), but there is no reason to
    persist it in plaintext either when a deterministic hash serves the
    same scoping purpose. sha256 (not a keyed/secret hash) is appropriate
    here: this key only needs to be deterministic and collision-resistant
    across distinct mailboxes, not resistant to a deliberate attacker
    trying to forge a collision.
    """
    fingerprint = (
        f"{imap_host.strip().lower()}:{imap_port}:"
        f"{username.strip().lower()}:{mailbox.strip().lower()}"
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]


def get_xing_scan_progress(
    db: Session, *, mailbox_scope: str, source: str = XING_SCAN_PROGRESS_SOURCE
) -> XingScanProgressRecord | None:
    """The mailbox's progress row, if one has ever been created. Returns
    `None` for a brand-new installation -- callers treat that exactly
    like `uid_validity`/`confirmed_upto_uid` both being unset (scan the
    full search window, same as before this fix existed).

    `mailbox_scope` (Codex gate follow-up, Astra R4A MEDIUM) restricts the
    lookup to the row for THIS configured mailbox -- see
    `compute_mailbox_scope`'s own docstring for why `source` alone is not
    a safe key.
    """
    return db.scalar(
        select(XingScanProgressRecord).where(
            XingScanProgressRecord.source == source,
            XingScanProgressRecord.mailbox_scope == mailbox_scope,
        )
    )


def advance_xing_scan_progress(
    db: Session,
    *,
    uid_validity: int | None,
    confirmed_upto_uid: int | None,
    mailbox_scope: str,
    source: str = XING_SCAN_PROGRESS_SOURCE,
) -> None:
    """Persist how far this run confirmed the scan can safely resume from.

    `uid_validity`/`confirmed_upto_uid` are the caller's freshly computed
    values for THIS run (see `run_xing`'s own contiguous-prefix
    computation) -- never partial/unvalidated data. Semantics:

    - No row yet: create one with the given values.
    - Stored `uid_validity` differs from the given one (mailbox
      recreated): the old watermark is no longer meaningful for the new
      UID space -- overwrite both columns with the given values
      unconditionally (this is a reset, not an advance).
    - Same `uid_validity`: only move `confirmed_upto_uid` forward, never
      backward -- a run that (for whatever reason) computed a smaller
      confirmed prefix than a previous run already persisted must never
      regress the watermark and force wasted rescanning.
    """
    existing = get_xing_scan_progress(db, mailbox_scope=mailbox_scope, source=source)
    if existing is None:
        record = XingScanProgressRecord(
            source=source,
            mailbox_scope=mailbox_scope,
            uid_validity=uid_validity,
            confirmed_upto_uid=confirmed_upto_uid,
        )
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = get_xing_scan_progress(db, mailbox_scope=mailbox_scope, source=source)
            if existing is not None:
                _advance_existing(db, existing, uid_validity=uid_validity, new=confirmed_upto_uid)
        return

    _advance_existing(db, existing, uid_validity=uid_validity, new=confirmed_upto_uid)


def _advance_existing(
    db: Session,
    existing: XingScanProgressRecord,
    *,
    uid_validity: int | None,
    new: int | None,
) -> None:
    if existing.uid_validity != uid_validity:
        db.execute(
            update(XingScanProgressRecord)
            .where(XingScanProgressRecord.id == existing.id)
            .values(
                uid_validity=uid_validity,
                confirmed_upto_uid=new,
                updated_at=datetime.now(UTC),
            )
        )
        db.commit()
        return

    stored = existing.confirmed_upto_uid
    if new is None:
        return
    if stored is not None and stored >= new:
        return
    db.execute(
        update(XingScanProgressRecord)
        .where(XingScanProgressRecord.id == existing.id)
        .values(confirmed_upto_uid=new, updated_at=datetime.now(UTC))
    )
    db.commit()
