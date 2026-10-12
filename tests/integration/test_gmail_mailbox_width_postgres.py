"""Codex mailbox-width finding, against REAL PostgreSQL (SQLite never
enforces VARCHAR length, so tests/test_gmail_mailbox_identity_width.py can
only prove the bound).

Proves, on one real server:
- at the pre-fix revision (f3b8d2a6c9e1) every canonical-mailbox column is
  VARCHAR(100) and REJECTS the 109-character identity of a mailbox the
  config accepts (40 x "Я");
- after d2f5a8c1b7e4 every such column is VARCHAR(600), its unique
  constraints/indexes are unchanged, and the real repository persists the
  40-character reproducer and the 536-character worst case -- with
  readable/encoded alias dedup (message + Message-ID claimant) and
  permanent-skip dedup still enforced by the database;
- downgrade refuses (and changes nothing) while a long identity is stored.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set. Like the
HARD-008 module, this module drives its own Alembic downgrade/upgrade and
always restores `head` in a `finally` block; CI runs it before HARD-008.
"""

import os
from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

import app.db.gmail_repository as repository
from app.db.gmail_repository import get_known_uids, record_permanent_skips, upsert_message
from app.db.models import (
    GMAIL_MAILBOX_IDENTITY_MAX_LENGTH,
    GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH,
    GmailMessageIdClaimRecord,
    GmailMessageRecord,
    GmailPermanentSkipRecord,
    GmailThreadRecord,
)
from app.providers.email.base import ParsedGmailMessage
from app.providers.email.imap_mailbox import canonical_mailbox_identity

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRE_FIX_REVISION = "f3b8d2a6c9e1"
ACCOUNT = "me@gmail-mailbox-width-postgres-integration.example.com"
IDENTITY_COLUMNS = (
    ("gmail_messages", "mailbox"),
    ("gmail_message_id_claims", "claimant_mailbox"),
    ("gmail_permanent_skips", "mailbox"),
)
# gmail_threads.lock_holder embeds the identity ("gmail_sync:<identity>:...").
COLUMNS = (*IDENTITY_COLUMNS, ("gmail_threads", "lock_holder"))
PRE_FIX_WIDTHS = {
    **{f"{t}.{c}": 100 for t, c in IDENTITY_COLUMNS},
    "gmail_threads.lock_holder": 200,
}
MIGRATED_WIDTHS = {
    **{f"{t}.{c}": GMAIL_MAILBOX_IDENTITY_MAX_LENGTH for t, c in IDENTITY_COLUMNS},
    "gmail_threads.lock_holder": GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH,
}
LONG_RU = "Я" * 40
WORST_CASE = "\U0001f600" * 100
SPELLINGS = [
    (LONG_RU, canonical_mailbox_identity(LONG_RU)),
    (canonical_mailbox_identity(LONG_RU), LONG_RU),
    (WORST_CASE, canonical_mailbox_identity(WORST_CASE)),
]


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_POSTGRES_URL)
    return cfg


@pytest.fixture()
def engine():
    engine = create_engine(TEST_POSTGRES_URL, future=True)

    def cleanup() -> None:
        with engine.begin() as conn:
            for model in (GmailMessageIdClaimRecord, GmailPermanentSkipRecord, GmailMessageRecord):
                conn.execute(delete(model).where(model.account_key == ACCOUNT))
            conn.execute(delete(GmailThreadRecord).where(GmailThreadRecord.account_key == ACCOUNT))

    cleanup()
    try:
        yield engine
    finally:
        try:
            cleanup()
        finally:
            upgrade(_alembic_config(), "head")
            engine.dispose()


def _widths(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            f"{table}.{column}": conn.execute(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar_one()
            for table, column in COLUMNS
        }


def _keys_and_indexes(engine) -> dict[str, list[str]]:
    with engine.connect() as conn:
        return {
            table: sorted(
                conn.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE tablename = :t"), {"t": table}
                ).scalars()
            )
            for table, _ in COLUMNS
        }


def _revision(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _insert_skip(engine, mailbox: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gmail_permanent_skips "
                "(account_key, mailbox, uid_validity, uid, reason, created_at) "
                "VALUES (:a, :m, 100, 1, 'OVERSIZED', now())"
            ),
            {"a": ACCOUNT, "m": mailbox},
        )


def _parsed(mailbox: str) -> ParsedGmailMessage:
    return ParsedGmailMessage(
        account_key=ACCOUNT,
        mailbox=mailbox,
        uid=7,
        uid_validity=100,
        message_id_header="<pg-long-mailbox@example.com>",
        in_reply_to=None,
        references=(),
        from_address="recruiter@example.com",
        from_display_name=None,
        to_addresses=("me@example.com",),
        cc_addresses=(),
        subject="Hello",
        sent_at=None,
        direction="INBOUND",
        body_plain="hello",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )


def test_pre_fix_schema_rejects_and_migrated_schema_accepts_the_reproducer(engine):
    cfg = _alembic_config()
    long_identity = canonical_mailbox_identity(LONG_RU)
    assert len(long_identity) == 109

    downgrade(cfg, PRE_FIX_REVISION)
    assert _widths(engine) == PRE_FIX_WIDTHS
    keys_before = _keys_and_indexes(engine)
    with pytest.raises(sqlalchemy.exc.DataError, match="character varying\\(100\\)"):
        _insert_skip(engine, long_identity)

    upgrade(cfg, "head")
    assert _widths(engine) == MIGRATED_WIDTHS
    assert _keys_and_indexes(engine) == keys_before
    _insert_skip(engine, long_identity)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _insert_skip(engine, long_identity)


@pytest.mark.parametrize(("spelling_a", "spelling_b"), SPELLINGS, ids=["rd-enc", "enc-rd", "worst"])
def test_long_alias_import_persists_one_message_one_claim_one_skip(
    monkeypatch, engine, spelling_a, spelling_b
):
    """Astra's interleaving on two real connections: A's duplicate lookup
    sees nothing, B (other spelling) inserts and commits, then A inserts --
    the UNIQUE key on the long canonical identity must resolve A to B's row."""
    canonical = canonical_mailbox_identity(spelling_a)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    original_resolve = repository._resolve_thread_for_message
    session_a, session_b = factory(), factory()
    results = {}

    def resolve_after_b_commits(db, parsed):
        if db is session_a and "b" not in results:
            results["b"] = upsert_message(session_b, _parsed(spelling_b))
        return original_resolve(db, parsed)

    monkeypatch.setattr(repository, "_resolve_thread_for_message", resolve_after_b_commits)
    try:
        record_a, created_a = upsert_message(session_a, _parsed(spelling_a))
        record_b, created_b = results["b"]
        assert (created_a, created_b) == (False, True)
        assert record_a.id == record_b.id

        record_permanent_skips(session_a, ACCOUNT, spelling_a, 100, [(9, "OVERSIZED")])
        record_permanent_skips(session_b, ACCOUNT, spelling_b, 100, [(9, "OVERSIZED")])
        assert get_known_uids(session_a, ACCOUNT, spelling_b, 100, [7, 9, 10]) == {7, 9}
    finally:
        session_a.close()
        session_b.close()

    with factory() as session:
        messages = session.scalars(
            select(GmailMessageRecord).where(GmailMessageRecord.account_key == ACCOUNT)
        ).all()
        claims = session.scalars(
            select(GmailMessageIdClaimRecord).where(
                GmailMessageIdClaimRecord.account_key == ACCOUNT
            )
        ).all()
        skips = session.scalars(
            select(GmailPermanentSkipRecord).where(GmailPermanentSkipRecord.account_key == ACCOUNT)
        ).all()
        assert [(m.mailbox, m.uid_validity, m.uid) for m in messages] == [(canonical, 100, 7)]
        assert [(c.claimant_mailbox, c.contested) for c in claims] == [(canonical, False)]
        assert [(s.mailbox, s.uid) for s in skips] == [(canonical, 9)]


def test_downgrade_refuses_while_a_long_identity_is_stored(engine):
    long_identity = canonical_mailbox_identity(WORST_CASE)
    assert len(long_identity) == 536
    _insert_skip(engine, long_identity)

    with pytest.raises(RuntimeError, match="Refusing to narrow"):
        downgrade(_alembic_config(), PRE_FIX_REVISION)

    # PostgreSQL runs the whole multi-step downgrade in one transaction, so
    # the refusal rolls back every step: the schema stays at head.
    assert _revision(engine) == ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert _widths(engine) == MIGRATED_WIDTHS
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT mailbox FROM gmail_permanent_skips WHERE account_key = :a"),
            {"a": ACCOUNT},
        ).scalar_one()
    assert stored == long_identity
