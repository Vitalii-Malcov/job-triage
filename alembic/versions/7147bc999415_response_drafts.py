"""response drafts

Stage 7C: an immutable, versioned table of deterministic response-draft
PROPOSALS derived from Stage 7B `gmail_message_analyses` rows — see
app/db/models.py's `ResponseDraftRecord` docstring for the full
rationale (idempotency identity, "never invents facts" contract, and
why `analysis_id`/`matched_job_id` are traceability columns rather than
ForeignKeys).

Revision ID: 7147bc999415
Revises: 805108385946
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7147bc999415"
down_revision: str | Sequence[str] | None = "805108385946"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() when collapsing account-scoped identity back
    to the pre-GMAIL-002 schema (8634f4be953a) would violate that
    schema's account_key-less UNIQUE constraints. Same check as
    847b7f5c87d8/813c9d5086d0/e6ccb9b4271b/805108385946's own
    `GmailAccountScopeDowngradeConflict` — duplicated here deliberately
    (not imported; see e6ccb9b4271b's own docstring for why cross-
    migration imports are avoided in this project), for the same
    "fail before this migration's own DDL runs" reason: without this
    migration's OWN preflight, a downgrade starting from THIS revision
    (current head) would run this migration's downgrade() (dropping
    response_drafts) and 805108385946's downgrade() (dropping
    job_reference_tokens) BEFORE 7058c097a542's own preflight finally
    detects the conflict — violating "database unchanged on unsafe
    downgrade".
    """


def _preflight_check_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    """Identical read-only compatibility check to this chain's earlier
    migrations' own `_preflight_check_downgrade_is_safe` — see this
    module's own docstring for why it is duplicated here rather than
    imported. Must run BEFORE any DDL in downgrade() below.
    """
    message_conflicts = connection.execute(
        sa.text(
            """
            SELECT mailbox, uid_validity, uid, COUNT(DISTINCT account_key) AS accounts
            FROM gmail_messages
            GROUP BY mailbox, uid_validity, uid
            HAVING COUNT(DISTINCT account_key) > 1
            """
        )
    ).fetchall()
    if message_conflicts:
        raise GmailAccountScopeDowngradeConflict(
            f"Cannot downgrade past 7058c097a542: {len(message_conflicts)} "
            "gmail_messages (mailbox, uid_validity, uid) identity/identities "
            "are shared by more than one account_key. The pre-account-scoping "
            "schema (8634f4be953a) cannot represent this without merging "
            "distinct accounts' correspondence into one row. Resolve manually "
            "(e.g. re-scope the affected accounts to distinct mailboxes, or "
            "remove the redundant rows) before downgrading — this migration "
            "will not do so implicitly."
        )

    thread_conflicts = connection.execute(
        sa.text(
            """
            SELECT thread_key, COUNT(DISTINCT account_key) AS accounts
            FROM gmail_threads
            GROUP BY thread_key
            HAVING COUNT(DISTINCT account_key) > 1
            """
        )
    ).fetchall()
    if thread_conflicts:
        raise GmailAccountScopeDowngradeConflict(
            f"Cannot downgrade past 7058c097a542: {len(thread_conflicts)} "
            "gmail_threads thread_key value(s) are shared by more than one "
            "account_key. The pre-account-scoping schema (8634f4be953a) "
            "cannot represent this without merging distinct accounts' "
            "correspondence threads into one row. Resolve manually before "
            "downgrading — this migration will not do so implicitly."
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "response_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("gmail_message_id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("analysis_version", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_version", sa.Integer(), nullable=False),
        sa.Column("matched_job_id", sa.Integer(), nullable=True),
        sa.Column("classification", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=5), nullable=True),
        sa.Column("missing_fields_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("generator_version", sa.String(length=20), nullable=False),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gmail_message_id"], ["gmail_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "gmail_message_id",
            "analysis_id",
            "candidate_profile_version",
            "generator_version",
            name="uq_response_drafts_identity",
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'NO_RESPONSE_RECOMMENDED')",
            name="ck_response_drafts_status_valid",
        ),
        sa.CheckConstraint(
            "language IS NULL OR language IN ('de', 'en')",
            name="ck_response_drafts_language_valid",
        ),
    )
    op.create_index(
        op.f("ix_response_drafts_account_key"), "response_drafts", ["account_key"], unique=False
    )
    op.create_index(
        op.f("ix_response_drafts_gmail_message_id"),
        "response_drafts",
        ["gmail_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_response_drafts_analysis_id"), "response_drafts", ["analysis_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema.

    GMAIL-013-style preflight: runs the same account-scope compatibility
    check as every migration back to 7058c097a542 — BEFORE this
    migration's own DDL — so a downgrade starting from current HEAD fails
    closed before anything (including this migration's own
    response_drafts drop) has mutated the database. See
    `_preflight_check_downgrade_is_safe` and
    `GmailAccountScopeDowngradeConflict` above.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())

    op.drop_index(op.f("ix_response_drafts_analysis_id"), table_name="response_drafts")
    op.drop_index(op.f("ix_response_drafts_gmail_message_id"), table_name="response_drafts")
    op.drop_index(op.f("ix_response_drafts_account_key"), table_name="response_drafts")
    op.drop_table("response_drafts")
