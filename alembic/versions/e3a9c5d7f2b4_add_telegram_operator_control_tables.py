"""add Telegram operator control tables (Stage 16)

Stage 16 (Telegram operator control) adds three tables:

- `telegram_operator_cards`: one operator card per review target
  (application + exact review package), UNIQUE(target_key), with the opaque
  random callback token its buttons carry (UNIQUE(callback_token)) and the
  card's delivery state.
- `application_operator_decisions`: the append-only operator decision audit
  (APPROVE / SKIP / LATER). UNIQUE(idempotency_key) admits one final
  decision per target and override generation; UNIQUE(callback_ref) makes a
  retried Telegram callback idempotent. UPDATE/DELETE are refused by
  database triggers (same shape as c8e4a2f6d1b9).
- `telegram_lifecycle_notifications`: the notification delivery ledger,
  UNIQUE(application_event_id) -- at most one notification per Stage 15
  lifecycle event.

Purely additive: no existing table or row is touched; the reviewed Stage 15
migrations b5d2e8f4a1c7 and c8e4a2f6d1b9 are not rewritten. Downgrade drops
the three tables (and the decision triggers/function) and therefore
discards the recorded operator decisions -- only run it on a disposable
database or after exporting `application_operator_decisions`.

Revision ID: e3a9c5d7f2b4
Revises: c8e4a2f6d1b9
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3a9c5d7f2b4"
down_revision: str | Sequence[str] | None = "c8e4a2f6d1b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of the Stage 16 vocabularies -- a migration must not import
# application code that may change after it ships.
_DELIVERY_STATES = ("SENDING", "SENT", "FAILED", "UNCERTAIN")
_DECISIONS = ("APPROVE", "SKIP", "LATER")

# Frozen copies of app.db.models.OPERATOR_DECISION_IMMUTABILITY_DDL.
_MESSAGE = "application_operator_decisions is append-only"
_SQLITE_TRIGGERS = (
    "CREATE TRIGGER trg_application_operator_decisions_no_update BEFORE UPDATE ON "
    f"application_operator_decisions BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
    "CREATE TRIGGER trg_application_operator_decisions_no_delete BEFORE DELETE ON "
    f"application_operator_decisions BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
)
_SQLITE_DROP = (
    "DROP TRIGGER IF EXISTS trg_application_operator_decisions_no_update",
    "DROP TRIGGER IF EXISTS trg_application_operator_decisions_no_delete",
)
_POSTGRES_TRIGGERS = (
    "CREATE FUNCTION application_operator_decisions_append_only() RETURNS trigger "
    f"LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{_MESSAGE}' "
    "USING ERRCODE = 'restrict_violation'; END $$",
    "CREATE TRIGGER trg_application_operator_decisions_append_only BEFORE UPDATE OR DELETE "
    "ON application_operator_decisions FOR EACH ROW "
    "EXECUTE FUNCTION application_operator_decisions_append_only()",
)
_POSTGRES_DROP = (
    "DROP TRIGGER IF EXISTS trg_application_operator_decisions_append_only "
    "ON application_operator_decisions",
    "DROP FUNCTION IF EXISTS application_operator_decisions_append_only()",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _dialect_statements(sqlite: tuple[str, ...], postgres: tuple[str, ...]) -> tuple[str, ...]:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite
    if dialect == "postgresql":
        return postgres
    raise NotImplementedError(f"unsupported dialect for Stage 16 triggers: {dialect}")


def upgrade() -> None:
    op.create_table(
        "telegram_operator_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey(
                "applications.id",
                ondelete="RESTRICT",
                name="fk_telegram_operator_cards_application_id",
            ),
            nullable=False,
        ),
        sa.Column("review_package_id", sa.Integer(), nullable=True),
        sa.Column("target_key", sa.String(80), nullable=False),
        sa.Column("callback_token", sa.String(32), nullable=False),
        sa.Column("delivery_state", sa.String(20), nullable=False),
        sa.Column("approve_offered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("card_text_sha256", sa.String(64), nullable=True),
        sa.Column("last_error", sa.String(60), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("target_key", name="uq_telegram_operator_cards_target_key"),
        sa.UniqueConstraint("callback_token", name="uq_telegram_operator_cards_callback_token"),
        sa.CheckConstraint(
            _in("delivery_state", _DELIVERY_STATES),
            name="ck_telegram_operator_cards_state_valid",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_telegram_operator_cards_attempts_nonnegative"
        ),
    )
    op.create_index(
        "ix_telegram_operator_cards_application_id",
        "telegram_operator_cards",
        ["application_id"],
    )

    op.create_table(
        "application_operator_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey(
                "applications.id",
                ondelete="RESTRICT",
                name="fk_application_operator_decisions_application_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "card_id",
            sa.Integer(),
            sa.ForeignKey(
                "telegram_operator_cards.id",
                ondelete="RESTRICT",
                name="fk_application_operator_decisions_card_id",
            ),
            nullable=True,
        ),
        sa.Column("review_package_id", sa.Integer(), nullable=True),
        sa.Column("target_key", sa.String(80), nullable=False),
        sa.Column("vacancy_reference", sa.String(200), nullable=True),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column(
            "supersedes_decision_id",
            sa.Integer(),
            sa.ForeignKey(
                "application_operator_decisions.id",
                ondelete="RESTRICT",
                name="fk_application_operator_decisions_supersedes",
            ),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("callback_ref", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(40), nullable=False),
        sa.Column("operator_ref", sa.String(64), nullable=True),
        sa.Column("chat_ref", sa.String(64), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("lifecycle_correlation_key", sa.String(100), nullable=True),
        sa.Column("reason", sa.String(1000), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_application_operator_decisions_idempotency_key"
        ),
        sa.UniqueConstraint("callback_ref", name="uq_application_operator_decisions_callback_ref"),
        sa.CheckConstraint(
            _in("decision", _DECISIONS),
            name="ck_application_operator_decisions_decision_valid",
        ),
        sa.CheckConstraint(
            "supersedes_decision_id IS NULL OR reason IS NOT NULL",
            name="ck_application_operator_decisions_override_reason",
        ),
    )
    op.create_index(
        "ix_application_operator_decisions_target_key",
        "application_operator_decisions",
        ["target_key"],
    )
    op.create_index(
        "ix_application_operator_decisions_application_id",
        "application_operator_decisions",
        ["application_id"],
    )
    for statement in _dialect_statements(_SQLITE_TRIGGERS, _POSTGRES_TRIGGERS):
        op.execute(statement)

    op.create_table(
        "telegram_lifecycle_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_event_id",
            sa.Integer(),
            sa.ForeignKey(
                "application_events.id",
                ondelete="RESTRICT",
                name="fk_telegram_lifecycle_notifications_event_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey(
                "applications.id",
                ondelete="RESTRICT",
                name="fk_telegram_lifecycle_notifications_application_id",
            ),
            nullable=False,
        ),
        sa.Column("delivery_state", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("text_sha256", sa.String(64), nullable=True),
        sa.Column("last_error", sa.String(60), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "application_event_id", name="uq_telegram_lifecycle_notifications_event_id"
        ),
        sa.CheckConstraint(
            _in("delivery_state", _DELIVERY_STATES),
            name="ck_telegram_lifecycle_notifications_state_valid",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_telegram_lifecycle_notifications_attempts_nonnegative",
        ),
    )
    op.create_index(
        "ix_telegram_lifecycle_notifications_application_id",
        "telegram_lifecycle_notifications",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_lifecycle_notifications_application_id",
        table_name="telegram_lifecycle_notifications",
    )
    op.drop_table("telegram_lifecycle_notifications")
    for statement in _dialect_statements(_SQLITE_DROP, _POSTGRES_DROP):
        op.execute(statement)
    op.drop_index(
        "ix_application_operator_decisions_application_id",
        table_name="application_operator_decisions",
    )
    op.drop_index(
        "ix_application_operator_decisions_target_key",
        table_name="application_operator_decisions",
    )
    op.drop_table("application_operator_decisions")
    op.drop_index("ix_telegram_operator_cards_application_id", table_name="telegram_operator_cards")
    op.drop_table("telegram_operator_cards")
