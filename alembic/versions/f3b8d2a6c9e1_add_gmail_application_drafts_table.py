"""add gmail_application_drafts table (Stage 9D Gmail draft handoff)

Adds `gmail_application_drafts` -- the durable, fenced ledger of ONE Gmail
draft handoff (IMAP APPEND into the verified Drafts mailbox) per exact
Stage 9C approval link (see app.db.models.GmailApplicationDraftRecord's
docstring). GMAIL DRAFT CREATED != APPLICATION SENT: nothing here sends.

`link_id` and the frozen handoff columns are deliberately NOT foreign keys,
so operational cleanup never erases draft-handoff history.

S9D-CONF-001: `drafts_mailbox_wire` is String(2048) -- 200 decoded code
points expand to at most 1,602 ASCII characters as quoted modified UTF-7.

The CHECK bodies below repeat app.db.models.GMAIL_DRAFT_CHECKS verbatim
(a migration never imports app code); tests compare the two copies.

No existing table is altered; no backfill.

Revision ID: f3b8d2a6c9e1
Revises: e5a17c3b9d42
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b8d2a6c9e1"
down_revision: str | Sequence[str] | None = "e5a17c3b9d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "gmail_application_drafts"
_UID_MAX = 4294967295
_MARKER_LENGTH = 60

_CHECKS: dict[str, str] = {
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
        f"AND uid_validity BETWEEN 1 AND {_UID_MAX} "
        f"AND draft_uid BETWEEN 1 AND {_UID_MAX})"
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
        f"marker_message_id IS NULL OR length(marker_message_id) = {_MARKER_LENGTH}"
    ),
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("link_id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("approved_revision_id", sa.Integer(), nullable=False),
        sa.Column("preparation_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("cv_draft_id", sa.Integer(), nullable=False),
        sa.Column("bewerbung_draft_id", sa.Integer(), nullable=False),
        sa.Column("package_token", sa.String(length=32), nullable=False),
        sa.Column("input_identity", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("claim_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("append_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("marker_message_id", sa.String(length=64), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("renderer_version", sa.String(length=30), nullable=True),
        sa.Column("drafts_mailbox", sa.String(length=200), nullable=True),
        sa.Column("drafts_mailbox_wire", sa.String(length=2048), nullable=True),
        sa.Column("attempt_budget_seconds", sa.Integer(), nullable=True),
        sa.Column("uid_validity", sa.BigInteger(), nullable=True),
        sa.Column("draft_uid", sa.BigInteger(), nullable=True),
        sa.Column("reconciled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.String(length=60), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_in_gmail_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("link_id", name="uq_gmail_application_drafts_link_id"),
        sa.UniqueConstraint(
            "marker_message_id", name="uq_gmail_application_drafts_marker_message_id"
        ),
        *(sa.CheckConstraint(body, name=name) for name, body in _CHECKS.items()),
    )
    op.create_index(
        "ix_gmail_application_drafts_state_claim",
        _TABLE,
        ["state", "claim_started_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_gmail_application_drafts_state_claim", table_name=_TABLE)
    op.drop_table(_TABLE)
