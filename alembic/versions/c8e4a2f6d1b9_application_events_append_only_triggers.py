"""make application_events append-only in the database (Stage 15 remediation)

Codex S15 P2: the ORM before_update/before_delete guards on
ApplicationEventRecord do not see Core statements such as
`Session.execute(update(ApplicationEventRecord)...)`, so the append-only
history could be rewritten by a bulk statement. This migration moves the
guarantee into the database:

- SQLite: BEFORE UPDATE and BEFORE DELETE triggers that RAISE(ABORT).
- PostgreSQL: one plpgsql function raising an exception, attached as a
  BEFORE UPDATE OR DELETE ... FOR EACH ROW trigger.

Every UPDATE/DELETE of an application_events row is refused for every
client. (PostgreSQL TRUNCATE is a privileged maintenance statement that row
triggers do not see; the application never issues it.) Downgrade drops the
triggers/function only; no row is touched.

Additive on purpose: the reviewed b5d2e8f4a1c7 migration is not rewritten.

Revision ID: c8e4a2f6d1b9
Revises: b5d2e8f4a1c7
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c8e4a2f6d1b9"
down_revision: str | Sequence[str] | None = "b5d2e8f4a1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.db.models.APPLICATION_EVENT_IMMUTABILITY_DDL.
_MESSAGE = "application_events is append-only"
_SQLITE_UPGRADE = (
    "CREATE TRIGGER trg_application_events_no_update BEFORE UPDATE ON application_events "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
    "CREATE TRIGGER trg_application_events_no_delete BEFORE DELETE ON application_events "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
)
_SQLITE_DOWNGRADE = (
    "DROP TRIGGER IF EXISTS trg_application_events_no_update",
    "DROP TRIGGER IF EXISTS trg_application_events_no_delete",
)
_POSTGRES_UPGRADE = (
    "CREATE FUNCTION application_events_append_only() RETURNS trigger LANGUAGE plpgsql AS "
    f"$$ BEGIN RAISE EXCEPTION '{_MESSAGE}' USING ERRCODE = 'restrict_violation'; END $$",
    "CREATE TRIGGER trg_application_events_append_only BEFORE UPDATE OR DELETE "
    "ON application_events FOR EACH ROW EXECUTE FUNCTION application_events_append_only()",
)
_POSTGRES_DOWNGRADE = (
    "DROP TRIGGER IF EXISTS trg_application_events_append_only ON application_events",
    "DROP FUNCTION IF EXISTS application_events_append_only()",
)


def _statements(sqlite: tuple[str, ...], postgres: tuple[str, ...]) -> tuple[str, ...]:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite
    if dialect == "postgresql":
        return postgres
    raise RuntimeError(f"unsupported dialect for application_events triggers: {dialect}")


def upgrade() -> None:
    for statement in _statements(_SQLITE_UPGRADE, _POSTGRES_UPGRADE):
        op.execute(statement)


def downgrade() -> None:
    for statement in _statements(_SQLITE_DOWNGRADE, _POSTGRES_DOWNGRADE):
        op.execute(statement)
