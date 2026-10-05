"""add telegram_bewerbung_approvals table (Stage 9C Telegram review approval)

Adds `telegram_bewerbung_approvals` -- the immutable, historical link from
one Stage 9B package generation to the one Stage 6E review created for it
from Telegram, plus its opaque decision capability (see
app.db.models.TelegramBewerbungApprovalRecord's docstring). It holds no
approval state: Stage 6E stays the only approval state machine.

`preparation_id` and `review_id` are deliberately NOT foreign keys, so
operational cleanup of Stage 9B/9A rows never erases approval lineage.

No existing table is altered; no backfill.

Revision ID: e5a17c3b9d42
Revises: d7b3e9a2c5f1
Create Date: 2026-10-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a17c3b9d42"
down_revision: str | Sequence[str] | None = "d7b3e9a2c5f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "telegram_bewerbung_approvals"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("preparation_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("package_token", sa.String(length=32), nullable=False),
        sa.Column("input_identity", sa.String(length=64), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("bound_review_version", sa.Integer(), nullable=False),
        sa.Column("approval_capability", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "preparation_id",
            "generation",
            name="uq_telegram_bewerbung_approvals_preparation_generation",
        ),
        sa.UniqueConstraint("review_id", name="uq_telegram_bewerbung_approvals_review_id"),
        sa.UniqueConstraint(
            "approval_capability",
            name="uq_telegram_bewerbung_approvals_approval_capability",
        ),
        sa.CheckConstraint(
            "generation >= 1", name="ck_telegram_bewerbung_approvals_generation_positive"
        ),
        sa.CheckConstraint(
            "bound_review_version = 1",
            name="ck_telegram_bewerbung_approvals_bound_review_version",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table(_TABLE)
