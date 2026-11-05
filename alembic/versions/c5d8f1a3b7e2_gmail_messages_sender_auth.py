"""Task 21: gmail_messages.sender_auth records what Authentication-Results proved

Additive on top of b4c7e9a1d3f5: `gmail_messages.sender_auth` (String(16), NULL) with a
CHECK limiting it to DMARC_ALIGNED / FAILED / UNVERIFIED. Existing rows keep NULL, which
reads as "unverified"; nothing is back-filled or re-evaluated (the raw header is not stored).
The downgrade drops the constraint and the column.

Revision ID: c5d8f1a3b7e2
Revises: b4c7e9a1d3f5
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5d8f1a3b7e2"
down_revision: str | Sequence[str] | None = "b4c7e9a1d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_gmail_messages_sender_auth_valid"


def upgrade() -> None:
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.add_column(sa.Column("sender_auth", sa.String(length=16), nullable=True))
        batch_op.create_check_constraint(
            _CHECK,
            "sender_auth IS NULL OR sender_auth IN ('DMARC_ALIGNED', 'FAILED', 'UNVERIFIED')",
        )


def downgrade() -> None:
    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_column("sender_auth")
