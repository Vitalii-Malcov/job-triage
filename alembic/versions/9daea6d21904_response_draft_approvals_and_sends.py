"""response draft approvals and sends

Stage 7D: two new tables backing the human approval + Gmail-reply-send
gate — see app/db/models.py's `ResponseDraftApprovalRecord` /
`ResponseDraftSendRecord` docstrings for the full concurrency/
idempotency rationale (`UNIQUE(response_draft_id)` as the atomic claim
arbiter on both tables, mirroring `GmailMessageIdClaimRecord`'s
established pattern, plus `response_draft_sends.status`'s CAS-guarded
PENDING/SENT/FAILED/UNCERTAIN state machine, mirroring
`ApplicationPackageReviewRecord`'s own precedent — `UNCERTAIN` is the
fail-closed terminal state for an SMTP send whose outcome could not be
proven either way; see `ResponseDraftSendRecord`'s docstring).

Revision ID: 9daea6d21904
Revises: 7147bc999415
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9daea6d21904"
down_revision: str | Sequence[str] | None = "7147bc999415"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() when collapsing account-scoped identity back
    to the pre-GMAIL-002 schema (8634f4be953a) would violate that
    schema's account_key-less UNIQUE constraints. Same check as every
    migration back to 7058c097a542's own `GmailAccountScopeDowngradeConflict`
    — duplicated here deliberately (not imported; see e6ccb9b4271b's own
    docstring for why cross-migration imports are avoided in this
    project), for the same "fail before this migration's own DDL runs"
    reason: without this migration's OWN preflight, a downgrade starting
    from THIS revision (current head) would run this migration's
    downgrade() (dropping response_draft_sends/response_draft_approvals)
    and every intermediate migration's own downgrade() BEFORE
    7058c097a542's own preflight finally detects the conflict —
    violating "database unchanged on unsafe downgrade".
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
        "response_draft_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("response_draft_id", sa.Integer(), nullable=False),
        sa.Column("gmail_message_id", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("pinned_subject", sa.String(length=500), nullable=False),
        sa.Column("pinned_body", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["response_draft_id"], ["response_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("response_draft_id", name="uq_response_draft_approvals_response_draft"),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_response_draft_approvals_decision_valid",
        ),
    )
    op.create_index(
        op.f("ix_response_draft_approvals_account_key"),
        "response_draft_approvals",
        ["account_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_response_draft_approvals_response_draft_id"),
        "response_draft_approvals",
        ["response_draft_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_response_draft_approvals_gmail_message_id"),
        "response_draft_approvals",
        ["gmail_message_id"],
        unique=False,
    )

    op.create_table(
        "response_draft_sends",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("response_draft_id", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.Integer(), nullable=False),
        sa.Column("gmail_message_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider_message_id", sa.String(length=998), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["response_draft_id"], ["response_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["response_draft_approvals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("response_draft_id", name="uq_response_draft_sends_response_draft"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_response_draft_sends_status_valid",
        ),
        sa.CheckConstraint(
            "attempt_count > 0", name="ck_response_draft_sends_attempt_count_positive"
        ),
    )
    op.create_index(
        op.f("ix_response_draft_sends_account_key"),
        "response_draft_sends",
        ["account_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_response_draft_sends_response_draft_id"),
        "response_draft_sends",
        ["response_draft_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_response_draft_sends_approval_id"),
        "response_draft_sends",
        ["approval_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_response_draft_sends_gmail_message_id"),
        "response_draft_sends",
        ["gmail_message_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    GMAIL-013-style preflight: runs the same account-scope compatibility
    check as every migration back to 7058c097a542 — BEFORE this
    migration's own DDL — so a downgrade starting from current HEAD fails
    closed before anything (including this migration's own table drops)
    has mutated the database.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())

    op.drop_index(
        op.f("ix_response_draft_sends_gmail_message_id"), table_name="response_draft_sends"
    )
    op.drop_index(op.f("ix_response_draft_sends_approval_id"), table_name="response_draft_sends")
    op.drop_index(
        op.f("ix_response_draft_sends_response_draft_id"), table_name="response_draft_sends"
    )
    op.drop_index(op.f("ix_response_draft_sends_account_key"), table_name="response_draft_sends")
    op.drop_table("response_draft_sends")

    op.drop_index(
        op.f("ix_response_draft_approvals_gmail_message_id"),
        table_name="response_draft_approvals",
    )
    op.drop_index(
        op.f("ix_response_draft_approvals_response_draft_id"),
        table_name="response_draft_approvals",
    )
    op.drop_index(
        op.f("ix_response_draft_approvals_account_key"), table_name="response_draft_approvals"
    )
    op.drop_table("response_draft_approvals")
