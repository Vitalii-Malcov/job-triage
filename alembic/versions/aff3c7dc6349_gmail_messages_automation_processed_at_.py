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

**No backfill from the old watermark (Astra R3 Codex re-review, HIGH
finding fixed here).** An earlier version of this migration backfilled
`automation_processed_at` for every existing row whose `id` was `<=`
that account's `gmail_after_message_id`. That was wrong, and exactly
backwards for what this migration exists to fix: the whole reason
AUD-004 is a real bug is that `gmail_after_message_id` is NOT trustworthy
evidence a message was actually processed — it is precisely the
mechanism that could advance past a lower-`id` row that had not yet
committed. Backfilling from it would silently CEMENT any historical
instance of the AUD-004 race as a permanent, undetectable skip: a
message that was never actually processed would be marked
`automation_processed_at != NULL` and would never be picked up by the
fixed selector either. There is no other durable, per-message evidence
in this schema that unambiguously proves Stage 8D's full pipeline
(analysis + response-draft-or-NO_RESPONSE_RECOMMENDED-or-OUTBOUND-skip)
already completed for a given message — `GmailMessageAnalysisRecord`/
`ResponseDraftRecord` existence is close but was never treated as the
authoritative signal Stage 8D itself relies on, and inferring "safe to
skip" from it here would reintroduce the same class of assumption this
fix removes elsewhere.

Every pre-existing `gmail_messages` row is therefore left with
`automation_processed_at = NULL` — eligible for the very next Stage 8D
scan, exactly like a message the fixed code path would treat as
unprocessed for any other reason. This trades a one-time, BOUNDED
(`Settings.automation_gmail_process_max_per_run` per cycle) historical
catch-up reprocessing pass for actual correctness: analysis and
response-draft generation are idempotent (re-running them for an
already-handled message reuses/no-ops rather than duplicating), and
Stage 8D never sends email or performs any external/approval-requiring
action on its own (see app/services/automation_gmail.py's own module
docstring, "Drafts only") — so re-scanning old messages after this
migration is safe, side-effect-free beyond bounded extra DB work, and
recovers any message the old watermark may have silently stranded.
Correctness over avoiding a bounded amount of harmless reprocessing.

`automation_mail_progress.gmail_after_message_id` itself is left
completely untouched (not dropped, not read, not written by this
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
    # Deliberately NO backfill (see module docstring) — every existing row
    # stays automation_processed_at = NULL, so it remains eligible for the
    # next Stage 8D scan rather than risking silently cementing a message
    # the old, untrustworthy `gmail_after_message_id` watermark may have
    # skipped without ever actually processing it.


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.drop_index("ix_gmail_messages_account_key_automation_processed_at")
        batch_op.drop_column("automation_processed_at")
