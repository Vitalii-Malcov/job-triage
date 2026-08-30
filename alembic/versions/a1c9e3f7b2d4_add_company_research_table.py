"""add company_research table

Revision ID: a1c9e3f7b2d4
Revises: c4e72b1a8d9f
Create Date: 2026-08-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c9e3f7b2d4"
down_revision: str | Sequence[str] | None = "c4e72b1a8d9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "company_research",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("identity_key", sa.String(length=350), nullable=False),
        sa.Column("normalized_company_name", sa.String(length=300), nullable=False),
        sa.Column("normalized_domain", sa.String(length=300), nullable=True),
        sa.Column("company_name", sa.String(length=300), nullable=False),
        sa.Column("company_domain", sa.String(length=300), nullable=True),
        sa.Column("industry", sa.String(length=200), nullable=True),
        sa.Column("headquarters", sa.String(length=300), nullable=True),
        sa.Column("company_size", sa.String(length=100), nullable=True),
        sa.Column("short_summary", sa.Text(), server_default="", nullable=False),
        sa.Column("products_or_services_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("technologies_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("hiring_signals_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("relevant_facts_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("positive_signals_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("risk_signals_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("source_urls_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("evidence_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.0", nullable=False),
        sa.Column(
            "research_status", sa.String(length=20), server_default="PENDING", nullable=False
        ),
        sa.Column("provider_name", sa.String(length=100), nullable=False),
        sa.Column("researched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_status", sa.String(length=20), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # Matches app/db/models.py's CompanyResearchRecord.identity_key
        # (mapped_column(..., unique=True)) exactly — a UniqueConstraint,
        # not a unique index, so `alembic check` sees no model/migration
        # drift. Mirrors jobs.fingerprint's uq_jobs_fingerprint below.
        sa.UniqueConstraint("identity_key", name="uq_company_research_identity_key"),
    )
    op.create_index(
        op.f("ix_company_research_normalized_company_name"),
        "company_research",
        ["normalized_company_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_company_research_normalized_domain"),
        "company_research",
        ["normalized_domain"],
        unique=False,
    )
    # RR-M-01: DB-backed atomic coordination for the "mixed name/domain
    # creation race" — see app/db/models.py's CompanyResearchIdentityAlias
    # and app/db/repositories.py's _create_company_research /
    # _join_or_diverge_after_alias_conflict.
    op.create_table(
        "company_research_identity_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("normalized_company_name", sa.String(length=300), nullable=False),
        sa.Column("company_research_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["company_research_id"],
            ["company_research.id"],
            name="fk_company_research_identity_aliases_company_research_id",
        ),
        sa.UniqueConstraint(
            "normalized_company_name",
            name="uq_company_research_identity_aliases_normalized_company_name",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("company_research_identity_aliases")
    op.drop_index(op.f("ix_company_research_normalized_domain"), table_name="company_research")
    op.drop_index(
        op.f("ix_company_research_normalized_company_name"), table_name="company_research"
    )
    op.drop_table("company_research")
