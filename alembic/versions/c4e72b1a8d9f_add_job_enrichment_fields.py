"""add job enrichment fields

Revision ID: c4e72b1a8d9f
Revises: 9fd80046ea7e
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e72b1a8d9f"
down_revision: str | Sequence[str] | None = "9fd80046ea7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "jobs",
        sa.Column("data_confidence", sa.Float(), server_default="0.0", nullable=False),
    )
    op.add_column("jobs", sa.Column("skill_source", sa.String(length=30), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("must_have_skills_json", sa.Text(), server_default="[]", nullable=False),
    )
    op.add_column(
        "jobs",
        sa.Column("nice_to_have_skills_json", sa.Text(), server_default="[]", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("jobs", "nice_to_have_skills_json")
    op.drop_column("jobs", "must_have_skills_json")
    op.drop_column("jobs", "skill_source")
    op.drop_column("jobs", "data_confidence")
