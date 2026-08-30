"""add candidate cv drafts table

Revision ID: db47a801596b
Revises: ececa0eab87a
Create Date: 2026-08-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db47a801596b"
down_revision: str | Sequence[str] | None = "ececa0eab87a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "candidate_cv_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_version", sa.Integer(), nullable=False),
        sa.Column("job_snapshot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("match_algorithm_version", sa.String(length=20), nullable=False),
        sa.Column("cv_adapter_version", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "match_id",
            "cv_adapter_version",
            name="uq_candidate_cv_drafts_cache_identity",
        ),
    )
    op.create_index(
        op.f("ix_candidate_cv_drafts_job_id"), "candidate_cv_drafts", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_candidate_cv_drafts_match_id"), "candidate_cv_drafts", ["match_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_candidate_cv_drafts_match_id"), table_name="candidate_cv_drafts")
    op.drop_index(op.f("ix_candidate_cv_drafts_job_id"), table_name="candidate_cv_drafts")
    op.drop_table("candidate_cv_drafts")
