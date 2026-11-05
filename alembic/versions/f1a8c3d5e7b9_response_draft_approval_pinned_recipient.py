"""Review 1.3: pin the reply recipient on response-draft approvals

Additive on top of e3b7c9d1f4a6: `response_draft_approvals.pinned_recipient`
(String(320), NOT NULL, server default ''). Existing approvals keep '' and are
therefore never sendable (fail closed); nothing is back-filled and no decision
is changed. The downgrade drops the column.

Revision ID: f1a8c3d5e7b9
Revises: e3b7c9d1f4a6
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a8c3d5e7b9"
down_revision: str | Sequence[str] | None = "e3b7c9d1f4a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "response_draft_approvals",
        sa.Column("pinned_recipient", sa.String(length=320), nullable=False, server_default=""),
    )


def downgrade() -> None:
    with op.batch_alter_table("response_draft_approvals") as batch_op:
        batch_op.drop_column("pinned_recipient")
