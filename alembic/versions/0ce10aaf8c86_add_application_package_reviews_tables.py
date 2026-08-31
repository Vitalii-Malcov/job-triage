"""add application package reviews tables

Revision ID: 0ce10aaf8c86
Revises: 2a3383bb29c5
Create Date: 2026-08-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0ce10aaf8c86"
down_revision: str | Sequence[str] | None = "2a3383bb29c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "application_package_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("cv_draft_id", sa.Integer(), nullable=False),
        sa.Column("bewerbung_draft_id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_version", sa.Integer(), nullable=False),
        sa.Column("job_snapshot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("match_algorithm_version", sa.String(length=20), nullable=False),
        sa.Column("cv_adapter_version", sa.String(length=20), nullable=False),
        sa.Column("bewerbung_generator_version", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("has_manual_overrides", sa.Boolean(), nullable=False),
        sa.Column("approved_revision_id", sa.Integer(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_application_package_reviews_job_id"),
        "application_package_reviews",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_package_reviews_cv_draft_id"),
        "application_package_reviews",
        ["cv_draft_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_package_reviews_bewerbung_draft_id"),
        "application_package_reviews",
        ["bewerbung_draft_id"],
        unique=False,
    )

    op.create_table(
        "application_package_review_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("reviewed_cv_json", sa.Text(), nullable=False),
        sa.Column("reviewed_bewerbung_json", sa.Text(), nullable=False),
        sa.Column("manual_override_paths_json", sa.Text(), nullable=False),
        sa.Column("edit_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "revision_number",
            name="uq_application_package_review_revisions_number",
        ),
    )
    op.create_index(
        op.f("ix_application_package_review_revisions_review_id"),
        "application_package_review_revisions",
        ["review_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_application_package_review_revisions_review_id"),
        table_name="application_package_review_revisions",
    )
    op.drop_table("application_package_review_revisions")

    op.drop_index(
        op.f("ix_application_package_reviews_bewerbung_draft_id"),
        table_name="application_package_reviews",
    )
    op.drop_index(
        op.f("ix_application_package_reviews_cv_draft_id"),
        table_name="application_package_reviews",
    )
    op.drop_index(
        op.f("ix_application_package_reviews_job_id"),
        table_name="application_package_reviews",
    )
    op.drop_table("application_package_reviews")
