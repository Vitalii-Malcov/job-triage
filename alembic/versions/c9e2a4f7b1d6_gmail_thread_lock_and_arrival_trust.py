"""gmail thread lock + provider_arrival_is_trusted (S7E-013, Codex re-review)

Two columns closing the final Stage 7E re-review's residual risks:

- `gmail_threads.lock_holder` / `lock_expires_at`: a generic, Gmail-thread-
  scoped mutual-exclusion primitive (see
  app.db.gmail_repository.acquire_thread_lock/wait_for_thread_lock) shared
  by Gmail message persistence (`upsert_message`) and Stage 7E follow-up
  send (`send_follow_up`) — so a new `GmailMessageRecord` cannot commit
  for a thread while a follow-up send holds that thread's guard, and vice
  versa. Generic to Gmail-thread coordination; carries no job/application
  concept.

- `gmail_messages.provider_arrival_is_trusted`: True only when
  `provider_arrival_at` (S7E-011) came from a real, successfully-parsed
  IMAP INTERNALDATE — False for every pre-existing row (backfilled from
  the old `received_at`-based behavior, never a real INTERNALDATE this
  project ever recorded for them) and for any future row whose IMAP
  fetch didn't return a parseable INTERNALDATE. Defaults to False
  (fail-closed): app.services.follow_up_eligibility now refuses to
  determine eligibility from a message whose chronology isn't True here.

Revision ID: c9e2a4f7b1d6
Revises: b3f1c9a7d5e2
Create Date: 2026-09-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e2a4f7b1d6"
down_revision: str | Sequence[str] | None = "b3f1c9a7d5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("gmail_threads") as batch_op:
        batch_op.add_column(sa.Column("lock_holder", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "provider_arrival_is_trusted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.drop_column("provider_arrival_is_trusted")

    with op.batch_alter_table("gmail_threads") as batch_op:
        batch_op.drop_column("lock_expires_at")
        batch_op.drop_column("lock_holder")
