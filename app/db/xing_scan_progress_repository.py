"""Codex gate follow-up (Astra R4A, XING starvation MEDIUM): persistence
for `XingScanProgressRecord` -- the durable IMAP UID watermark that lets
`app.services.collector_runner.run_xing` skip a confirmed-handled prefix
of a mailbox WITHOUT any per-message IMAP round trip on later runs. See
`app.db.models.XingScanProgressRecord`'s own docstring for the full
rationale, including why an IMAP UID is a safe watermark where AUD-004
already proved a PostgreSQL row id is not.

Mirrors `app.db.automation_mail_progress_repository`'s INSERT +
IntegrityError-catch idiom for first-row creation, AND (Codex gate
follow-up, Astra R4A MEDIUM take 3: UIDVALIDITY CAS) that same module's
`_cas_advance` idea of a `WHERE`-clause compare-and-swap against the
caller's own observed baseline, rather than trusting a Python-side
branch on a possibly-stale in-process read. XING has exactly one
configured mailbox and `run_xing` is never invoked concurrently for it
within this project's automation model (unlike Gmail's per-account
fan-out) -- but the advance is still made monotonic/CAS-safe at the SQL
level as defense in depth: it can only ever move `confirmed_upto_uid`
forward within an epoch, or transition to a new `UIDVALIDITY` epoch via
a CAS against the epoch the caller actually observed, never silently
regressing either one under an unexpected concurrent overlap. See
`_advance_existing`'s own docstring for the exact races this closes.
"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
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

    **Serialization (Codex gate follow-up, Astra R4A MEDIUM take 2).** The
    four fields are encoded via `json.dumps` of an explicit list, not
    plain `":"`-joined string interpolation -- a naive
    `f"{host}:{port}:{username}:{mailbox}"` is ambiguous whenever any
    field can itself contain the `":"` delimiter: e.g. `username="ab:cd"`,
    `mailbox="ef"` and `username="ab"`, `mailbox="cd:ef"` both concatenate
    to the identical `"...:ab:cd:ef"` tail, so two genuinely different
    mailbox configurations would silently hash to the SAME scope --
    exactly the cross-mailbox collision this key exists to prevent.
    `json.dumps` escapes embedded delimiter/quote characters within each
    string field and keeps `imap_port` encoded as a distinct JSON number
    (not a string that could itself be mistaken for part of a
    neighboring field), so distinct tuples always serialize to distinct
    strings.

    **Case folding.** `imap_host` is still lowercased (DNS hostnames are
    case-insensitive). `username` is deliberately NOT lowercased --
    unlike a hostname, an IMAP username's local-part is not guaranteed
    case-insensitive by every server, so folding case here could
    silently collapse two operators' distinct mailboxes (or hide a
    genuine reconfiguration) into one scope. `mailbox` keeps its prior
    `.lower()` fold; it is always `"INBOX"` in practice (see
    `XingEmailCollector`'s hardcoded `client.select("INBOX", ...)` call),
    so this has no observable effect today.

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
    fingerprint = json.dumps(
        [imap_host.strip().lower(), imap_port, username.strip(), mailbox.strip().lower()],
        separators=(",", ":"),
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
    observed_uid_validity: int | None,
    source: str = XING_SCAN_PROGRESS_SOURCE,
) -> None:
    """Persist how far this run confirmed the scan can safely resume from.

    `uid_validity`/`confirmed_upto_uid` are the caller's freshly computed
    values for THIS run (see `run_xing`'s own contiguous-prefix
    computation) -- never partial/unvalidated data. `observed_uid_validity`
    is the epoch the caller read from THIS row (via `get_xing_scan_progress`)
    BEFORE it decided `scan_from_uid`/computed these new values -- i.e. the
    epoch this write is contingent on, not necessarily the row's current
    state (which may have moved on concurrently). Semantics:

    - No row yet: create one with the given values.
    - `uid_validity != observed_uid_validity` (mailbox recreated, as THIS
      caller understood it): the old watermark is no longer meaningful for
      the new UID space -- overwrite both columns with the given values,
      but (Codex gate follow-up, Astra R4A MEDIUM take 3: UIDVALIDITY CAS)
      ONLY if the row is still in the `observed_uid_validity` epoch this
      caller actually read -- see `_advance_existing`'s own docstring for
      why a broad "differs from the new value" check is unsafe here.
    - `uid_validity == observed_uid_validity` (same epoch): only move
      `confirmed_upto_uid` forward, never backward -- a run that (for
      whatever reason) computed a smaller confirmed prefix than a
      previous run already persisted must never regress the watermark
      and force wasted rescanning.

    Codex gate follow-up (Astra R4A MEDIUM, concurrency): both guards are
    enforced by `_advance_existing`'s `UPDATE ... WHERE` clause itself,
    evaluated by the database against the row's CURRENT committed state
    at execution time -- not by this function's own `existing` snapshot,
    which may already be stale under a concurrent writer. See
    `_advance_existing`'s own docstring.
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
                _advance_existing(
                    db,
                    existing,
                    uid_validity=uid_validity,
                    new=confirmed_upto_uid,
                    observed_uid_validity=observed_uid_validity,
                )
        return

    _advance_existing(
        db,
        existing,
        uid_validity=uid_validity,
        new=confirmed_upto_uid,
        observed_uid_validity=observed_uid_validity,
    )


def _uid_validity_eq_clause(value: int | None):
    """`col IS NULL` for `value is None`, `col == value` otherwise --
    plain SQL `= NULL` never matches (unlike Python's `is None`), so this
    must be explicit. Mirrors
    `app.db.automation_mail_progress_repository._cas_advance`'s identical
    NULL-handling idiom for its own CAS `WHERE` clause.
    """
    col = XingScanProgressRecord.uid_validity
    return col.is_(None) if value is None else col == value


def _advance_existing(
    db: Session,
    existing: XingScanProgressRecord,
    *,
    uid_validity: int | None,
    new: int | None,
    observed_uid_validity: int | None,
) -> None:
    """Atomically/monotonically advance `existing`'s row via a single
    conditional `UPDATE ... WHERE` (Codex gate follow-up, Astra R4A
    MEDIUM: concurrency; take 3: UIDVALIDITY CAS).

    **The race this closes (concurrency, take 2).** An earlier version
    branched in Python on `existing.uid_validity`/`existing.
    confirmed_upto_uid` -- a snapshot read by the caller BEFORE this
    function runs -- and then issued an unconditional `UPDATE ... WHERE
    id = :id`. Two concurrent writers that both read the same stale
    snapshot each independently decide "my new value is forward
    progress" and both issue an unconditional UPDATE; whichever COMMITS
    LAST wins regardless of which value is actually larger, silently
    regressing the watermark. Fixed by moving the decision INTO the
    `WHERE` clause, evaluated against the row's live committed state.

    **The race THIS still left open (UIDVALIDITY CAS, take 3).** That
    fix's reset condition was "stored `uid_validity` differs from the
    NEW value" -- too broad: it also matches a row that has ALREADY been
    advanced to a NEWER epoch than this writer knows about. Concretely:
    worker A observes epoch 1, decides to reset to epoch 2 (mailbox
    recreated) with watermark 10, and commits. A stale worker B -- which
    also observed epoch 1 before A committed -- separately decides to
    reset to (its own, different) epoch with watermark 8. Under the
    broad condition, B's target epoch differs from the NOW-current
    stored epoch (2), so `validity_changed` is STILL true and B's UPDATE
    would overwrite A's epoch-2 row right back down to B's stale epoch
    and a lower watermark -- silently losing A's already-confirmed
    progress and resurrecting an epoch that no longer matches the actual
    mailbox.

    **The fix.** The reset branch is now a genuine compare-and-swap
    against `observed_uid_validity` -- the epoch THIS caller actually
    read before computing `uid_validity`/`new` -- not against the target
    `uid_validity` it wants to write. The UPDATE's `WHERE` only matches
    if the row's live `uid_validity` still equals `observed_uid_validity`
    exactly: a stale writer whose observed epoch has already been
    superseded (by ANY newer state, whether that's a further epoch
    change or just a same-epoch watermark advance the writer never saw)
    no-ops instead of restoring stale state. The same-epoch monotonic
    branch additionally re-checks `uid_validity == observed_uid_validity`
    against the row's live state too, guarding against a concurrent
    epoch transition landing between this writer's read and this UPDATE.
    """
    if uid_validity == observed_uid_validity:
        # Same epoch as this worker's own baseline: forward-only advance
        # -- but the WHERE still re-confirms the row is STILL at that
        # baseline epoch (not just "any" epoch), so a concurrent reset
        # that lands in between causes this UPDATE to no-op rather than
        # writing a confirmed_upto_uid that belongs to the OLD epoch.
        if new is None:
            return
        condition = and_(
            _uid_validity_eq_clause(observed_uid_validity),
            or_(
                XingScanProgressRecord.confirmed_upto_uid.is_(None),
                XingScanProgressRecord.confirmed_upto_uid < new,
            ),
        )
    else:
        # Epoch transition: CAS against the OLD epoch this worker
        # actually observed. Succeeds only if the row is still in that
        # expected prior epoch -- a row already moved to a newer epoch
        # (by this same transition or any other writer) must never be
        # clobbered back to a stale one.
        condition = _uid_validity_eq_clause(observed_uid_validity)

    db.execute(
        update(XingScanProgressRecord)
        .where(XingScanProgressRecord.id == existing.id, condition)
        .values(
            uid_validity=uid_validity,
            confirmed_upto_uid=new,
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()
