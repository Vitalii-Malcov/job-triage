"""Stage 21: persistent vacancy assessment cache

Additive only, on top of b6c1e9a4d2f7: one new table `job_fit_assessments`
(one current `assess_job` result per vacancy, keyed by an input hash, see
`app/services/fit_cache.py`). No existing table or row changes. The table is
a cache that can always be recomputed from the other tables, so the
downgrade drops it.

Revision ID: c7e2a9d4f1b3
Revises: b6c1e9a4d2f7
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e2a9d4f1b3"
down_revision: str | Sequence[str] | None = "b6c1e9a4d2f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_fit_assessments",
        sa.Column("job_id", sa.Integer(), nullable=False, autoincrement=False),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("fit_tier", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("criteria_json", sa.Text(), nullable=False),
        sa.Column("tier_reasons_json", sa.Text(), nullable=False),
        sa.Column("work_mode", sa.String(30), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "fit_tier IN ('GOOD_MATCH', 'PARTIAL_MATCH', 'NEEDS_OPERATOR', 'NOT_SUITABLE')",
            name="ck_job_fit_assessments_fit_tier",
        ),
        sa.CheckConstraint(
            "decision IN ('BLOCKED', 'NEEDS_VERIFICATION', 'CANDIDATE')",
            name="ck_job_fit_assessments_decision",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "ix_job_fit_assessments_tier_job", "job_fit_assessments", ["fit_tier", "job_id"]
    )
    op.create_index("ix_job_fit_assessments_input_key", "job_fit_assessments", ["input_key"])


def downgrade() -> None:
    op.drop_index("ix_job_fit_assessments_input_key", table_name="job_fit_assessments")
    op.drop_index("ix_job_fit_assessments_tier_job", table_name="job_fit_assessments")
    op.drop_table("job_fit_assessments")
