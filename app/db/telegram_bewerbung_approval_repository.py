"""Stage 9C: persistence for the immutable Telegram approval link plus the
fresh row-lock helpers the review/approval transactions use.

**The link is insert-only.** `insert_link` flushes and never commits: the
caller inserts the Stage 6E review, its revision 1 and the link in ONE
transaction and commits (or rolls back) all three together. Nothing here
updates or deletes a link, and nothing here writes any other table.

**Fresh locked values.** `SELECT ... FOR UPDATE` acquires a row lock but does
not by itself overwrite attributes of an ORM object already in the session's
identity map. Every lock helper therefore either uses
`populate_existing` or refreshes the locked object, so checks made under the
lock see the committed row, never a stale earlier read.

Common lock order (never acquire an earlier lock after a later one):
profile -> job -> Stage 9B preparation -> Stage 6E review header -> revision.
"""

import re
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.candidate_profile_repository import get_candidate_profile
from app.db.models import (
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
    CandidateProfileRecord,
    JobRecord,
    TelegramBewerbungApprovalRecord,
    TelegramBewerbungPreparationRecord,
)

# 12 random bytes -> exactly 16 base64url characters (96 bits).
APPROVAL_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9_-]{16}")
_CAPABILITY_BYTES = 12

BOUND_REVIEW_VERSION = 1

GENERATION_CONFLICT = "GENERATION"
CAPABILITY_CONFLICT = "CAPABILITY"
REVIEW_CONFLICT = "REVIEW"

# Constraint names (PostgreSQL reports these) and the column lists SQLite
# reports instead ("UNIQUE constraint failed: <table>.<col>, ...").
_TABLE = TelegramBewerbungApprovalRecord.__tablename__
_CONFLICT_SIGNATURES = (
    (
        GENERATION_CONFLICT,
        (
            "uq_telegram_bewerbung_approvals_preparation_generation",
            f"{_TABLE}.preparation_id, {_TABLE}.generation",
        ),
    ),
    (
        CAPABILITY_CONFLICT,
        (
            "uq_telegram_bewerbung_approvals_approval_capability",
            f"{_TABLE}.approval_capability",
        ),
    ),
    (
        REVIEW_CONFLICT,
        ("uq_telegram_bewerbung_approvals_review_id", f"{_TABLE}.review_id"),
    ),
)


def new_approval_capability() -> str:
    return secrets.token_urlsafe(_CAPABILITY_BYTES)


def classify_link_conflict(exc: IntegrityError) -> str | None:
    """Which link UNIQUE constraint an IntegrityError violated, or None for
    anything else -- an unrecognized integrity error is never treated as
    "another worker won"."""
    message = str(exc.orig)
    for kind, signatures in _CONFLICT_SIGNATURES:
        if any(signature in message for signature in signatures):
            return kind
    return None


def get_link_by_capability(db: Session, capability: str) -> TelegramBewerbungApprovalRecord | None:
    """Resolve a decision/page capability. Anything that is not exactly the
    capability shape is rejected without querying (client-supplied input).
    A plain read: link rows are immutable."""
    if not APPROVAL_CAPABILITY_PATTERN.fullmatch(capability):
        return None
    return db.scalar(
        select(TelegramBewerbungApprovalRecord).where(
            TelegramBewerbungApprovalRecord.approval_capability == capability
        )
    )


def get_link_for_generation(
    db: Session, preparation_id: int, generation: int
) -> TelegramBewerbungApprovalRecord | None:
    return db.scalar(
        select(TelegramBewerbungApprovalRecord).where(
            TelegramBewerbungApprovalRecord.preparation_id == preparation_id,
            TelegramBewerbungApprovalRecord.generation == generation,
        )
    )


def insert_link(
    db: Session,
    *,
    preparation_id: int,
    generation: int,
    package_token: str,
    input_identity: str,
    review_id: int,
    approval_capability: str,
) -> TelegramBewerbungApprovalRecord:
    """Add and FLUSH the immutable link (never commits). A UNIQUE violation
    surfaces here as IntegrityError; the caller rolls back the whole
    transaction, including the provisional Stage 6E review."""
    link = TelegramBewerbungApprovalRecord(
        preparation_id=preparation_id,
        generation=generation,
        package_token=package_token,
        input_identity=input_identity,
        review_id=review_id,
        bound_review_version=BOUND_REVIEW_VERSION,
        approval_capability=approval_capability,
    )
    db.add(link)
    db.flush()
    return link


def lock_profile_fresh(db: Session) -> CandidateProfileRecord | None:
    """Lock the singleton profile (fail-closed lookup, never created) and
    refresh it while the lock is held."""
    profile = get_candidate_profile(db, for_update=True)
    if profile is not None:
        db.refresh(profile, with_for_update=True)
    return profile


def _lock(db: Session, model, row_id: int):
    stmt = (
        select(model)
        .where(model.id == row_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)


def lock_job_fresh(db: Session, job_id: int) -> JobRecord | None:
    return _lock(db, JobRecord, job_id)


def lock_preparation_fresh(
    db: Session, preparation_id: int
) -> TelegramBewerbungPreparationRecord | None:
    return _lock(db, TelegramBewerbungPreparationRecord, preparation_id)


def lock_review_header_fresh(db: Session, review_id: int) -> ApplicationPackageReviewRecord | None:
    return _lock(db, ApplicationPackageReviewRecord, review_id)


def get_bound_revision(
    db: Session, review_id: int, revision_number: int
) -> ApplicationPackageReviewRevisionRecord | None:
    """The exact immutable revision `(review_id, revision_number)` -- never
    "the latest revision"."""
    return db.scalar(
        select(ApplicationPackageReviewRevisionRecord)
        .where(
            ApplicationPackageReviewRevisionRecord.review_id == review_id,
            ApplicationPackageReviewRevisionRecord.revision_number == revision_number,
        )
        .execution_options(populate_existing=True)
    )
