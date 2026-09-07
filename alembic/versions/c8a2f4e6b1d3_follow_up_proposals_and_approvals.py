"""follow up proposals, approvals, and sends

Stage 7E: three new tables backing the deterministic follow-up
eligibility/proposal engine plus its human approval + send gate — see
app/db/models.py's `FollowUpProposalRecord` / `FollowUpApprovalRecord` /
`FollowUpSendRecord` docstrings for the full rationale. Mirrors
7147bc999415 (response_drafts) + 9daea6d21904 (response_draft_approvals /
response_draft_sends) structurally: `UNIQUE(account_key,
anchor_gmail_message_id)` is the follow-up proposal's own idempotency
identity (one proposal per correspondence anchor, ever), and
`UNIQUE(follow_up_proposal_id)` is the atomic claim arbiter on both the
approval and send tables, mirroring `GmailMessageIdClaimRecord`'s
established pattern.

Revision ID: c8a2f4e6b1d3
Revises: 9daea6d21904
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8a2f4e6b1d3"
down_revision: str | Sequence[str] | None = "9daea6d21904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "follow_up_proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("gmail_thread_id", sa.Integer(), nullable=False),
        sa.Column("anchor_gmail_message_id", sa.Integer(), nullable=False),
        sa.Column("eligibility_reason", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False),
        sa.Column("missing_fields_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("generator_version", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PROPOSED"),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["anchor_gmail_message_id"], ["gmail_messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_key", "anchor_gmail_message_id", name="uq_follow_up_proposals_anchor"
        ),
        sa.CheckConstraint(
            "language IN ('de', 'en')", name="ck_follow_up_proposals_language_valid"
        ),
        sa.CheckConstraint("status IN ('PROPOSED')", name="ck_follow_up_proposals_status_valid"),
    )
    op.create_index(
        op.f("ix_follow_up_proposals_account_key"),
        "follow_up_proposals",
        ["account_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_follow_up_proposals_job_id"), "follow_up_proposals", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_follow_up_proposals_gmail_thread_id"),
        "follow_up_proposals",
        ["gmail_thread_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_follow_up_proposals_anchor_gmail_message_id"),
        "follow_up_proposals",
        ["anchor_gmail_message_id"],
        unique=False,
    )

    op.create_table(
        "follow_up_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("follow_up_proposal_id", sa.Integer(), nullable=False),
        sa.Column("gmail_message_id", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("pinned_subject", sa.String(length=500), nullable=False),
        sa.Column("pinned_body", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["follow_up_proposal_id"], ["follow_up_proposals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "follow_up_proposal_id", name="uq_follow_up_approvals_follow_up_proposal"
        ),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')", name="ck_follow_up_approvals_decision_valid"
        ),
    )
    op.create_index(
        op.f("ix_follow_up_approvals_account_key"),
        "follow_up_approvals",
        ["account_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_follow_up_approvals_follow_up_proposal_id"),
        "follow_up_approvals",
        ["follow_up_proposal_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_follow_up_approvals_gmail_message_id"),
        "follow_up_approvals",
        ["gmail_message_id"],
        unique=False,
    )

    op.create_table(
        "follow_up_sends",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("follow_up_proposal_id", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.Integer(), nullable=False),
        sa.Column("gmail_message_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider_message_id", sa.String(length=998), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["follow_up_proposal_id"], ["follow_up_proposals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["follow_up_approvals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("follow_up_proposal_id", name="uq_follow_up_sends_follow_up_proposal"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_follow_up_sends_status_valid",
        ),
        sa.CheckConstraint("attempt_count > 0", name="ck_follow_up_sends_attempt_count_positive"),
    )
    op.create_index(
        op.f("ix_follow_up_sends_account_key"), "follow_up_sends", ["account_key"], unique=False
    )
    op.create_index(
        op.f("ix_follow_up_sends_follow_up_proposal_id"),
        "follow_up_sends",
        ["follow_up_proposal_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_follow_up_sends_approval_id"), "follow_up_sends", ["approval_id"], unique=False
    )
    op.create_index(
        op.f("ix_follow_up_sends_gmail_message_id"),
        "follow_up_sends",
        ["gmail_message_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_follow_up_sends_gmail_message_id"), table_name="follow_up_sends")
    op.drop_index(op.f("ix_follow_up_sends_approval_id"), table_name="follow_up_sends")
    op.drop_index(op.f("ix_follow_up_sends_follow_up_proposal_id"), table_name="follow_up_sends")
    op.drop_index(op.f("ix_follow_up_sends_account_key"), table_name="follow_up_sends")
    op.drop_table("follow_up_sends")

    op.drop_index(op.f("ix_follow_up_approvals_gmail_message_id"), table_name="follow_up_approvals")
    op.drop_index(
        op.f("ix_follow_up_approvals_follow_up_proposal_id"), table_name="follow_up_approvals"
    )
    op.drop_index(op.f("ix_follow_up_approvals_account_key"), table_name="follow_up_approvals")
    op.drop_table("follow_up_approvals")

    op.drop_index(
        op.f("ix_follow_up_proposals_anchor_gmail_message_id"), table_name="follow_up_proposals"
    )
    op.drop_index(op.f("ix_follow_up_proposals_gmail_thread_id"), table_name="follow_up_proposals")
    op.drop_index(op.f("ix_follow_up_proposals_job_id"), table_name="follow_up_proposals")
    op.drop_index(op.f("ix_follow_up_proposals_account_key"), table_name="follow_up_proposals")
    op.drop_table("follow_up_proposals")
