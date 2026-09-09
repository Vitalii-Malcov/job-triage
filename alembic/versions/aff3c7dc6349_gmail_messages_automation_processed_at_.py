"""gmail messages automation_processed_at (AUD-004, Astra R3)

Adds `gmail_messages.automation_processed_at` — a durable PER-MESSAGE
completion marker for Stage 8D's `gmail_response_drafts` step, replacing
its reliance on `automation_mail_progress.gmail_after_message_id` (a
single per-account `id > cursor` watermark) for message SELECTION.

**The bug this closes.** `id > cursor` assumes `GmailMessageRecord.id`
allocation order equals commit-visibility order. PostgreSQL does not
guarantee this: transaction A can obtain a LOWER `id` but commit AFTER
transaction B, which obtained a HIGHER `id` and committed first. If the
cursor had already advanced to B's `id` before A's row became visible,
`id > cursor` would PERMANENTLY skip A — real data loss, not a cosmetic
issue. See app/db/models.py's `GmailMessageRecord.automation_processed_at`
docstring and app/db/gmail_repository.py's
`list_unprocessed_messages_for_automation`/`mark_message_automation_processed`
for the fixed selection query and CAS.

**Backfill is additive, not destructive.** Existing rows are marked
processed (`automation_processed_at = now`) wherever their `id` is `<=`
that account's existing `gmail_after_message_id` cursor — preserving
current progress so upgrading does not trigger a reprocessing flood of
the entire historical mailbox. This mirrors migration
`b3f1c9a7d5e2`'s (`provider_arrival_at`) "honest best-effort backfill,
identical behavior to what this project already had, strictly no worse"
precedent — it is not a claim that no message committed out of order
before this fix; historical continuity, not retroactive correctness
recovery, is the goal. `automation_mail_progress.gmail_after_message_id`
itself is left completely untouched (not dropped, not written to by this
migration) — Stage 8D's own code simply stops reading it for message
selection going forward; nothing here is destructive or loses data.

Revision ID: aff3c7dc6349
Revises: f1a2b3c4d5e6
Create Date: 2026-09-09 17:06:57.776179

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aff3c7dc6349"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "automation_processed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_gmail_messages_account_key_automation_processed_at",
            ["account_key", "automation_processed_at"],
        )
    # Best-effort backfill (see module docstring): preserve each account's
    # existing Stage 8D progress so upgrading does not reprocess the whole
    # historical mailbox. A correlated subquery against
    # automation_mail_progress works identically on SQLite and PostgreSQL.
    op.execute(
        """
        UPDATE gmail_messages
        SET automation_processed_at = CURRENT_TIMESTAMP
        WHERE id <= (
            SELECT gmail_after_message_id
            FROM automation_mail_progress
            WHERE automation_mail_progress.account_key = gmail_messages.account_key
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.drop_index("ix_gmail_messages_account_key_automation_processed_at")
        batch_op.drop_column("automation_processed_at")
