"""Stage 17: persistent candidate search configuration

Additive only -- no existing column, row, index or constraint changes:

- `candidate_profiles`: contact columns (email, phone, website) plus their
  provenance `contact_trust_json`;
- `candidate_job_preferences`: the search configuration (home/commute origin,
  car-route limit, relocation regions, remote scope, salary range, earliest
  start date, primary/secondary roles) plus `search_trust_json`;
- `candidate_profile_revisions`: append-only audit of configuration changes
  (UPDATE/DELETE refused by triggers on SQLite and PostgreSQL);
- `job_commute_checks`: verified car-route measurements per job.

New NOT NULL columns carry a server default, so existing rows stay valid
and unchanged in meaning (an empty list / "UNKNOWN" = never stated).

Downgrade refuses while any revision or commute check exists (audit and
verification data are never silently discarded); otherwise it removes
exactly what upgrade added.

Revision ID: a9d3f6b2c4e8
Revises: f4b8d2a6c9e1
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9d3f6b2c4e8"
down_revision: str | Sequence[str] | None = "f4b8d2a6c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _profile_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("contact_email", sa.String(320), nullable=True),
        sa.Column("contact_phone", sa.String(40), nullable=True),
        sa.Column("contact_website_url", sa.Text(), nullable=True),
        sa.Column("contact_trust_json", sa.Text(), nullable=False, server_default="{}"),
    )


def _preference_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("home_city", sa.String(200), nullable=True),
        sa.Column("home_postal_code", sa.String(20), nullable=True),
        sa.Column("home_region", sa.String(100), nullable=True),
        sa.Column("commute_origin_label", sa.String(300), nullable=True),
        sa.Column("commute_origin_lat", sa.Float(), nullable=True),
        sa.Column("commute_origin_lon", sa.Float(), nullable=True),
        sa.Column("max_commute_km", sa.Integer(), nullable=True),
        sa.Column("commute_measure", sa.String(20), nullable=True),
        sa.Column("relocation_regions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("remote_work_scope", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("remote_work_from_region", sa.String(100), nullable=True),
        sa.Column("maximum_salary", sa.Float(), nullable=True),
        sa.Column("salary_negotiable", sa.Boolean(), nullable=True),
        sa.Column("earliest_start_date", sa.Date(), nullable=True),
        sa.Column("primary_roles_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("secondary_roles_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("search_trust_json", sa.Text(), nullable=False, server_default="{}"),
    )


# Frozen copy of app/db/models.py's CANDIDATE_PROFILE_REVISION_IMMUTABILITY_DDL.
_MESSAGE = "candidate_profile_revisions is append-only"
_TRIGGERS = {
    "sqlite": (
        "CREATE TRIGGER trg_candidate_profile_revisions_no_update BEFORE UPDATE ON "
        f"candidate_profile_revisions BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
        "CREATE TRIGGER trg_candidate_profile_revisions_no_delete BEFORE DELETE ON "
        f"candidate_profile_revisions BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
    ),
    "postgresql": (
        "CREATE FUNCTION candidate_profile_revisions_append_only() RETURNS trigger "
        f"LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{_MESSAGE}' "
        "USING ERRCODE = 'restrict_violation'; END $$",
        "CREATE TRIGGER trg_candidate_profile_revisions_append_only BEFORE UPDATE OR DELETE "
        "ON candidate_profile_revisions FOR EACH ROW "
        "EXECUTE FUNCTION candidate_profile_revisions_append_only()",
    ),
}
_DROP_TRIGGERS = {
    "sqlite": (
        "DROP TRIGGER IF EXISTS trg_candidate_profile_revisions_no_update",
        "DROP TRIGGER IF EXISTS trg_candidate_profile_revisions_no_delete",
    ),
    "postgresql": (
        "DROP TRIGGER IF EXISTS trg_candidate_profile_revisions_append_only "
        "ON candidate_profile_revisions",
        "DROP FUNCTION IF EXISTS candidate_profile_revisions_append_only()",
    ),
}


def _dialect() -> str:
    dialect = op.get_bind().dialect.name
    if dialect not in _TRIGGERS:
        raise RuntimeError(f"unsupported dialect for candidate_profile_revisions: {dialect}")
    return dialect


def upgrade() -> None:
    dialect = _dialect()
    for column in _profile_columns():
        op.add_column("candidate_profiles", column)
    for column in _preference_columns():
        op.add_column("candidate_job_preferences", column)

    op.create_table(
        "candidate_profile_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "candidate_profile_id",
            sa.Integer(),
            sa.ForeignKey("candidate_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("changed_fields_json", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("after_json", sa.Text(), nullable=False),
    )
    for statement in _TRIGGERS[dialect]:
        op.execute(statement)

    op.create_table(
        "job_commute_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin_label", sa.String(300), nullable=False),
        sa.Column("destination_label", sa.String(300), nullable=False),
        sa.Column("destination_region", sa.String(100), nullable=True),
        sa.Column("car_route_km", sa.Float(), nullable=False),
        sa.Column("car_route_minutes", sa.Float(), nullable=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_commute_checks_job_id", "job_commute_checks", ["job_id"])


def downgrade() -> None:
    dialect = _dialect()
    bind = op.get_bind()
    for table in ("candidate_profile_revisions", "job_commute_checks"):
        rows = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if rows:
            raise RuntimeError(
                f"cannot downgrade: {table} holds {rows} row(s); restore a pre-migration "
                "backup instead of discarding audit/verification data"
            )
    op.drop_index("ix_job_commute_checks_job_id", table_name="job_commute_checks")
    op.drop_table("job_commute_checks")
    for statement in _DROP_TRIGGERS[dialect]:
        op.execute(statement)
    op.drop_table("candidate_profile_revisions")
    with op.batch_alter_table("candidate_job_preferences") as batch_op:
        for column in reversed(_preference_columns()):
            batch_op.drop_column(column.name)
    with op.batch_alter_table("candidate_profiles") as batch_op:
        for column in reversed(_profile_columns()):
            batch_op.drop_column(column.name)
