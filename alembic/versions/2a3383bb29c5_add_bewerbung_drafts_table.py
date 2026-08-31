"""add bewerbung drafts table

Revision ID: 2a3383bb29c5
Revises: db47a801596b
Create Date: 2026-08-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2a3383bb29c5"
down_revision: str | Sequence[str] | None = "db47a801596b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bewerbung_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("cv_draft_id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_version", sa.Integer(), nullable=False),
        sa.Column("job_snapshot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("match_algorithm_version", sa.String(length=20), nullable=False),
        sa.Column("cv_adapter_version", sa.String(length=20), nullable=False),
        sa.Column("bewerbung_generator_version", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_bewerbung_drafts_job_id"), "bewerbung_drafts", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_bewerbung_drafts_cv_draft_id"), "bewerbung_drafts", ["cv_draft_id"], unique=False
    )
    op.create_index(
        op.f("ix_bewerbung_drafts_match_id"), "bewerbung_drafts", ["match_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_bewerbung_drafts_match_id"), table_name="bewerbung_drafts")
    op.drop_index(op.f("ix_bewerbung_drafts_cv_draft_id"), table_name="bewerbung_drafts")
    op.drop_index(op.f("ix_bewerbung_drafts_job_id"), table_name="bewerbung_drafts")
    op.drop_table("bewerbung_drafts")
