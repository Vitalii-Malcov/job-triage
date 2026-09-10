"""add gmail_permanent_skips table

FINAL-004 (Astra R5A): a durable, per-message "never fetch this UID
again" marker for a Gmail IMAP message whose content itself is
permanently unpersistable (oversized, or MIME that fails to parse) --
closes a starvation bug where a prefix of permanently-bad, never
-persisted messages at least as large as MAX_MESSAGES_PER_SYNC would
consume the entire budget of every future sync forever, since such a
message was never excluded from the candidate list the way an
already-persisted message already is. See app/db/models.py's
GmailPermanentSkipRecord docstring for the full rationale.

Revision ID: c7d3f9a1e5b8
Revises: b4f6a1c9e7d2
Create Date: 2026-09-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d3f9a1e5b8"
down_revision: str | Sequence[str] | None = "b4f6a1c9e7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "gmail_permanent_skips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("mailbox", sa.String(length=100), nullable=False),
        sa.Column("uid_validity", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_key",
            "mailbox",
            "uid_validity",
            "uid",
            name="uq_gmail_permanent_skips_account_provider_identity",
        ),
        sa.CheckConstraint("uid > 0", name="ck_gmail_permanent_skips_uid_positive"),
        sa.CheckConstraint(
            "uid_validity > 0", name="ck_gmail_permanent_skips_uid_validity_positive"
        ),
    )
    op.create_index(
        "ix_gmail_permanent_skips_account_mailbox_uid_validity",
        "gmail_permanent_skips",
        ["account_key", "mailbox", "uid_validity"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_gmail_permanent_skips_account_mailbox_uid_validity",
        table_name="gmail_permanent_skips",
    )
    op.drop_table("gmail_permanent_skips")
