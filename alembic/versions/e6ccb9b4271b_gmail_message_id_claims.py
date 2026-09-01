"""gmail message-id ownership claims

Stage 7A security remediation round 2 (Codex review GMAIL-011): a
DB-enforced atomic arbiter for "which provider message legitimately owns
this Message-ID within this account" — replaces a Python
check-then-act collision guard that was itself racy under concurrency.
See app/db/models.py's GmailMessageIdClaimRecord docstring for the full
rationale, and app/db/gmail_repository.py's
`_claim_message_id_or_get_collision_thread` for how the UNIQUE
constraint below is used as the actual concurrency arbiter.

Revision ID: e6ccb9b4271b
Revises: 7058c097a542
Create Date: 2026-09-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6ccb9b4271b"
down_revision: str | Sequence[str] | None = "7058c097a542"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "gmail_message_id_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("message_id_header", sa.String(length=998), nullable=False),
        sa.Column("claimant_mailbox", sa.String(length=100), nullable=False),
        sa.Column("claimant_uid_validity", sa.Integer(), nullable=False),
        sa.Column("claimant_uid", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.Integer(), nullable=False),
        sa.Column("contested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["gmail_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_key",
            "message_id_header",
            name="uq_gmail_message_id_claims_account_message_id",
        ),
        sa.CheckConstraint("claimant_uid > 0", name="ck_gmail_message_id_claims_uid_positive"),
        sa.CheckConstraint(
            "claimant_uid_validity > 0",
            name="ck_gmail_message_id_claims_uid_validity_positive",
        ),
    )
    op.create_index(
        op.f("ix_gmail_message_id_claims_thread_id"),
        "gmail_message_id_claims",
        ["thread_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_gmail_message_id_claims_thread_id"), table_name="gmail_message_id_claims"
    )
    op.drop_table("gmail_message_id_claims")
