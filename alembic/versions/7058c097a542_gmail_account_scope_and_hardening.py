"""gmail account scope and integrity hardening

Stage 7A security remediation round (Codex review GMAIL-002/GMAIL-009):
adds account-scoped identity to the Gmail inbox tables and DB-level
CHECK constraints, on top of the original Stage 7A schema
(8634f4be953a). Applied as a NEW migration rather than editing
8634f4be953a in place — Stage 7A is not yet merged to main, but the
prior migration is already committed to this feature branch's history,
and this project's convention (every stage/change ships its own
migration, history is never rewritten — see README "Миграции") favors a
follow-up migration over amending a committed one.

Revision ID: 7058c097a542
Revises: 8634f4be953a
Create Date: 2026-08-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7058c097a542"
down_revision: str | Sequence[str] | None = "8634f4be953a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # `server_default=""` backfills every pre-existing row (Stage 7A has
    # no production data yet — this branch is unmerged — so "" is a safe,
    # explicit "predates account scoping" placeholder, not a guess at a
    # real account) on both SQLite and PostgreSQL without a separate
    # UPDATE statement.
    with op.batch_alter_table("gmail_threads") as batch_op:
        batch_op.add_column(
            sa.Column("account_key", sa.String(length=320), server_default="", nullable=False)
        )
        batch_op.drop_constraint("uq_gmail_threads_thread_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_threads_account_thread_key", ["account_key", "thread_key"]
        )
        batch_op.create_index("ix_gmail_threads_account_key", ["account_key"])

    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.add_column(
            sa.Column("account_key", sa.String(length=320), server_default="", nullable=False)
        )
        batch_op.drop_constraint("uq_gmail_messages_provider_identity", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_messages_account_provider_identity",
            ["account_key", "mailbox", "uid_validity", "uid"],
        )
        batch_op.create_index("ix_gmail_messages_account_key", ["account_key"])
        batch_op.create_check_constraint("ck_gmail_messages_uid_positive", "uid > 0")
        batch_op.create_check_constraint(
            "ck_gmail_messages_uid_validity_positive", "uid_validity > 0"
        )
        batch_op.create_check_constraint(
            "ck_gmail_messages_direction_valid", "direction IN ('INBOUND', 'OUTBOUND')"
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.drop_constraint("ck_gmail_messages_direction_valid", type_="check")
        batch_op.drop_constraint("ck_gmail_messages_uid_validity_positive", type_="check")
        batch_op.drop_constraint("ck_gmail_messages_uid_positive", type_="check")
        batch_op.drop_index("ix_gmail_messages_account_key")
        batch_op.drop_constraint("uq_gmail_messages_account_provider_identity", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_messages_provider_identity", ["mailbox", "uid_validity", "uid"]
        )
        batch_op.drop_column("account_key")

    with op.batch_alter_table("gmail_threads") as batch_op:
        batch_op.drop_index("ix_gmail_threads_account_key")
        batch_op.drop_constraint("uq_gmail_threads_account_thread_key", type_="unique")
        batch_op.create_unique_constraint("uq_gmail_threads_thread_key", ["thread_key"])
        batch_op.drop_column("account_key")
