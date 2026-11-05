"""ORM models: gmail drafts (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    false as sa_false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Stage 9D CHECK constraint bodies. The Alembic migration repeats them
# verbatim (migrations never import app code); the migration tests compare
# both copies so they cannot drift.
GMAIL_DRAFT_UID_MAX = 4294967295


GMAIL_DRAFT_MARKER_LENGTH = 60


GMAIL_DRAFT_CHECKS: dict[str, str] = {
    "ck_gmail_application_drafts_state_valid": (
        "state IN ('CREATING', 'CREATED', 'FAILED', 'UNCERTAIN')"
    ),
    "ck_gmail_application_drafts_attempt_count_positive": "attempt_count >= 1",
    "ck_gmail_application_drafts_generation_positive": "generation >= 1",
    "ck_gmail_application_drafts_claim_state": (
        "(state = 'CREATING' AND claim_token IS NOT NULL AND claim_started_at IS NOT NULL) "
        "OR (state <> 'CREATING' AND claim_token IS NULL AND claim_started_at IS NULL)"
    ),
    "ck_gmail_application_drafts_armed_bundle": (
        "append_started_at IS NULL OR (marker_message_id IS NOT NULL "
        "AND content_sha256 IS NOT NULL AND renderer_version IS NOT NULL "
        "AND drafts_mailbox IS NOT NULL AND drafts_mailbox_wire IS NOT NULL "
        "AND attempt_budget_seconds IS NOT NULL)"
    ),
    "ck_gmail_application_drafts_unarmed_no_bundle": (
        "append_started_at IS NOT NULL OR (marker_message_id IS NULL "
        "AND content_sha256 IS NULL AND renderer_version IS NULL "
        "AND drafts_mailbox IS NULL AND drafts_mailbox_wire IS NULL "
        "AND attempt_budget_seconds IS NULL)"
    ),
    "ck_gmail_application_drafts_outcome_requires_fence": (
        "state NOT IN ('CREATED', 'UNCERTAIN') OR append_started_at IS NOT NULL"
    ),
    "ck_gmail_application_drafts_uid_pair": (
        "(uid_validity IS NULL AND draft_uid IS NULL) OR (uid_validity IS NOT NULL "
        "AND draft_uid IS NOT NULL "
        f"AND uid_validity BETWEEN 1 AND {GMAIL_DRAFT_UID_MAX} "
        f"AND draft_uid BETWEEN 1 AND {GMAIL_DRAFT_UID_MAX})"
    ),
    "ck_gmail_application_drafts_created_timestamp": (
        "state <> 'CREATED' OR created_in_gmail_at IS NOT NULL"
    ),
    "ck_gmail_application_drafts_remote_evidence_only_created": (
        "state = 'CREATED' OR (uid_validity IS NULL AND draft_uid IS NULL "
        "AND created_in_gmail_at IS NULL AND NOT reconciled)"
    ),
    "ck_gmail_application_drafts_reconciled_requires_uid": (
        "NOT reconciled OR (state = 'CREATED' AND uid_validity IS NOT NULL "
        "AND draft_uid IS NOT NULL)"
    ),
    "ck_gmail_application_drafts_budget_positive": (
        "attempt_budget_seconds IS NULL OR attempt_budget_seconds >= 1"
    ),
    "ck_gmail_application_drafts_marker_length": (
        f"marker_message_id IS NULL OR length(marker_message_id) = {GMAIL_DRAFT_MARKER_LENGTH}"
    ),
}


class GmailApplicationDraftRecord(Base):
    """Stage 9D: the durable ledger of ONE Gmail draft handoff per exact
    Stage 9C approval link (see app/services/gmail_application_draft.py).

    **GMAIL DRAFT CREATED != APPLICATION SENT.** Stage 9D's only external
    mutation is ONE IMAP APPEND of a recipient-less, attachment-less
    plain-text draft into the verified Drafts mailbox. Nothing is sent; no
    other table is written; `JobRecord.status` is never touched.

    **Business key.** UNIQUE(link_id): one creation history per approved
    link, across accounts -- a configuration change can never duplicate
    the package into another mailbox. `link_id` and the frozen handoff
    columns are deliberately NOT foreign keys and never cascade (same
    historical-retention rationale as `TelegramBewerbungApprovalRecord`).

    **States.** `CREATING` (one owner: `claim_token` + `attempt_count`),
    `CREATED` (terminal, historical: Stage 9D created this draft at least
    once -- NOT a claim that it still exists, is unsent or unchanged),
    `FAILED` (definite proof that this attempt stored nothing; explicitly
    retryable), `UNCERTAIN` (an APPEND may have stored a draft; sticky --
    only positive evidence for the exact attempt may turn it CREATED).

    **Fence.** `append_started_at` plus the attempt bundle (marker
    Message-ID, logical content hash, renderer version, decoded and wire
    Drafts mailbox, absolute attempt budget) are frozen together by the
    `begin_append` CAS; only that transaction's ACKNOWLEDGED commit permits
    the single APPEND. Transition-history rules a CHECK cannot express
    (CREATED terminal, no UNCERTAIN -> FAILED/CREATING) are enforced by the
    repository's SQL CAS predicates.

    No recipient, body, capability, password or send-status columns exist.
    """

    __tablename__ = "gmail_application_drafts"
    __table_args__ = (
        UniqueConstraint("link_id", name="uq_gmail_application_drafts_link_id"),
        UniqueConstraint("marker_message_id", name="uq_gmail_application_drafts_marker_message_id"),
        *(CheckConstraint(body, name=name) for name, body in GMAIL_DRAFT_CHECKS.items()),
        Index("ix_gmail_application_drafts_state_claim", "state", "claim_started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False)

    review_id: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_revision_id: Mapped[int] = mapped_column(Integer, nullable=False)
    preparation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    match_id: Mapped[int] = mapped_column(Integer, nullable=False)
    cv_draft_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bewerbung_draft_id: Mapped[int] = mapped_column(Integer, nullable=False)
    package_token: Mapped[str] = mapped_column(String(32), nullable=False)
    input_identity: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    claim_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    append_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    marker_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    renderer_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    drafts_mailbox: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # S9D-CONF-001: 200 decoded code points expand to at most 1,602 ASCII
    # characters as quoted modified UTF-7; 2048 covers that with margin.
    # The encoded value is bound-checked before any claim -- never truncated.
    drafts_mailbox_wire: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attempt_budget_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    uid_validity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    draft_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reconciled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )

    last_error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    created_in_gmail_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
