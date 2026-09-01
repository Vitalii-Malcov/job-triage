"""gmail message analyses

Stage 7B (Job/Application <-> Email matching + classification): adds
`gmail_message_analyses`, the immutable, versioned record of one
deterministic evidence-based matching + classification run over an
already-persisted `GmailMessageRecord`. See app/db/models.py's
`GmailMessageAnalysisRecord` docstring for the full rationale
(immutability, idempotency identity, why `matched_job_id` is not a
ForeignKey) and app/services/gmail_message_analysis.py for the
orchestration this table backs.

This table's OWN schema/data is purely additive relative to its parent
(e6ccb9b4271b) — nothing about `gmail_message_analyses` itself needs a
downgrade preflight; losing analysis rows on a downgrade is acceptable
(they are cheaply re-derivable by calling POST
/gmail/messages/{id}/analyze again after a later re-upgrade, since the
underlying `gmail_messages` content those analyses were computed from is
untouched by any of this).

**Downgrade still runs e6ccb9b4271b/7058c097a542's account-scope
preflight first anyway (duplicated here, same GMAIL-013 rationale).**
Without it, `downgrade(..., "8634f4be953a")` starting from THIS
migration's head would run: (1) THIS migration's downgrade drops
`gmail_message_analyses` and commits, (2) e6ccb9b4271b's downgrade drops
`gmail_message_id_claims` and commits, (3) alembic_version advances to
7058c097a542, (4) THAT migration's own preflight finally detects an
account-scope conflict and raises. Steps 1-3 would already have mutated
the database by the time the failure surfaces, violating this project's
established "nothing changes on an unsafe downgrade, all the way from
whatever the current head happens to be" invariant (see
e6ccb9b4271b/7058c097a542's own docstrings for the same reasoning one
migration earlier in the chain).

Revision ID: 813c9d5086d0
Revises: e6ccb9b4271b
Create Date: 2026-09-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "813c9d5086d0"
down_revision: str | Sequence[str] | None = "e6ccb9b4271b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() when collapsing account-scoped identity back
    to the pre-GMAIL-002 schema (8634f4be953a) would violate that
    schema's account_key-less UNIQUE constraints. Same check as
    e6ccb9b4271b's own `GmailAccountScopeDowngradeConflict` — duplicated
    here deliberately (not imported), for the same two reasons that
    class's docstring gives: hex-leading revision ids aren't importable
    Python module names, and Alembic version scripts are conventionally
    self-contained. See this module's own docstring for why THIS
    migration needs the check too, one link further down the chain.
    """


def _preflight_check_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    """Identical read-only compatibility check to e6ccb9b4271b's own
    `_preflight_check_downgrade_is_safe` — see that function's docstring
    and this module's own docstring for why it is duplicated here rather
    than imported. Must run BEFORE any DDL in downgrade() below.
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
        "gmail_message_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("gmail_message_id", sa.Integer(), nullable=False),
        sa.Column("analysis_version", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False),
        sa.Column("matched_job_id", sa.Integer(), nullable=True),
        sa.Column("match_confidence", sa.String(length=10), nullable=False),
        sa.Column("match_score", sa.Integer(), nullable=False),
        sa.Column("match_evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("candidate_matches_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("classification", sa.String(length=40), nullable=False),
        sa.Column("classification_confidence", sa.String(length=10), nullable=False),
        sa.Column("classification_evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("is_automated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gmail_message_id"], ["gmail_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "gmail_message_id",
            "analysis_version",
            "input_fingerprint",
            name="uq_gmail_message_analyses_identity",
        ),
        sa.CheckConstraint(
            "analysis_version > 0", name="ck_gmail_message_analyses_version_positive"
        ),
        sa.CheckConstraint(
            "match_type IN ('APPLICATION', 'JOB_ONLY', 'AMBIGUOUS', 'UNMATCHED')",
            name="ck_gmail_message_analyses_match_type_valid",
        ),
        sa.CheckConstraint(
            "match_confidence IN ('HIGH', 'MEDIUM', 'LOW')",
            name="ck_gmail_message_analyses_match_confidence_valid",
        ),
        sa.CheckConstraint(
            "classification_confidence IN ('HIGH', 'MEDIUM', 'LOW')",
            name="ck_gmail_message_analyses_classification_confidence_valid",
        ),
        sa.CheckConstraint(
            "classification IN ("
            "'APPLICATION_RECEIVED', 'REQUEST_FOR_INFORMATION', 'INTERVIEW_INVITATION', "
            "'INTERVIEW_RESCHEDULE', 'REJECTION', 'OFFER', "
            "'WITHDRAWAL_OR_POSITION_CLOSED', 'GENERAL_RECRUITER_MESSAGE', "
            "'AUTOMATED_NOTIFICATION', 'OTHER', 'UNKNOWN')",
            name="ck_gmail_message_analyses_classification_valid",
        ),
        sa.CheckConstraint("match_score >= 0", name="ck_gmail_message_analyses_match_score_valid"),
    )
    op.create_index(
        op.f("ix_gmail_message_analyses_account_key"),
        "gmail_message_analyses",
        ["account_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_gmail_message_analyses_gmail_message_id"),
        "gmail_message_analyses",
        ["gmail_message_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Runs the same account-scope compatibility preflight as
    e6ccb9b4271b/7058c097a542's own downgrade() — BEFORE this migration's
    own DDL — so a downgrade starting from current HEAD fails closed
    before anything (including this migration's own analyses-table drop)
    has mutated the database. See `_preflight_check_downgrade_is_safe`
    and `GmailAccountScopeDowngradeConflict` above, and this module's own
    docstring, for why this check is duplicated here rather than shared.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())

    op.drop_index(
        op.f("ix_gmail_message_analyses_gmail_message_id"), table_name="gmail_message_analyses"
    )
    op.drop_index(
        op.f("ix_gmail_message_analyses_account_key"), table_name="gmail_message_analyses"
    )
    op.drop_table("gmail_message_analyses")
