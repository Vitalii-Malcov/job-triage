"""Stage 9D: persistence for the Gmail draft handoff ledger
(`GmailApplicationDraftRecord`) -- one row per exact Stage 9C approval link.

**Every authority-changing transition is a single SQL CAS** whose WHERE
clause names the exact attempt it acts on (id, state, claim token, attempt
count, marker -- as applicable). Ownership is decided by the database
predicate alone, never by a possibly stale ORM object in Python.

**Nothing here commits.** The caller (app.services.gmail_application_draft)
owns every commit, because the commit ACKNOWLEDGMENT is itself part of the
authority contract: `begin_append`'s `rowcount == 1` is NOT permission to
APPEND -- only a successfully returning `session.commit()` is.

Transitions (anything not listed is forbidden and has no function here):

    (no row)            -> CREATING          insert_claim
    CREATING (unarmed)  -> CREATING attempt+1 takeover_pre_fence (lease expired)
    CREATING (unarmed)  -> CREATING armed     begin_append (the network permit)
    CREATING            -> CREATED            finalize_created (tagged OK)
    CREATING            -> FAILED             finalize_failed (definite: nothing stored)
    CREATING (armed)    -> UNCERTAIN          finalize_uncertain / classify_stale_armed
    FAILED              -> CREATING attempt+1 retry_failed (explicit operator retry)
    UNCERTAIN           -> CREATED            reconcile_created (one positive match)
    UNCERTAIN           -> CREATED            retained_ok_created (same live attempt's OK)

CREATED is terminal. UNCERTAIN never becomes FAILED or CREATING.
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.datetime_utils import ensure_utc
from app.db.models import GmailApplicationDraftRecord as Ledger

CREATING = "CREATING"
CREATED = "CREATED"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"

LINK_CONFLICT = "LINK"
MARKER_CONFLICT = "MARKER"

_TABLE = Ledger.__tablename__
# Constraint names (PostgreSQL reports these) and the column lists SQLite
# reports instead ("UNIQUE constraint failed: <table>.<col>").
_CONFLICT_SIGNATURES = (
    (LINK_CONFLICT, ("uq_gmail_application_drafts_link_id", f"{_TABLE}.link_id")),
    (
        MARKER_CONFLICT,
        (
            "uq_gmail_application_drafts_marker_message_id",
            f"{_TABLE}.marker_message_id",
        ),
    ),
)

# `secrets.token_urlsafe(16)` -> exactly 22 chars of [A-Za-z0-9_-].
CLAIM_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}")


def new_claim_token() -> str:
    return secrets.token_urlsafe(16)


def classify_ledger_conflict(exc: IntegrityError) -> str | None:
    """Which ledger UNIQUE constraint an IntegrityError violated, or None.
    Only a known conflict may be read as "another worker won"; anything
    else must be re-raised by the caller."""
    message = str(exc.orig)
    for kind, signatures in _CONFLICT_SIGNATURES:
        if any(signature in message for signature in signatures):
            return kind
    return None


@dataclass(frozen=True)
class FrozenHandoff:
    """The exact Stage 9C/6E package identity a ledger row is bound to."""

    review_id: int
    approved_revision_id: int
    preparation_id: int
    generation: int
    match_id: int
    cv_draft_id: int
    bewerbung_draft_id: int
    package_token: str = field(repr=False)
    input_identity: str = field(repr=False)


@dataclass(frozen=True)
class AttemptBundle:
    """What `begin_append` freezes for one attempt. Never logged."""

    marker_message_id: str = field(repr=False)
    content_sha256: str = field(repr=False)
    renderer_version: str
    drafts_mailbox: str = field(repr=False)
    drafts_mailbox_wire: str = field(repr=False)
    attempt_budget_seconds: int


@dataclass(frozen=True)
class Claim:
    """One live owned CREATING attempt (id + token + attempt count)."""

    ledger_id: int
    link_id: int
    claim_token: str = field(repr=False)
    attempt_count: int


@dataclass(frozen=True)
class LedgerSnapshot:
    """A detached, immutable copy of one ledger row (for reporting and for
    building exact CAS predicates). Sensitive fields stay out of repr."""

    id: int
    link_id: int
    state: str
    attempt_count: int
    account_key: str = field(repr=False)
    handoff: FrozenHandoff = field(repr=False)
    claim_token: str | None = field(repr=False)
    claim_started_at: datetime | None = field(repr=False)
    append_started_at: datetime | None = field(repr=False)
    marker_message_id: str | None = field(repr=False)
    content_sha256: str | None = field(repr=False)
    renderer_version: str | None = field(repr=False)
    drafts_mailbox: str | None = field(repr=False)
    drafts_mailbox_wire: str | None = field(repr=False)
    attempt_budget_seconds: int | None = field(repr=False)
    uid_validity: int | None = field(repr=False)
    draft_uid: int | None = field(repr=False)
    reconciled: bool = field(repr=False)
    last_error: str | None = field(repr=False)
    created_in_gmail_at: datetime | None = field(repr=False)

    @classmethod
    def of(cls, row: Ledger) -> "LedgerSnapshot":
        return cls(
            id=row.id,
            link_id=row.link_id,
            state=row.state,
            attempt_count=row.attempt_count,
            account_key=row.account_key,
            handoff=handoff_of(row),
            claim_token=row.claim_token,
            claim_started_at=row.claim_started_at,
            append_started_at=row.append_started_at,
            marker_message_id=row.marker_message_id,
            content_sha256=row.content_sha256,
            renderer_version=row.renderer_version,
            drafts_mailbox=row.drafts_mailbox,
            drafts_mailbox_wire=row.drafts_mailbox_wire,
            attempt_budget_seconds=row.attempt_budget_seconds,
            uid_validity=row.uid_validity,
            draft_uid=row.draft_uid,
            reconciled=bool(row.reconciled),
            last_error=row.last_error,
            created_in_gmail_at=row.created_in_gmail_at,
        )

    @property
    def armed(self) -> bool:
        return self.append_started_at is not None

    @property
    def attempt_deadline_at(self) -> datetime | None:
        """append_started_at + the FROZEN budget -- never the current
        configuration, and never restarted by a replay."""
        if self.append_started_at is None or self.attempt_budget_seconds is None:
            return None
        return ensure_utc(self.append_started_at) + timedelta(seconds=self.attempt_budget_seconds)


def handoff_of(row: Ledger) -> FrozenHandoff:
    return FrozenHandoff(
        review_id=row.review_id,
        approved_revision_id=row.approved_revision_id,
        preparation_id=row.preparation_id,
        generation=row.generation,
        match_id=row.match_id,
        cv_draft_id=row.cv_draft_id,
        bewerbung_draft_id=row.bewerbung_draft_id,
        package_token=row.package_token,
        input_identity=row.input_identity,
    )


def _cas(stmt):
    """CAS UPDATEs never synchronize in-session ORM objects: the outcome is
    decided by the database predicate alone; callers re-read afterwards."""
    return stmt.execution_options(synchronize_session=False)


def _one(result) -> bool:
    return result.rowcount == 1


# --- reads ----------------------------------------------------------------------


def get_snapshot_by_link(db: Session, link_id: int) -> LedgerSnapshot | None:
    """Fresh, UNLOCKED read (reporting / duplicate detection only -- never
    authority). Callers end this transaction before any full-freshness
    path so no ledger-first lock is ever held there."""
    row = db.scalar(
        select(Ledger).where(Ledger.link_id == link_id).execution_options(populate_existing=True)
    )
    return LedgerSnapshot.of(row) if row is not None else None


def get_snapshot_by_id(db: Session, ledger_id: int) -> LedgerSnapshot | None:
    row = db.scalar(
        select(Ledger).where(Ledger.id == ledger_id).execution_options(populate_existing=True)
    )
    return LedgerSnapshot.of(row) if row is not None else None


def lock_by_link(db: Session, link_id: int) -> LedgerSnapshot | None:
    """`SELECT ... FOR UPDATE` of the ledger row -- always acquired LAST,
    after profile -> job -> preparation -> review (-> revision read)."""
    row = db.scalar(
        select(Ledger)
        .where(Ledger.link_id == link_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return LedgerSnapshot.of(row) if row is not None else None


# --- claims -----------------------------------------------------------------------


def insert_claim(
    db: Session, *, link_id: int, account_key: str, handoff: FrozenHandoff, now: datetime
) -> Claim:
    """INSERT the first CREATING claim (attempt 1) and FLUSH (no commit).
    A concurrent first claim loses on UNIQUE(link_id) with an
    IntegrityError the caller classifies (`classify_ledger_conflict`)."""
    token = new_claim_token()
    row = Ledger(
        link_id=link_id,
        account_key=account_key,
        review_id=handoff.review_id,
        approved_revision_id=handoff.approved_revision_id,
        preparation_id=handoff.preparation_id,
        generation=handoff.generation,
        match_id=handoff.match_id,
        cv_draft_id=handoff.cv_draft_id,
        bewerbung_draft_id=handoff.bewerbung_draft_id,
        package_token=handoff.package_token,
        input_identity=handoff.input_identity,
        state=CREATING,
        attempt_count=1,
        claim_token=token,
        claim_started_at=now,
        reconciled=False,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return Claim(row.id, link_id, token, 1)


def takeover_pre_fence(
    db: Session, observed: LedgerSnapshot, *, lease_cutoff: datetime, now: datetime
) -> Claim | None:
    """CAS a crashed/abandoned UNARMED claim to a NEW attempt (new token,
    attempt + 1). Only valid when the caller holds full fresh Stage 9C
    authorization in lock order: this grants future APPEND authority."""
    token = new_claim_token()
    result = db.execute(
        _cas(update(Ledger))
        .where(
            Ledger.id == observed.id,
            Ledger.state == CREATING,
            Ledger.claim_token == observed.claim_token,
            Ledger.attempt_count == observed.attempt_count,
            Ledger.append_started_at.is_(None),
            Ledger.claim_started_at < lease_cutoff,
        )
        .values(
            claim_token=token,
            claim_started_at=now,
            attempt_count=Ledger.attempt_count + 1,
            last_error=None,
            updated_at=now,
        )
    )
    if not _one(result):
        return None
    return Claim(observed.id, observed.link_id, token, observed.attempt_count + 1)


def retry_failed(db: Session, observed: LedgerSnapshot, *, now: datetime) -> Claim | None:
    """Explicit operator retry: CAS `FAILED -> CREATING` (attempt + 1, new
    token) and atomically clear EVERY trace of the previous attempt, so no
    old marker, target, UID or confirmation can leak into the new one."""
    token = new_claim_token()
    result = db.execute(
        _cas(update(Ledger))
        .where(
            Ledger.id == observed.id,
            Ledger.state == FAILED,
            Ledger.attempt_count == observed.attempt_count,
        )
        .values(
            state=CREATING,
            attempt_count=Ledger.attempt_count + 1,
            claim_token=token,
            claim_started_at=now,
            append_started_at=None,
            marker_message_id=None,
            content_sha256=None,
            renderer_version=None,
            drafts_mailbox=None,
            drafts_mailbox_wire=None,
            attempt_budget_seconds=None,
            uid_validity=None,
            draft_uid=None,
            reconciled=False,
            created_in_gmail_at=None,
            last_error=None,
            updated_at=now,
        )
    )
    if not _one(result):
        return None
    return Claim(observed.id, observed.link_id, token, observed.attempt_count + 1)


# --- the network permit -------------------------------------------------------------


def begin_append(
    db: Session, claim: Claim, *, account_key: str, bundle: AttemptBundle, now: datetime
) -> bool:
    """Freeze the attempt bundle and set the fence, atomically. True means
    only "the UPDATE matched"; the APPEND is permitted solely after the
    caller's `commit()` of this transaction RETURNS successfully."""
    result = db.execute(
        _cas(update(Ledger))
        .where(
            Ledger.id == claim.ledger_id,
            Ledger.state == CREATING,
            Ledger.claim_token == claim.claim_token,
            Ledger.attempt_count == claim.attempt_count,
            Ledger.append_started_at.is_(None),
        )
        .values(
            account_key=account_key,
            append_started_at=now,
            marker_message_id=bundle.marker_message_id,
            content_sha256=bundle.content_sha256,
            renderer_version=bundle.renderer_version,
            drafts_mailbox=bundle.drafts_mailbox,
            drafts_mailbox_wire=bundle.drafts_mailbox_wire,
            attempt_budget_seconds=bundle.attempt_budget_seconds,
            updated_at=now,
        )
    )
    return _one(result)


# --- finalization (owner of the live claim) ---------------------------------------


def _owned(claim: Claim, marker: str | None) -> tuple:
    conditions = (
        Ledger.id == claim.ledger_id,
        Ledger.state == CREATING,
        Ledger.claim_token == claim.claim_token,
        Ledger.attempt_count == claim.attempt_count,
    )
    if marker is None:
        return (*conditions, Ledger.append_started_at.is_(None))
    return (*conditions, Ledger.marker_message_id == marker)


def finalize_created(
    db: Session,
    claim: Claim,
    *,
    marker: str,
    uid_validity: int | None,
    draft_uid: int | None,
    now: datetime,
) -> bool:
    """`CREATING (armed) -> CREATED` after a fully parsed tagged OK."""
    result = db.execute(
        _cas(update(Ledger))
        .where(*_owned(claim, marker))
        .values(
            state=CREATED,
            claim_token=None,
            claim_started_at=None,
            uid_validity=uid_validity,
            draft_uid=draft_uid,
            reconciled=False,
            created_in_gmail_at=now,
            last_error=None,
            updated_at=now,
        )
    )
    return _one(result)


def finalize_failed(
    db: Session, claim: Claim, *, marker: str | None, error_code: str, now: datetime
) -> bool:
    """`CREATING -> FAILED` ONLY with proof that this attempt stored
    nothing (no APPEND invoked, or a fully parsed tagged NO). `marker=None`
    releases an unarmed claim; otherwise the armed attempt is named."""
    result = db.execute(
        _cas(update(Ledger))
        .where(*_owned(claim, marker))
        .values(
            state=FAILED,
            claim_token=None,
            claim_started_at=None,
            last_error=error_code[:60],
            updated_at=now,
        )
    )
    return _one(result)


def finalize_uncertain(
    db: Session, claim: Claim, *, marker: str, error_code: str, now: datetime
) -> bool:
    """`CREATING (armed) -> UNCERTAIN`: the APPEND may have stored a draft."""
    result = db.execute(
        _cas(update(Ledger))
        .where(*_owned(claim, marker))
        .values(
            state=UNCERTAIN,
            claim_token=None,
            claim_started_at=None,
            last_error=error_code[:60],
            updated_at=now,
        )
    )
    return _one(result)


def classify_stale_armed(
    db: Session, observed: LedgerSnapshot, *, stale_margin_seconds: float, now: datetime
) -> bool:
    """Ledger-only: an ARMED attempt past its frozen deadline + margin
    becomes UNCERTAIN. It never grants new network authority, and the
    attempt is never transferred to another APPEND worker."""
    deadline = observed.attempt_deadline_at
    if observed.state != CREATING or deadline is None:
        return False
    if now <= deadline + timedelta(seconds=stale_margin_seconds):
        return False
    cutoff = now - timedelta(seconds=observed.attempt_budget_seconds + stale_margin_seconds)
    result = db.execute(
        _cas(update(Ledger))
        .where(
            Ledger.id == observed.id,
            Ledger.state == CREATING,
            Ledger.claim_token == observed.claim_token,
            Ledger.attempt_count == observed.attempt_count,
            Ledger.marker_message_id == observed.marker_message_id,
            Ledger.attempt_budget_seconds == observed.attempt_budget_seconds,
            Ledger.append_started_at < cutoff,
        )
        .values(
            state=UNCERTAIN,
            claim_token=None,
            claim_started_at=None,
            last_error="STALE_ARMED_ATTEMPT",
            updated_at=now,
        )
    )
    return _one(result)


# --- positive evidence only: UNCERTAIN -> CREATED ----------------------------------


def _uncertain_attempt(ledger_id: int, attempt_count: int, marker: str) -> tuple:
    return (
        Ledger.id == ledger_id,
        Ledger.state == UNCERTAIN,
        Ledger.attempt_count == attempt_count,
        Ledger.marker_message_id == marker,
    )


def reconcile_created(
    db: Session,
    *,
    ledger_id: int,
    attempt_count: int,
    marker: str,
    uid_validity: int,
    draft_uid: int,
    now: datetime,
) -> bool:
    """Exactly one read-only Message-ID match for THIS attempt's marker."""
    result = db.execute(
        _cas(update(Ledger))
        .where(*_uncertain_attempt(ledger_id, attempt_count, marker))
        .values(
            state=CREATED,
            uid_validity=uid_validity,
            draft_uid=draft_uid,
            reconciled=True,
            created_in_gmail_at=now,
            last_error=None,
            updated_at=now,
        )
    )
    return _one(result)


def retained_ok_created(
    db: Session,
    *,
    ledger_id: int,
    attempt_count: int,
    marker: str,
    uid_validity: int | None,
    draft_uid: int | None,
    now: datetime,
) -> bool:
    """The same LIVE operation's retained tagged APPEND OK for this exact
    attempt, after a stale classifier won the race to UNCERTAIN. The old
    claim token is deliberately NOT a predicate: both claim columns are
    NULL outside CREATING, and id + monotonic attempt + unique marker
    already identify the attempt."""
    result = db.execute(
        _cas(update(Ledger))
        .where(*_uncertain_attempt(ledger_id, attempt_count, marker))
        .values(
            state=CREATED,
            uid_validity=uid_validity,
            draft_uid=draft_uid,
            reconciled=False,
            created_in_gmail_at=now,
            last_error=None,
            updated_at=now,
        )
    )
    return _one(result)
