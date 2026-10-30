"""Stage 22 P0-1: persist the source's own job reference

Additive only, on top of c7e2a9d4f1b3: a nullable `jobs.source_reference`
(String(64)) plus a non-unique index on (source, source_reference). No
existing row is changed; existing rows are filled by the separate,
operator-run `scripts/backfill_source_reference.py`. The downgrade drops the
index and the column (the value is re-derivable from BA URLs or the next
collector run).

Revision ID: d5f1a7c3e9b2
Revises: c7e2a9d4f1b3
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5f1a7c3e9b2"
down_revision: str | Sequence[str] | None = "c7e2a9d4f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ALTER TABLE, like b2c5d8e4f7a1 (jobs.posting_type): no batch-mode
    # table rebuild, which would drop and recreate `jobs` under its FKs.
    op.add_column("jobs", sa.Column("source_reference", sa.String(64), nullable=True))
    op.create_index("ix_jobs_source_source_reference", "jobs", ["source", "source_reference"])


def downgrade() -> None:
    op.drop_index("ix_jobs_source_source_reference", table_name="jobs")
    op.drop_column("jobs", "source_reference")
