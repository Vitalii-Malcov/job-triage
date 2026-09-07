"""gmail messages provider_arrival_at (S7E-011, Codex re-review)

Adds `gmail_messages.provider_arrival_at` — the mail server's own IMAP
INTERNALDATE, trusted for correspondence chronology in place of
`received_at` (this project's own sync wall-clock write time).

**Why `received_at` alone was unsafe (the finding this closes).** Gmail
sync runs BOTH mailboxes every time (INBOX, then Sent — see
app.api.routes._run_gmail_sync, S7E-001), each persisted via its own
independent `GmailInboxService.sync()` call. On a first-time/historical
sync (or any run that imports a whole thread's backlog at once), a REAL
outbound message and a chronologically LATER recruiter reply can both be
NEW to this run — but because INBOX is synced first, the reply's
`received_at` (this project's own wall-clock write time) ends up EARLIER
than the outbound message's `received_at`, reversing their true order.
`app.db.follow_up_repository.get_thread_message_infos` trusted
`received_at` for exactly this ordering (S7E-004), so a follow-up could
fire even though a reply had, in Gmail's own reality, already arrived.

`provider_arrival_at` (IMAP INTERNALDATE, see
app/providers/email/imap.py's `_parse_internal_date`) is immune to both
failure modes `received_at`/`sent_at` each had: it is assigned by Gmail
itself (never the sender, unlike `sent_at`'s RFC 5322 Date header) at
real arrival time (never this project's own sync order, unlike
`received_at`).

**Backfill is an honest best-effort, not a retroactive INTERNALDATE
recovery.** Existing rows predating this migration never had their real
INTERNALDATE recorded — backfilling them from their own `received_at` is
the closest available proxy (identical ordering behavior to what this
project already had for those rows before this migration; strictly no
worse), not a claim that it now equals their true Gmail arrival time.
Only messages synced AFTER this migration get a real, trusted
`provider_arrival_at` going forward.

Revision ID: b3f1c9a7d5e2
Revises: 9d4c22a2e372
Create Date: 2026-09-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1c9a7d5e2"
down_revision: str | Sequence[str] | None = "9d4c22a2e372"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "provider_arrival_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    # Best-effort backfill (see module docstring): the closest available
    # proxy for pre-migration rows that never had a real INTERNALDATE
    # recorded is their own existing `received_at`.
    op.execute("UPDATE gmail_messages SET provider_arrival_at = received_at")
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.alter_column(
            "provider_arrival_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.drop_column("provider_arrival_at")
