"""jobs.full_time_offered: the source's own full-time flag

Additive only, on top of d6e9a2b4c8f1: one nullable boolean `jobs` column for the
Bundesagentur `arbeitszeitVollzeit` flag. No existing row is changed; the next
collector run (or the BA refresh CLI) fills it. Plain ALTER TABLE like
e3b7c9d1f4a6 (no batch rebuild of `jobs` under its foreign keys). The downgrade
drops the column (the value comes back with the next run).

Revision ID: a7c3e5f1b9d2
Revises: d6e9a2b4c8f1
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e5f1b9d2"
down_revision: str | Sequence[str] | None = "d6e9a2b4c8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("full_time_offered", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "full_time_offered")
