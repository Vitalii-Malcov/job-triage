"""add gmail inbox tables

Revision ID: 8634f4be953a
Revises: 0ce10aaf8c86
Create Date: 2026-08-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8634f4be953a"
down_revision: str | Sequence[str] | None = "0ce10aaf8c86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "gmail_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("thread_key", sa.String(length=998), nullable=False),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_key", name="uq_gmail_threads_thread_key"),
    )

    op.create_table(
        "gmail_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.Integer(), nullable=False),
        sa.Column("mailbox", sa.String(length=100), nullable=False),
        sa.Column("uid_validity", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("message_id_header", sa.String(length=998), nullable=True),
        sa.Column("in_reply_to", sa.String(length=998), nullable=True),
        sa.Column("references_json", sa.Text(), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=True),
        sa.Column("from_display_name", sa.String(length=200), nullable=True),
        sa.Column("to_addresses_json", sa.Text(), nullable=False),
        sa.Column("cc_addresses_json", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("body_plain", sa.Text(), nullable=False),
        sa.Column("body_truncated", sa.Boolean(), nullable=False),
        sa.Column("has_html", sa.Boolean(), nullable=False),
        sa.Column("attachments_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["gmail_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mailbox", "uid_validity", "uid", name="uq_gmail_messages_provider_identity"
        ),
    )
    op.create_index(
        op.f("ix_gmail_messages_thread_id"), "gmail_messages", ["thread_id"], unique=False
    )
    op.create_index(
        op.f("ix_gmail_messages_message_id_header"),
        "gmail_messages",
        ["message_id_header"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_gmail_messages_message_id_header"), table_name="gmail_messages")
    op.drop_index(op.f("ix_gmail_messages_thread_id"), table_name="gmail_messages")
    op.drop_table("gmail_messages")
    op.drop_table("gmail_threads")
