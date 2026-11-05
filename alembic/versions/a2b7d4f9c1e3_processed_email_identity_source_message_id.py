"""Review 2.1: processed_email_messages identity is (source, message_id)

On top of f1a8c3d5e7b9. The table was UNIQUE on `message_id` alone while every
lookup and write uses `(source, message_id)`. This replaces the global unique
index with a NON-unique index on `message_id` and adds the named composite
unique constraint `uq_processed_email_messages_source_message_id`. Existing
rows satisfy it (their message_ids are globally unique); nothing is rewritten.

Downgrade restores global uniqueness and therefore REFUSES (before any DDL) when
two sources hold the same Message-ID; nothing is deleted or merged
automatically.

Revision ID: a2b7d4f9c1e3
Revises: f1a8c3d5e7b9
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2b7d4f9c1e3"
down_revision: str | Sequence[str] | None = "f1a8c3d5e7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "processed_email_messages"
_MESSAGE_ID_INDEX = "ix_processed_email_messages_message_id"
_COMPOSITE = "uq_processed_email_messages_source_message_id"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_index(_MESSAGE_ID_INDEX)
        batch_op.create_index(_MESSAGE_ID_INDEX, ["message_id"], unique=False)
        batch_op.create_unique_constraint(_COMPOSITE, ["source", "message_id"])


def downgrade() -> None:
    bind = op.get_bind()
    duplicated = bind.execute(
        sa.text(
            f"SELECT message_id FROM {_TABLE} GROUP BY message_id "  # noqa: S608 - constant
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicated is not None:
        raise RuntimeError(
            "Cannot downgrade: processed_email_messages holds the same message_id "
            "for more than one source, which the previous schema forbids. "
            "Resolve those rows manually first (nothing was changed)."
        )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_COMPOSITE, type_="unique")
        batch_op.drop_index(_MESSAGE_ID_INDEX)
        batch_op.create_index(_MESSAGE_ID_INDEX, ["message_id"], unique=True)
