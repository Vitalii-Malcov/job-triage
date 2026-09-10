"""Gmail inbox persistence (Stage 7A) — idempotent, account-scoped
message/thread storage.

**Dedup identity is `(account_key, mailbox, uid_validity, uid)`** (see
GmailMessageRecord's docstring for why `account_key` is part of it —
GMAIL-002), enforced by a DB UNIQUE constraint, not just a Python
SELECT-then-INSERT: `upsert_message` always re-queries after an
`IntegrityError` rather than assuming its own insert won, so two
concurrent syncs racing to persist the same message can never create two
rows for it (spec section 22).

**Threading is neutral, not Gmail-native, and account-scoped** —
`resolve_thread_anchor` derives a thread grouping purely from
Message-ID/In-Reply-To/References headers, and every thread lookup is
scoped to `account_key` (GMAIL-002) so two different configured mailbox
accounts can never merge or collide threads merely because they
reference the same Message-ID string.

**Message-ID ownership is a DB-enforced atomic claim, not a Python
check-then-act decision (GMAIL-011).** A message that self-anchors on
its own Message-ID (no References/In-Reply-To) atomically claims
`(account_key, message_id_header)` in `GmailMessageIdClaimRecord` via
INSERT + IntegrityError-catch — never a SELECT-then-decide sequence,
which a Codex concurrency probe proved could let two concurrent
unrelated messages sharing a reused Message-ID both "win" and get
silently merged into one thread. See `_claim_message_id_or_get_collision_thread`
and `GmailMessageIdClaimRecord`'s own docstring for the full design and
the permanent `contested` flag it sets once a collision is ever proven.
See app/providers/email/imap.py's module docstring for the documented
References/In-Reply-To limitation this is independent of.

Nothing in this module logs message content (subject/body/addresses) —
see app/services/gmail_inbox.py for the sync orchestration and its
privacy-safe logging.
"""

import json
import time
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    GmailMessageIdClaimRecord,
    GmailMessageRecord,
    GmailPermanentSkipRecord,
    GmailThreadRecord,
)
from app.models.gmail import (
    GmailAttachment,
    GmailMessage,
    GmailMessageSummary,
    GmailThread,
    GmailThreadDetail,
)
from app.providers.email.base import ParsedGmailMessage

# GMAIL-013 (thread detail readiness): a bounded default/maximum for how
# many of a thread's messages GET /gmail/threads/{id} returns inline —
# mirrors the GMAIL_DEFAULT_LIST_LIMIT/GMAIL_MAX_LIST_LIMIT pattern used
# for the top-level list endpoints (app/api/routes.py), scoped smaller
# since a single thread is expected to hold far fewer messages than the
# whole mailbox.
THREAD_DETAIL_DEFAULT_MESSAGE_LIMIT = 50
THREAD_DETAIL_MAX_MESSAGE_LIMIT = 200


class GmailRepositoryConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is
    violated anyway — e.g. reloading the row a UNIQUE constraint
    IntegrityError implies must exist comes back None. No code path in
    this project deletes GmailThreadRecord/GmailMessageRecord rows, so
    this should be unreachable; raised instead of silently returning None
    and letting a caller crash later with an unrelated AttributeError
    (mirrors app.db.repositories.CompanyResearchConsistencyError).
    """


# S7E-013 (Codex re-review, final safety fix): a generic Gmail-thread
# mutual-exclusion primitive — see `acquire_thread_lock`'s docstring.
# `_THREAD_LOCK_TTL_SECONDS` bounds how long a crashed holder (a sync run
# or a follow-up send that died mid-critical-section) can block everyone
# else — comfortably above how long either side's own guarded section
# should ever realistically take, so a live holder is never preempted by
# its own TTL under normal operation.
THREAD_LOCK_TTL_SECONDS = 30.0
THREAD_LOCK_DEFAULT_MAX_WAIT_SECONDS = 5.0
THREAD_LOCK_POLL_INTERVAL_SECONDS = 0.05


class GmailThreadLockTimeoutError(Exception):
    """`wait_for_thread_lock` could not acquire the thread's guard within
    `max_wait_seconds` — the thread is currently held by another writer
    (a concurrent Gmail sync, or a concurrent Stage 7E follow-up send).
    Callers fail closed on this rather than proceeding unguarded.
    """


def acquire_thread_lock(
    db: Session, thread_id: int, *, holder: str, ttl_seconds: float = THREAD_LOCK_TTL_SECONDS
) -> bool:
    """Single CAS attempt to claim `thread_id`'s guard for `holder` — the
    sole arbiter of "who may currently mutate/read-for-mutation this
    Gmail thread's correspondence". Succeeds (returns True) if the thread
    is currently unlocked, its existing lock has expired (past
    `lock_expires_at` — a crashed holder is never a permanent block), or
    `holder` already holds it (idempotent re-acquire, e.g. a retry loop's
    own successful attempt observed by a caller that re-checks). Returns
    False if a DIFFERENT holder currently holds a live lock.

    Deliberately generic: this function and `GmailThreadRecord` know
    nothing about WHY a caller wants the lock — Stage 7A's
    `upsert_message` and Stage 7E's `app.services.follow_up_send` are its
    two callers today, but this module never imports or reasons about
    job/application concepts (see app/services/gmail_inbox.py's "zero
    job/application linkage" constraint) — it is pure Gmail-thread
    coordination, reusable by anything that needs it.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    result = db.execute(
        update(GmailThreadRecord)
        .where(
            GmailThreadRecord.id == thread_id,
            or_(
                GmailThreadRecord.lock_holder.is_(None),
                GmailThreadRecord.lock_expires_at < now,
                GmailThreadRecord.lock_holder == holder,
            ),
        )
        .values(lock_holder=holder, lock_expires_at=expires_at)
        # This is the only CAS UPDATE in this project that filters on a
        # DateTime column rather than a status string/id/boolean.
        # SQLAlchemy's default in-session "evaluate" synchronization
        # would otherwise re-check this WHERE clause in Python against
        # any already-loaded GmailThreadRecord in the session's identity
        # map — and SQLite (unlike Postgres) does not round-trip tzinfo
        # through `DateTime(timezone=True)`, so a previously-loaded
        # `lock_expires_at` can come back naive and raise comparing it
        # against `now` (aware). `synchronize_session=False` lets the
        # database evaluate the WHERE clause instead (a plain string
        # comparison for SQLite's ISO-formatted storage, unaffected by
        # tzinfo), which is correct here regardless: this function never
        # relies on any in-session object being auto-synced by this call.
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def renew_thread_lock(
    db: Session, thread_id: int, *, holder: str, ttl_seconds: float = THREAD_LOCK_TTL_SECONDS
) -> bool:
    """S7E-016 (Codex re-review, correctness fix): the DEDICATED CAS a
    lease-renewal heartbeat (`app.services.follow_up_send._ThreadLockHeartbeat`)
    must use instead of `acquire_thread_lock` — the two are deliberately
    NOT interchangeable.

    `acquire_thread_lock` treats an EXPIRED lease as free-for-the-taking
    by design (`lock_expires_at < now` is one of its success conditions)
    — that is exactly the crash-recovery property initial/recovery
    acquisition needs (see its own docstring). A heartbeat calling THAT
    function to "renew" would, on an expired lease, silently succeed and
    resume as if ownership had been continuous — but it was NOT: the
    lease lapsed, and for however long that gap lasted, NOTHING actually
    protected the thread (a Gmail sync's `upsert_message` could freely
    have acquired and released it in between, entirely unnoticed). A
    renewal proving continuous ownership must therefore REQUIRE the
    lease to still be live at the moment of renewal — this function's
    WHERE clause adds `lock_expires_at >= now` on top of `lock_holder ==
    holder`, so it returns False the instant the lease has expired, even
    if no one else has acquired it yet. `app.services.follow_up_send`
    treats any False return as an unrecoverable loss of exclusivity —
    the heartbeat stops immediately and never falls back to calling
    `acquire_thread_lock` to "reacquire"; a gap once opened is a gap,
    regardless of whether anyone else happened to walk through it.

    Returns True only if EXACTLY one row was updated.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    result = db.execute(
        update(GmailThreadRecord)
        .where(
            GmailThreadRecord.id == thread_id,
            GmailThreadRecord.lock_holder == holder,
            GmailThreadRecord.lock_expires_at >= now,
        )
        .values(lock_expires_at=expires_at)
        # See acquire_thread_lock's identical execution_options comment —
        # this is the second CAS UPDATE in this project filtering on a
        # DateTime column, subject to the same SQLite naive/aware
        # in-session-evaluator hazard.
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def release_thread_lock(db: Session, thread_id: int, *, holder: str) -> None:
    """Release `thread_id`'s guard — a no-op (never raises) if `holder`
    doesn't currently hold it (e.g. it already expired and was reclaimed
    by someone else; releasing must never accidentally clear a DIFFERENT
    holder's live lock).
    """
    db.execute(
        update(GmailThreadRecord)
        .where(GmailThreadRecord.id == thread_id, GmailThreadRecord.lock_holder == holder)
        .values(lock_holder=None, lock_expires_at=None)
    )
    db.commit()


def wait_for_thread_lock(
    db: Session,
    thread_id: int,
    *,
    holder: str,
    ttl_seconds: float = THREAD_LOCK_TTL_SECONDS,
    max_wait_seconds: float = THREAD_LOCK_DEFAULT_MAX_WAIT_SECONDS,
    poll_interval_seconds: float = THREAD_LOCK_POLL_INTERVAL_SECONDS,
) -> None:
    """Block (via short, bounded polling — there is no cross-connection
    wait/notify primitive available here) until `acquire_thread_lock`
    succeeds, or raise `GmailThreadLockTimeoutError` after
    `max_wait_seconds`. Always attempts at least once immediately before
    ever sleeping.
    """
    deadline = time.monotonic() + max_wait_seconds
    while True:
        if acquire_thread_lock(db, thread_id, holder=holder, ttl_seconds=ttl_seconds):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GmailThreadLockTimeoutError(
                f"Timed out waiting for the lock on gmail_thread_id={thread_id!r}"
            )
        time.sleep(min(poll_interval_seconds, remaining))


def new_thread_lock_holder_token(prefix: str) -> str:
    """A per-attempt-unique holder identity — never reused across
    different logical attempts, so a stale/expired lock's own former
    holder string can never be mistaken for a currently-live one by
    coincidence.
    """
    return f"{prefix}:{uuid.uuid4().hex}"


def resolve_thread_anchor(
    message_id_header: str | None,
    in_reply_to: str | None,
    references: tuple[str, ...],
) -> str | None:
    """The thread-grouping key a message resolves to, or None if it has no
    usable Message-ID/In-Reply-To/References at all (caller falls back to
    a synthetic per-message key — see `upsert_message`).

    `references[0]` (the oldest ancestor a mail client recorded) is
    preferred over `in_reply_to` (the immediate parent only) so that,
    regardless of fetch order, every message in a thread that consistently
    carries References resolves to the SAME anchor: the root message's own
    Message-ID (its thread key, when the root itself is later parsed, is
    its own Message-ID — i.e. references[0] again). A message with
    In-Reply-To but no References anchors to its immediate parent's
    Message-ID instead — see the documented limitation in
    app/providers/email/imap.py's module docstring for what this means
    when a whole subtree of replies drops References.
    """
    if references:
        return references[0]
    if in_reply_to:
        return in_reply_to
    return message_id_header


def is_message_known(
    db: Session, account_key: str, mailbox: str, uid_validity: int, uid: int
) -> bool:
    """True if this exact provider identity is already persisted.

    A plain single-UID convenience wrapper around
    `get_message_by_identity` — NOT what `GmailImapProvider` is wired to
    (see `get_known_uids` below, GMAIL-012): calling this once per
    candidate UID is exactly the query-per-UID amplification a Codex
    probe reproduced as 100 SEARCH results -> 100 SELECTs, before
    MAX_MESSAGES_PER_SYNC was even applied. Kept as a small, honestly
    named utility for call sites that only ever need to check one UID.
    """
    return get_message_by_identity(db, account_key, mailbox, uid_validity, uid) is not None


# GMAIL-012: chunk size for get_known_uids' bulk membership queries — kept
# comfortably under SQLite's default SQLITE_MAX_VARIABLE_NUMBER (999) so a
# single chunk's `IN (...)` clause never risks that limit, while still
# being large enough that a real sync's candidate list (bounded well
# below this by MAX_MESSAGES_PER_SYNC's own order of magnitude) needs only
# one or two chunk queries, not hundreds.
KNOWN_UIDS_QUERY_CHUNK_SIZE = 500


def get_known_uids(
    db: Session,
    account_key: str,
    mailbox: str,
    uid_validity: int,
    candidate_uids: Collection[int],
) -> set[int]:
    """Bulk membership check (GMAIL-012): which of `candidate_uids` should
    never be fetched again for this (account_key, mailbox, uid_validity)
    generation — either already persisted, OR (FINAL-004, Astra R5A)
    permanently skipped because the message's own content makes it
    unpersistable (see `GmailPermanentSkipRecord`'s docstring for the
    starvation bug this second half closes).

    Bound into `GmailImapProvider` via closure as `get_known_uids` (see
    app/services/gmail_sync.py's `make_gmail_provider`) so the provider
    can filter these UIDs out BEFORE applying MAX_MESSAGES_PER_SYNC
    (GMAIL-005 starvation fix — see app/providers/email/imap.py's
    `_fetch_sync_body`), using ONE query per table per
    `KNOWN_UIDS_QUERY_CHUNK_SIZE`-sized chunk of candidates — never one
    query per UID, regardless of how many UIDs IMAP SEARCH returns.
    """
    candidates = list(dict.fromkeys(candidate_uids))  # de-dup, preserve order
    known: set[int] = set()
    for start in range(0, len(candidates), KNOWN_UIDS_QUERY_CHUNK_SIZE):
        chunk = candidates[start : start + KNOWN_UIDS_QUERY_CHUNK_SIZE]
        rows = db.scalars(
            select(GmailMessageRecord.uid).where(
                GmailMessageRecord.account_key == account_key,
                GmailMessageRecord.mailbox == mailbox,
                GmailMessageRecord.uid_validity == uid_validity,
                GmailMessageRecord.uid.in_(chunk),
            )
        ).all()
        known.update(rows)
        skip_rows = db.scalars(
            select(GmailPermanentSkipRecord.uid).where(
                GmailPermanentSkipRecord.account_key == account_key,
                GmailPermanentSkipRecord.mailbox == mailbox,
                GmailPermanentSkipRecord.uid_validity == uid_validity,
                GmailPermanentSkipRecord.uid.in_(chunk),
            )
        ).all()
        known.update(skip_rows)
    return known


def record_permanent_skips(
    db: Session,
    account_key: str,
    mailbox: str,
    uid_validity: int,
    skips: Collection[tuple[int, str]],
) -> None:
    """FINAL-004 (Astra R5A): durably records each `(uid, reason)` in
    `skips` as permanently unfetchable for this (account_key, mailbox,
    uid_validity) generation — see `GmailPermanentSkipRecord`'s docstring
    for why this is required to make forward progress past a prefix of
    permanently-bad messages, and `get_known_uids` above for how these
    rows are excluded from future candidate lists.

    Each UID is inserted and committed independently (mirrors
    `upsert_message`'s own per-row isolation): one UID's insert failure
    (including a UNIQUE-constraint collision from a concurrent or
    repeated sync recording the SAME uid — expected and harmless, not an
    error) never prevents the others from being recorded, and never
    rolls back any message persistence this sync run already committed
    via `upsert_message`. Callers (`app.services.gmail_inbox
    .GmailInboxService.sync`) treat a total failure here as best-effort:
    it can never turn an otherwise-successful sync into a failure, only
    mean this same UID is (harmlessly, just wastefully) reconsidered on
    the next run.
    """
    for uid, reason in skips:
        record = GmailPermanentSkipRecord(
            account_key=account_key,
            mailbox=mailbox,
            uid_validity=uid_validity,
            uid=uid,
            reason=reason,
        )
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            # Already recorded (a prior run, or a concurrent one) — not
            # an error, nothing further to do for this uid.
            db.rollback()


def get_message_by_identity(
    db: Session, account_key: str, mailbox: str, uid_validity: int, uid: int
) -> GmailMessageRecord | None:
    """Pure read by the DB-enforced dedup identity — never mutates."""
    return db.scalar(
        select(GmailMessageRecord).where(
            GmailMessageRecord.account_key == account_key,
            GmailMessageRecord.mailbox == mailbox,
            GmailMessageRecord.uid_validity == uid_validity,
            GmailMessageRecord.uid == uid,
        )
    )


def get_or_create_thread(
    db: Session, account_key: str, thread_key: str, subject: str
) -> GmailThreadRecord:
    """Race-safe get-or-create on `GmailThreadRecord`'s
    `(account_key, thread_key)` UNIQUE constraint: a plain SELECT first,
    then INSERT, then — if a concurrent creator won the race in between —
    catch the IntegrityError and reload the winning row instead of
    raising. Mirrors the create/catch/reload idiom used throughout
    app.db.repositories (e.g. CompanyResearchIdentityAlias).
    """
    existing = db.scalar(
        select(GmailThreadRecord).where(
            GmailThreadRecord.account_key == account_key,
            GmailThreadRecord.thread_key == thread_key,
        )
    )
    if existing is not None:
        return existing

    record = GmailThreadRecord(account_key=account_key, thread_key=thread_key, subject=subject)
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(GmailThreadRecord).where(
                GmailThreadRecord.account_key == account_key,
                GmailThreadRecord.thread_key == thread_key,
            )
        )
        if existing is None:
            raise GmailRepositoryConsistencyError(
                f"Expected a gmail_threads row for account_key={account_key!r} "
                f"thread_key={thread_key!r} after a UNIQUE constraint collision, "
                "but none was found."
            ) from None
        return existing

    db.refresh(record)
    return record


def _get_message_id_claim(
    db: Session, account_key: str, message_id_header: str
) -> GmailMessageIdClaimRecord | None:
    return db.scalar(
        select(GmailMessageIdClaimRecord).where(
            GmailMessageIdClaimRecord.account_key == account_key,
            GmailMessageIdClaimRecord.message_id_header == message_id_header,
        )
    )


def _mark_claim_contested(db: Session, account_key: str, message_id_header: str) -> None:
    """Permanently flag a Message-ID as proven ambiguous (GMAIL-011) —
    idempotent (safe to call redundantly if multiple losers race).
    """
    stmt = (
        update(GmailMessageIdClaimRecord)
        .where(
            GmailMessageIdClaimRecord.account_key == account_key,
            GmailMessageIdClaimRecord.message_id_header == message_id_header,
        )
        .values(contested=True)
    )
    db.execute(stmt)
    db.commit()


def _same_claimant_identity(claim: GmailMessageIdClaimRecord, parsed: ParsedGmailMessage) -> bool:
    """True if `claim`'s recorded owner IS `parsed`'s own provider
    identity — i.e. this is a concurrent/later retry of the exact same
    message, not a different message that happens to share a Message-ID.
    """
    return (
        claim.claimant_mailbox == parsed.mailbox
        and claim.claimant_uid_validity == parsed.uid_validity
        and claim.claimant_uid == parsed.uid
    )


def _claim_message_id_or_get_collision_thread(
    db: Session, parsed: ParsedGmailMessage, anchor: str
) -> GmailThreadRecord:
    """Atomically claim `(account_key, anchor)` for `parsed` — the sole
    arbiter of self-anchored Message-ID ownership (GMAIL-011).

    Reuses an existing `GmailThreadRecord` at this `thread_key` if one
    already exists (e.g. created by a reply that arrived before this
    root — see `_resolve_thread_for_message`'s reply branch), otherwise
    creates one. Either way, the actual ownership decision is made by
    the claim INSERT below, never by whichever caller happened to see
    "no thread yet" first.

    If the claim INSERT fails, the existing claim is reloaded and
    classified — never assumed to be a collision outright:

    - **Same provider identity** (mailbox/uid_validity/uid all match
      `parsed`'s own): this is a concurrent or later retry of the exact
      same message racing against itself (e.g. two overlapping sync
      runs), not ambiguity. The existing claim's thread is reused as-is;
      `contested` is never touched.
    - **Different provider identity**: a genuinely different message
      already legitimately owns this exact Message-ID — this message's
      claim to share it is unverifiable and must not be trusted as proof
      of shared conversation. The winning claim is marked permanently
      `contested` (see `_resolve_thread_for_message`) and this message is
      routed to its own brand-new synthetic thread — it never joins the
      winner's thread just because it momentarily observed the same
      (about-to-be-superseded) thread row.
    """
    existing_thread = db.scalar(
        select(GmailThreadRecord).where(
            GmailThreadRecord.account_key == parsed.account_key,
            GmailThreadRecord.thread_key == anchor,
        )
    )
    thread = existing_thread or get_or_create_thread(db, parsed.account_key, anchor, parsed.subject)

    claim = GmailMessageIdClaimRecord(
        account_key=parsed.account_key,
        message_id_header=anchor,
        claimant_mailbox=parsed.mailbox,
        claimant_uid_validity=parsed.uid_validity,
        claimant_uid=parsed.uid,
        thread_id=thread.id,
    )
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing_claim = _get_message_id_claim(db, parsed.account_key, anchor)
        if existing_claim is None:
            raise GmailRepositoryConsistencyError(
                f"Expected a gmail_message_id_claims row for account_key="
                f"{parsed.account_key!r} message_id_header={anchor!r} after a "
                "UNIQUE constraint collision, but none was found."
            ) from None

        if _same_claimant_identity(existing_claim, parsed):
            # Not a collision — this IS the claim's own owner, racing
            # against itself (concurrent retry of the same message).
            # Reuse its thread untouched; never mark contested.
            owner_thread = db.get(GmailThreadRecord, existing_claim.thread_id)
            if owner_thread is None:
                raise GmailRepositoryConsistencyError(
                    f"gmail_message_id_claims row for account_key={parsed.account_key!r} "
                    f"message_id_header={anchor!r} points at missing thread_id="
                    f"{existing_claim.thread_id}."
                ) from None
            return owner_thread

        # A genuinely different provider identity already owns this
        # Message-ID — mark the winning claim as permanently contested
        # (so future replies referencing this anchor stop trusting it
        # too — see _resolve_thread_for_message) and give this message
        # its own separate, never-shared synthetic thread.
        _mark_claim_contested(db, parsed.account_key, anchor)
        return get_or_create_thread(
            db,
            parsed.account_key,
            f"synthetic-collision:{parsed.mailbox}:{parsed.uid_validity}:{parsed.uid}",
            parsed.subject,
        )

    return thread


def _resolve_thread_for_message(db: Session, parsed: ParsedGmailMessage) -> GmailThreadRecord:
    """The `GmailThreadRecord` `upsert_message` should attach `parsed` to.

    Three cases:
    1. No usable Message-ID/In-Reply-To/References at all: an
       unlinkable singleton, given its own synthetic thread.
    2. The resolved anchor came from `references[0]`/`in_reply_to` (this
       message explicitly points at another message as its
       parent/ancestor): the legitimate, protocol-intended use of
       Message-ID. Joins the thread already claimed for that anchor —
       UNLESS that anchor has been proven `contested` (GMAIL-011: an
       ambiguous Message-ID is never later trusted just because this
       particular reference looks legitimate), in which case this
       message gets its own synthetic thread too. If no claim exists yet
       (the root hasn't been synced), falls back to joining/creating a
       plain thread keyed on the anchor, so a later-arriving root that
       claims the same anchor finds and reuses this same thread (see
       `_claim_message_id_or_get_collision_thread`'s `existing_thread`
       reuse).
    3. The resolved anchor is this message's OWN Message-ID (no
       References/In-Reply-To of its own — it is establishing itself as
       a would-be thread root): resolved via an atomic DB claim, never a
       Python check-then-act decision (GMAIL-011) — see
       `_claim_message_id_or_get_collision_thread`.
    """
    anchor = resolve_thread_anchor(parsed.message_id_header, parsed.in_reply_to, parsed.references)
    if anchor is None:
        synthetic_key = f"synthetic:{parsed.mailbox}:{parsed.uid_validity}:{parsed.uid}"
        return get_or_create_thread(db, parsed.account_key, synthetic_key, parsed.subject)

    self_anchored = (
        anchor == parsed.message_id_header and not parsed.references and not parsed.in_reply_to
    )

    if not self_anchored:
        claim = _get_message_id_claim(db, parsed.account_key, anchor)
        if claim is not None:
            if claim.contested:
                return get_or_create_thread(
                    db,
                    parsed.account_key,
                    f"synthetic-ambiguous-reply:{parsed.mailbox}:{parsed.uid_validity}:{parsed.uid}",
                    parsed.subject,
                )
            thread = db.get(GmailThreadRecord, claim.thread_id)
            if thread is not None:
                return thread
            raise GmailRepositoryConsistencyError(
                f"gmail_message_id_claims row for account_key={parsed.account_key!r} "
                f"message_id_header={anchor!r} points at missing thread_id="
                f"{claim.thread_id}."
            )
        return get_or_create_thread(db, parsed.account_key, anchor, parsed.subject)

    return _claim_message_id_or_get_collision_thread(db, parsed, anchor)


def upsert_message(
    db: Session,
    parsed: ParsedGmailMessage,
    *,
    lock_wait_seconds: float = THREAD_LOCK_DEFAULT_MAX_WAIT_SECONDS,
) -> tuple[GmailMessageRecord, bool]:
    """Persist one parsed message idempotently. Returns (record, created) —
    created=False for an already-persisted
    (account_key, mailbox, uid_validity, uid) identity, in which case
    `record` is the pre-existing row and nothing is written.

    Concurrency: if two syncs race to insert the same new message, the
    loser's INSERT fails on the UNIQUE(account_key, mailbox, uid_validity,
    uid) constraint; caught below, rolled back, and resolved by
    re-reading the winner's row rather than raising or double-inserting.

    **S7E-013 (Codex re-review, final safety fix).** The actual INSERT +
    commit below only ever runs while this call holds `thread.id`'s guard
    (`app.db.gmail_repository.acquire_thread_lock`/`wait_for_thread_lock`)
    — the SAME generic per-thread lock `app.services.follow_up_send`
    holds across its own revalidate-then-dispatch window. This is what
    makes that window airtight: a new `GmailMessageRecord` for a thread a
    follow-up send is currently guarding cannot commit until the send
    releases the guard (success, failure, or crash-bounded TTL expiry),
    and a Gmail sync that got here first blocks a concurrent send from
    ever starting its own guarded window. `GmailThreadLockTimeoutError`
    propagates to the caller (`app.services.gmail_inbox.GmailInboxService.sync`
    already isolates one message's persistence failure from the rest of
    the run — see its own docstring — so a lock timeout here is retried
    on the NEXT sync run exactly like any other one-message failure, not
    a fatal error for the whole sync).
    """
    existing = get_message_by_identity(
        db, parsed.account_key, parsed.mailbox, parsed.uid_validity, parsed.uid
    )
    if existing is not None:
        return existing, False

    thread = _resolve_thread_for_message(db, parsed)

    holder = new_thread_lock_holder_token(
        f"gmail_sync:{parsed.mailbox}:{parsed.uid_validity}:{parsed.uid}"
    )
    wait_for_thread_lock(db, thread.id, holder=holder, max_wait_seconds=lock_wait_seconds)
    try:
        # S7E-011 (Codex re-review): a single `now` shared by both
        # `received_at`'s fallback default and `provider_arrival_at`'s
        # fallback below — when the IMAP layer couldn't determine a real
        # INTERNALDATE (see ParsedGmailMessage.provider_arrival_at's
        # docstring), `provider_arrival_at` degrades to EXACTLY this
        # project's pre-S7E-011 behavior (this sync's own wall-clock
        # write time) rather than a separately-computed,
        # microseconds-divergent "now".
        now = datetime.now(UTC)
        record = GmailMessageRecord(
            thread_id=thread.id,
            account_key=parsed.account_key,
            mailbox=parsed.mailbox,
            uid_validity=parsed.uid_validity,
            uid=parsed.uid,
            message_id_header=parsed.message_id_header,
            in_reply_to=parsed.in_reply_to,
            references_json=json.dumps(list(parsed.references)),
            from_address=parsed.from_address,
            from_display_name=parsed.from_display_name,
            to_addresses_json=json.dumps(list(parsed.to_addresses)),
            cc_addresses_json=json.dumps(list(parsed.cc_addresses)),
            subject=parsed.subject,
            sent_at=parsed.sent_at,
            received_at=now,
            provider_arrival_at=parsed.provider_arrival_at or now,
            # S7E-013: True ONLY for a real, successfully-parsed IMAP
            # INTERNALDATE — never the wall-clock fallback above.
            provider_arrival_is_trusted=parsed.provider_arrival_at is not None,
            direction=parsed.direction,
            body_plain=parsed.body_plain,
            body_truncated=parsed.body_truncated,
            has_html=parsed.has_html,
            attachments_json=json.dumps(
                [
                    {"filename": a.filename, "content_type": a.content_type, "size": a.size}
                    for a in parsed.attachments
                ]
            ),
        )
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = get_message_by_identity(
                db, parsed.account_key, parsed.mailbox, parsed.uid_validity, parsed.uid
            )
            if existing is None:
                raise GmailRepositoryConsistencyError(
                    f"Expected a gmail_messages row for identity "
                    f"(account_key={parsed.account_key!r}, mailbox={parsed.mailbox!r}, "
                    f"uid_validity={parsed.uid_validity}, uid={parsed.uid}) after a "
                    "UNIQUE constraint collision, but none was found."
                ) from None
            return existing, False

        db.refresh(record)
        return record, True
    finally:
        release_thread_lock(db, thread.id, holder=holder)


def get_message_by_id(db: Session, account_key: str, message_id: int) -> GmailMessageRecord | None:
    return db.scalar(
        select(GmailMessageRecord).where(
            GmailMessageRecord.id == message_id,
            GmailMessageRecord.account_key == account_key,
        )
    )


def list_messages(
    db: Session, account_key: str, limit: int, offset: int
) -> list[GmailMessageRecord]:
    stmt = (
        select(GmailMessageRecord)
        .where(GmailMessageRecord.account_key == account_key)
        .order_by(GmailMessageRecord.received_at.desc(), GmailMessageRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


class GmailMessageAutomationCASLostError(Exception):
    """AUD-004 (Astra R3): raised (constructed, never actually thrown --
    see the one caller, `app.services.automation_gmail`) when
    `mark_message_automation_processed` returns `False`: some OTHER
    process already marked this exact message as automation-processed
    between this call's own pipeline run and its attempt to record that.
    The message's own analysis/response-draft work already durably
    committed regardless (idempotent either way) -- only the "who gets to
    report this as newly done" bookkeeping was lost, mirroring
    `app.db.automation_mail_progress_repository.AutomationMailProgressCASLostError`'s
    contract (never folded into a silent "ok").
    """


def list_unprocessed_messages_for_automation(
    db: Session, account_key: str, *, limit: int
) -> list[GmailMessageRecord]:
    """Stage 8D message selection for `gmail_response_drafts` (AUD-004,
    Astra R3 fix). Selects every message for this account that has not
    yet been fully handled (`automation_processed_at IS NULL`) -- NEVER
    `id > watermark`, which this replaces. See
    `GmailMessageRecord.automation_processed_at`'s own docstring for why
    an `id`-ordered watermark is unsafe on PostgreSQL: sequence
    allocation order is not commit-visibility order, so a lower-`id` row
    can become visible strictly AFTER a higher-`id` row a watermark had
    already advanced past -- permanently skipping it. Selecting by this
    row's OWN completion state instead means no other row's `id` or
    processing history can ever cause a message to be skipped.

    `id.asc()` orders the scan (oldest-first is a nice, but non
    -load-bearing, property -- a new installation naturally starts from
    the oldest stored message and catches up) and `limit` bounds one
    run's work exactly like the old `list_messages_by_id_after` did; only
    the INCLUSION predicate changed.
    """
    stmt = (
        select(GmailMessageRecord)
        .where(
            GmailMessageRecord.account_key == account_key,
            GmailMessageRecord.automation_processed_at.is_(None),
        )
        .order_by(GmailMessageRecord.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def mark_message_automation_processed(db: Session, message_id: int) -> bool:
    """Atomic per-message CAS (AUD-004, Astra R3): `UPDATE gmail_messages
    SET automation_processed_at = :now WHERE id = :message_id AND
    automation_processed_at IS NULL`. Returns True iff exactly one row
    was updated -- i.e. THIS call is the one that durably recorded this
    message as processed. False means it was already marked (a
    concurrent/duplicate processing attempt, or a retry racing a prior
    successful mark) -- the caller
    (`app.services.automation_gmail.prepare_gmail_response_drafts`)
    treats that exactly like `AutomationMailProgressCASLostError` used to:
    a non-"ok" outcome for this message, never silently folded into
    success, even though the underlying analysis/draft work already
    committed either way.
    """
    now = datetime.now(UTC)
    result = db.execute(
        update(GmailMessageRecord)
        .where(
            GmailMessageRecord.id == message_id,
            GmailMessageRecord.automation_processed_at.is_(None),
        )
        .values(automation_processed_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def get_thread_by_id(db: Session, account_key: str, thread_id: int) -> GmailThreadRecord | None:
    return db.scalar(
        select(GmailThreadRecord).where(
            GmailThreadRecord.id == thread_id,
            GmailThreadRecord.account_key == account_key,
        )
    )


def list_threads_with_counts(
    db: Session, account_key: str, limit: int, offset: int
) -> list[tuple[GmailThreadRecord, int]]:
    """GMAIL-008: one grouped/joined aggregate query for the whole page —
    never a separate COUNT query per thread. Returns `(record,
    message_count)` pairs; the number of SQL statements this executes
    stays constant regardless of how many threads are returned.
    """
    stmt = (
        select(GmailThreadRecord, func.count(GmailMessageRecord.id))
        .outerjoin(GmailMessageRecord, GmailMessageRecord.thread_id == GmailThreadRecord.id)
        .where(GmailThreadRecord.account_key == account_key)
        .group_by(GmailThreadRecord.id)
        .order_by(GmailThreadRecord.updated_at.desc(), GmailThreadRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(record, count) for record, count in db.execute(stmt).all()]


def get_thread_message_count(db: Session, account_key: str, thread_id: int) -> int:
    """One COUNT query for exactly one thread — used by the single-thread
    detail endpoint (GET /gmail/threads/{id}), which is not the N+1
    pattern GMAIL-008 addresses (that was the *list* endpoint issuing one
    such query per row; a single detail read issuing one query for its
    one thread is normal and unavoidable).
    """
    return (
        db.scalar(
            select(func.count(GmailMessageRecord.id)).where(
                GmailMessageRecord.thread_id == thread_id,
                GmailMessageRecord.account_key == account_key,
            )
        )
        or 0
    )


def list_messages_for_thread(
    db: Session, account_key: str, thread_id: int, limit: int
) -> list[GmailMessageRecord]:
    """Bounded message list for one thread (GET /gmail/threads/{id},
    section 13 "thread detail API readiness") — never unbounded, and
    always account-scoped.

    S7E-011 (Codex re-review): ordered by `provider_arrival_at` (Gmail's
    own IMAP INTERNALDATE), not `received_at` — see
    app.db.follow_up_repository.get_thread_message_infos's docstring for
    why `received_at` (this project's own sync wall-clock write time) can
    misorder messages first imported together in one INBOX-then-Sent sync
    run.
    """
    stmt = (
        select(GmailMessageRecord)
        .where(
            GmailMessageRecord.thread_id == thread_id,
            GmailMessageRecord.account_key == account_key,
        )
        .order_by(GmailMessageRecord.provider_arrival_at.asc(), GmailMessageRecord.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def to_gmail_message(record: GmailMessageRecord) -> GmailMessage:
    """Full detail conversion — GET /gmail/messages/{id} only (GMAIL-007).
    Includes body_plain/full recipients/references; never used for the
    bulk list endpoint.
    """
    attachments = [GmailAttachment(**item) for item in json.loads(record.attachments_json)]
    return GmailMessage(
        id=record.id,
        thread_id=record.thread_id,
        message_id_header=record.message_id_header,
        in_reply_to=record.in_reply_to,
        references=json.loads(record.references_json),
        from_address=record.from_address,
        from_display_name=record.from_display_name,
        to_addresses=json.loads(record.to_addresses_json),
        cc_addresses=json.loads(record.cc_addresses_json),
        subject=record.subject,
        sent_at=record.sent_at,
        received_at=record.received_at,
        direction=record.direction,
        body_plain=record.body_plain,
        body_truncated=record.body_truncated,
        has_html=record.has_html,
        attachments=attachments,
        created_at=record.created_at,
    )


def to_gmail_message_summary(record: GmailMessageRecord) -> GmailMessageSummary:
    """Compact conversion — GET /gmail/messages (list) and
    GET /gmail/threads/{id} (GMAIL-007). Never includes body_plain, full
    recipient arrays, references, or per-attachment detail.
    """
    attachment_count = len(json.loads(record.attachments_json))
    return GmailMessageSummary(
        id=record.id,
        thread_id=record.thread_id,
        direction=record.direction,
        from_address=record.from_address,
        subject=record.subject,
        sent_at=record.sent_at,
        received_at=record.received_at,
        has_html=record.has_html,
        body_truncated=record.body_truncated,
        attachment_count=attachment_count,
    )


def to_gmail_thread(record: GmailThreadRecord, message_count: int) -> GmailThread:
    """GMAIL-008: `message_count` is supplied by the caller (from
    `list_threads_with_counts`'s single grouped query) — this function
    itself never issues a DB query.
    """
    return GmailThread(
        id=record.id,
        thread_key=record.thread_key,
        subject=record.subject,
        message_count=message_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_gmail_thread_detail(
    db: Session,
    record: GmailThreadRecord,
    message_count: int,
    *,
    message_limit: int = THREAD_DETAIL_DEFAULT_MESSAGE_LIMIT,
) -> GmailThreadDetail:
    """GET /gmail/threads/{id}'s full response — thread header plus a
    bounded, chronologically-ordered list of its messages in summary form
    (section 13). `message_limit` is caller-bounded (see
    THREAD_DETAIL_MAX_MESSAGE_LIMIT in app/api/routes.py).
    """
    messages = list_messages_for_thread(db, record.account_key, record.id, message_limit)
    thread = to_gmail_thread(record, message_count)
    return GmailThreadDetail(
        **thread.model_dump(),
        messages=[to_gmail_message_summary(message) for message in messages],
    )
