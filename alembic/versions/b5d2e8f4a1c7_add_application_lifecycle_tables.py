"""add application lifecycle tables (Stage 15)

Stage 15 (application lifecycle & response tracking) adds two tables:

- `applications`: one real job application, identified by its stable
  external vacancy identity UNIQUE(source, source_reference), with the
  current lifecycle status as a cached projection of its event history.
- `application_events`: the append-only history. UNIQUE(application_id,
  sequence) orders events per application; UNIQUE(correlation_key) is the
  idempotency backbone (one event per piece of evidence, e.g. one inbound
  Gmail message).

Purely additive: no existing table or row is touched; `jobs.status` is not
changed. Downgrade drops both tables (events first) and therefore discards
any recorded lifecycle history -- only run it on a disposable database or
after exporting `application_events`.

Revision ID: b5d2e8f4a1c7
Revises: a7c3e9f1b2d4
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5d2e8f4a1c7"
down_revision: str | Sequence[str] | None = "a7c3e9f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of the Stage 15 vocabularies -- a migration must not import
# application code that may change after it ships.
_STATUSES = (
    "DISCOVERED",
    "MATCHED",
    "PACKAGE_READY",
    "APPROVED",
    "SUBMITTED",
    "RECEIVED",
    "UNDER_REVIEW",
    "INTERVIEW",
    "TECHNICAL_INTERVIEW",
    "OFFER",
    "REJECTED",
    "WITHDRAWN",
)
_CHANNELS = ("EMPLOYER_PORTAL", "EMAIL", "JOB_BOARD", "OTHER")
_EVENT_TYPES = ("CREATED", "STATUS_CHANGED", "STATUS_OVERRIDDEN", "INBOUND_MESSAGE")
_ORIGINS = ("SYSTEM", "GMAIL", "PORTAL", "OPERATOR", "IMPORT")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("company_name", sa.String(300), nullable=False),
        sa.Column("role_title", sa.String(300), nullable=False),
        sa.Column("location", sa.String(300), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_reference", sa.String(64), nullable=True),
        sa.Column("external_job_id", sa.String(64), nullable=True),
        sa.Column("vacancy_fingerprint", sa.String(64), nullable=True),
        sa.Column("application_channel", sa.String(20), nullable=False),
        sa.Column("destination_url", sa.Text(), nullable=True),
        sa.Column("employer_domains_json", sa.Text(), nullable=False),
        sa.Column("review_package_id", sa.Integer(), nullable=True),
        sa.Column("current_status", sa.String(30), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_employer_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_followup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("followup_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_reference", name="uq_applications_source_reference"),
        sa.CheckConstraint(
            _in("current_status", _STATUSES), name="ck_applications_current_status_valid"
        ),
        sa.CheckConstraint(
            _in("application_channel", _CHANNELS), name="ck_applications_channel_valid"
        ),
        sa.CheckConstraint("version >= 1", name="ck_applications_version_positive"),
        sa.CheckConstraint(
            "received_at IS NULL OR (submitted_at IS NOT NULL AND received_at >= submitted_at)",
            name="ck_applications_submitted_before_received",
        ),
    )
    op.create_index("ix_applications_current_status", "applications", ["current_status"])
    op.create_index("ix_applications_company_name", "applications", ["company_name"])
    op.create_index("ix_applications_external_job_id", "applications", ["external_job_id"])
    op.create_index("ix_applications_created_at", "applications", ["created_at"])
    op.create_index(
        "ix_applications_last_status_changed_at", "applications", ["last_status_changed_at"]
    )

    op.create_table(
        "application_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("old_status", sa.String(30), nullable=True),
        sa.Column("new_status", sa.String(30), nullable=True),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=True),
        sa.Column("source_reference", sa.String(200), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=True),
        sa.Column("evidence_summary", sa.String(500), nullable=True),
        sa.Column("correlation_key", sa.String(100), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("application_id", "sequence", name="uq_application_events_sequence"),
        sa.UniqueConstraint("correlation_key", name="uq_application_events_correlation_key"),
        sa.CheckConstraint(
            _in("event_type", _EVENT_TYPES), name="ck_application_events_type_valid"
        ),
        sa.CheckConstraint(_in("origin", _ORIGINS), name="ck_application_events_origin_valid"),
        sa.CheckConstraint(
            "old_status IS NULL OR " + _in("old_status", _STATUSES),
            name="ck_application_events_old_status_valid",
        ),
        sa.CheckConstraint(
            "new_status IS NULL OR " + _in("new_status", _STATUSES),
            name="ck_application_events_new_status_valid",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_application_events_sequence_positive"),
        sa.CheckConstraint(
            "(event_type = 'CREATED' AND old_status IS NULL AND new_status IS NOT NULL) OR "
            "(event_type IN ('STATUS_CHANGED', 'STATUS_OVERRIDDEN') "
            "AND old_status IS NOT NULL AND new_status IS NOT NULL) OR "
            "(event_type = 'INBOUND_MESSAGE' AND new_status IS NULL)",
            name="ck_application_events_shape",
        ),
        sa.CheckConstraint(
            "event_type <> 'STATUS_OVERRIDDEN' OR (reason IS NOT NULL AND reason <> '')",
            name="ck_application_events_override_reason",
        ),
    )
    op.create_index(
        "ix_application_events_application_occurred",
        "application_events",
        ["application_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_application_events_application_occurred", table_name="application_events")
    op.drop_table("application_events")
    for name in (
        "ix_applications_last_status_changed_at",
        "ix_applications_created_at",
        "ix_applications_external_job_id",
        "ix_applications_company_name",
        "ix_applications_current_status",
    ):
        op.drop_index(name, table_name="applications")
    op.drop_table("applications")
