"""add candidate job matches table

Revision ID: ececa0eab87a
Revises: fa99eefca6bd
Create Date: 2026-08-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ececa0eab87a"
down_revision: str | Sequence[str] | None = "fa99eefca6bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "candidate_job_matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_version", sa.Integer(), nullable=False),
        sa.Column("job_snapshot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("algorithm_version", sa.String(length=20), nullable=False),
        sa.Column("company_research_id", sa.Integer(), nullable=True),
        sa.Column("overall_score", sa.Integer(), nullable=False),
        sa.Column("coverage_score", sa.Integer(), nullable=False),
        sa.Column("required_skill_score", sa.Integer(), nullable=False),
        sa.Column("preferred_skill_score", sa.Integer(), nullable=False),
        sa.Column("analysis_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "candidate_profile_version",
            "job_snapshot_fingerprint",
            "algorithm_version",
            name="uq_candidate_job_matches_cache_identity",
        ),
    )
    op.create_index(
        op.f("ix_candidate_job_matches_job_id"),
        "candidate_job_matches",
        ["job_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_candidate_job_matches_job_id"), table_name="candidate_job_matches")
    op.drop_table("candidate_job_matches")
