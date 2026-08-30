"""add candidate profile tables

Revision ID: fa99eefca6bd
Revises: a1c9e3f7b2d4
Create Date: 2026-08-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fa99eefca6bd"
down_revision: str | Sequence[str] | None = "a1c9e3f7b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("professional_title", sa.String(length=300), nullable=True),
        sa.Column("location_city", sa.String(length=200), nullable=True),
        sa.Column("location_country", sa.String(length=200), nullable=True),
        sa.Column("professional_summary", sa.Text(), nullable=False),
        sa.Column("career_goal", sa.Text(), nullable=False),
        sa.Column("target_roles_json", sa.Text(), nullable=False),
        sa.Column("field_trust_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_candidate_profiles_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "candidate_certifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("issuer", sa.String(length=300), nullable=True),
        sa.Column("issued_date", sa.Date(), nullable=True),
        sa.Column("expires_date", sa.Date(), nullable=True),
        sa.Column("credential_id", sa.String(length=200), nullable=True),
        sa.Column("credential_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "candidate_education",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("institution", sa.String(length=300), nullable=False),
        sa.Column("program", sa.String(length=300), nullable=True),
        sa.Column("degree", sa.String(length=200), nullable=True),
        sa.Column("field_of_study", sa.String(length=300), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "candidate_experiences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("company", sa.String(length=300), nullable=False),
        sa.Column("job_title", sa.String(length=300), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("responsibilities_json", sa.Text(), nullable=False),
        sa.Column("achievements_json", sa.Text(), nullable=False),
        sa.Column("technologies_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "candidate_job_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("preferred_locations_json", sa.Text(), nullable=False),
        sa.Column("remote_preference", sa.String(length=20), nullable=False),
        sa.Column("employment_types_json", sa.Text(), nullable=False),
        sa.Column("minimum_salary", sa.Float(), nullable=True),
        sa.Column("salary_currency", sa.String(length=10), nullable=True),
        sa.Column("relocation", sa.Boolean(), nullable=True),
        sa.Column("travel", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_profile_id"),
    )
    op.create_table(
        "candidate_languages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=100), nullable=False),
        sa.Column("normalized_language", sa.String(length=100), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("certificate", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_profile_id",
            "normalized_language",
            name="uq_candidate_languages_profile_language",
        ),
    )
    op.create_index(
        op.f("ix_candidate_languages_normalized_language"),
        "candidate_languages",
        ["normalized_language"],
        unique=False,
    )
    op.create_table(
        "candidate_projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=300), nullable=True),
        sa.Column("technologies_json", sa.Text(), nullable=False),
        sa.Column("repository_url", sa.Text(), nullable=True),
        sa.Column("demo_url", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("highlights_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "candidate_skills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("proficiency", sa.String(length=20), nullable=False),
        sa.Column("years_experience", sa.Float(), nullable=True),
        sa.Column("last_used_year", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_profile_id", "normalized_name", name="uq_candidate_skills_profile_name"
        ),
    )
    op.create_index(
        op.f("ix_candidate_skills_normalized_name"),
        "candidate_skills",
        ["normalized_name"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_candidate_skills_normalized_name"), table_name="candidate_skills")
    op.drop_table("candidate_skills")
    op.drop_table("candidate_projects")
    op.drop_index(
        op.f("ix_candidate_languages_normalized_language"), table_name="candidate_languages"
    )
    op.drop_table("candidate_languages")
    op.drop_table("candidate_job_preferences")
    op.drop_table("candidate_experiences")
    op.drop_table("candidate_education")
    op.drop_table("candidate_certifications")
    op.drop_table("candidate_profiles")
