"""Gmail IMAP 32-bit identifiers, against REAL PostgreSQL (SQLite stores
every INTEGER as 64-bit, so tests/test_gmail_imap_32bit_identifiers.py
cannot show the overflow).

Proves, on one real server, for each of the six UID/UIDVALIDITY columns:
- at the pre-fix revision (d2f5a8c1b7e4) the column is INTEGER, accepts
  2147483647 and REJECTS the legal values 2147483648 and 4294967295;
- after e8b1c4f7a2d9 it is BIGINT, constraints and indexes are unchanged,
  and it accepts all three;
- the real repository keeps identity dedup, permanent skips and
  Message-ID claims at uid_validity = uid = 4294967295;
- downgrade refuses (and changes nothing) while any value exceeds INTEGER.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set. Drives its
own Alembic downgrade/upgrade and always restores `head` in a `finally`
block; CI runs it after the at-head modules and before HARD-008.
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

from app.db.gmail_repository import get_known_uids, record_permanent_skips, upsert_message
from app.db.models import (
    GmailMessageIdClaimRecord,
    GmailMessageRecord,
    GmailPermanentSkipRecord,
    GmailThreadRecord,
)
from app.providers.email.base import ParsedGmailMessage

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRE_FIX_REVISION = "d2f5a8c1b7e4"
ACCOUNT = "me@gmail-imap-32bit-postgres-integration.example.com"
OTHER_ACCOUNT = "other@gmail-imap-32bit-postgres-integration.example.com"
ACCOUNTS = (ACCOUNT, OTHER_ACCOUNT)
# table -> (UIDVALIDITY column, UID column)
TABLES = {
    "gmail_messages": ("uid_validity", "uid"),
    "gmail_message_id_claims": ("claimant_uid_validity", "claimant_uid"),
    "gmail_permanent_skips": ("uid_validity", "uid"),
}
COLUMNS = tuple((t, c) for t, cols in TABLES.items() for c in cols)
INTEGER_MAX = 2147483647
HIGH = INTEGER_MAX + 1
MAX = 4294967295


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_POSTGRES_URL)
    return cfg


def _head() -> str:
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


@pytest.fixture()
def engine():
    engine = create_engine(TEST_POSTGRES_URL, future=True)

    def cleanup() -> None:
        with engine.begin() as conn:
            for model in (GmailMessageIdClaimRecord, GmailPermanentSkipRecord, GmailMessageRecord):
                conn.execute(delete(model).where(model.account_key.in_(ACCOUNTS)))
            conn.execute(
                delete(GmailThreadRecord).where(GmailThreadRecord.account_key.in_(ACCOUNTS))
            )

    cleanup()
    try:
        yield engine
    finally:
        try:
            cleanup()
        finally:
            upgrade(_alembic_config(), "head")
            engine.dispose()


def _types(engine) -> dict[str, str]:
    with engine.connect() as conn:
        return {
            f"{table}.{column}": conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar_one()
            for table, column in COLUMNS
        }


def _schema_objects(engine) -> dict[str, list[str]]:
    with engine.connect() as conn:
        return {
            table: sorted(
                [
                    *conn.execute(
                        text("SELECT indexdef FROM pg_indexes WHERE tablename = :t"), {"t": table}
                    ).scalars(),
                    *conn.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid = CAST(:t AS regclass)"
                        ),
                        {"t": table},
                    ).scalars(),
                ]
            )
            for table in TABLES
        }


def _revision(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _insert(engine, table: str, uid_validity: int, uid: int) -> None:
    """One raw row in `table` (plus the gmail_threads row it needs), in its
    own transaction."""
    params = {"a": ACCOUNT, "v": uid_validity, "u": uid}
    with engine.begin() as conn:
        if table == "gmail_permanent_skips":
            conn.execute(
                text(
                    "INSERT INTO gmail_permanent_skips (account_key, mailbox, uid_validity, uid, "
                    "reason, created_at) VALUES (:a, 'INBOX', :v, :u, 'OVERSIZED', now())"
                ),
                params,
            )
            return
        params["t"] = conn.execute(
            text(
                "INSERT INTO gmail_threads (account_key, thread_key, subject, created_at, "
                "updated_at) VALUES (:a, :k, '', now(), now()) RETURNING id"
            ),
            {"a": ACCOUNT, "k": f"pg-imap-32bit:{table}:{uid_validity}:{uid}"},
        ).scalar_one()
        if table == "gmail_message_id_claims":
            conn.execute(
                text(
                    "INSERT INTO gmail_message_id_claims (account_key, message_id_header, "
                    "claimant_mailbox, claimant_uid_validity, claimant_uid, thread_id, "
                    "contested, created_at) "
                    "VALUES (:a, :m, 'INBOX', :v, :u, :t, false, now())"
                ),
                {**params, "m": f"<raw-{uid_validity}-{uid}@example.com>"},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO gmail_messages (thread_id, account_key, mailbox, uid_validity, "
                    "uid, references_json, to_addresses_json, cc_addresses_json, subject, "
                    "received_at, direction, body_plain, body_truncated, has_html, "
                    "attachments_json, created_at) "
                    "VALUES (:t, :a, 'INBOX', :v, :u, '[]', '[]', '[]', '', now(), 'INBOUND', "
                    "'', false, false, '[]', now())"
                ),
                params,
            )


def _insert_into_column(engine, table: str, column: str, value: int, other: int) -> None:
    """`value` in exactly `column`; the table's other identifier is `other`."""
    validity_column, _ = TABLES[table]
    if column == validity_column:
        _insert(engine, table, value, other)
    else:
        _insert(engine, table, other, value)


def _parsed(*, account_key=ACCOUNT, uid_validity=MAX, uid=MAX, message_id=None):
    return ParsedGmailMessage(
        account_key=account_key,
        mailbox="INBOX",
        uid=uid,
        uid_validity=uid_validity,
        message_id_header=message_id,
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


@pytest.mark.parametrize(("table", "column"), COLUMNS, ids=[f"{t}.{c}" for t, c in COLUMNS])
def test_pre_fix_integer_overflows_and_migrated_bigint_accepts(engine, table, column):
    cfg = _alembic_config()
    downgrade(cfg, PRE_FIX_REVISION)
    assert set(_types(engine).values()) == {"integer"}
    objects_before = _schema_objects(engine)

    _insert_into_column(engine, table, column, INTEGER_MAX, 1)  # the boundary still fits
    for other, value in enumerate((HIGH, MAX), start=2):
        with pytest.raises(sqlalchemy.exc.DataError, match="integer out of range"):
            _insert_into_column(engine, table, column, value, other)

    upgrade(cfg, "head")
    assert set(_types(engine).values()) == {"bigint"}
    assert _schema_objects(engine) == objects_before
    for other, value in enumerate((HIGH, MAX), start=2):
        _insert_into_column(engine, table, column, value, other)
    with engine.connect() as conn:
        stored = (
            conn.execute(
                text(f"SELECT {column} FROM {table} WHERE account_key = :a ORDER BY {column}"),
                {"a": ACCOUNT},
            )
            .scalars()
            .all()
        )
    assert stored == [INTEGER_MAX, HIGH, MAX]


def test_repository_identity_at_max_uid_and_uidvalidity(engine):
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        first, created = upsert_message(db, _parsed(message_id="<pg-max@example.com>"))
        assert created is True
        again, created_again = upsert_message(db, _parsed(message_id="<pg-max@example.com>"))
        assert (created_again, again.id) == (False, first.id)

        others = [
            upsert_message(db, _parsed(uid=MAX - 1)),
            upsert_message(db, _parsed(uid_validity=MAX - 1)),
            upsert_message(db, _parsed(account_key=OTHER_ACCOUNT)),
        ]
        assert all(created for _, created in others)
        assert len({first.id, *(record.id for record, _ in others)}) == 4

        record_permanent_skips(db, ACCOUNT, "INBOX", MAX, [(MAX - 2, "OVERSIZED")])
        record_permanent_skips(db, ACCOUNT, "INBOX", MAX, [(MAX - 2, "OVERSIZED")])
        assert get_known_uids(db, ACCOUNT, "INBOX", MAX, [MAX, MAX - 1, MAX - 2, 1]) == {
            MAX,
            MAX - 1,
            MAX - 2,
        }
        # The adjacent generation sees only its own message, never this skip.
        assert get_known_uids(db, ACCOUNT, "INBOX", MAX - 1, [MAX, MAX - 2]) == {MAX}

        claim = db.scalars(
            select(GmailMessageIdClaimRecord).where(
                GmailMessageIdClaimRecord.account_key == ACCOUNT
            )
        ).one()
        assert (claim.claimant_uid_validity, claim.claimant_uid) == (MAX, MAX)
        assert claim.contested is False
        assert claim.thread_id == first.thread_id

        skips = db.scalars(
            select(GmailPermanentSkipRecord).where(GmailPermanentSkipRecord.account_key == ACCOUNT)
        ).all()
        assert [(s.uid_validity, s.uid) for s in skips] == [(MAX, MAX - 2)]


def test_downgrade_refuses_while_any_value_exceeds_integer(engine):
    for table, (validity_column, uid_column) in TABLES.items():
        _insert_into_column(engine, table, validity_column, MAX, 1)
        _insert_into_column(engine, table, uid_column, MAX, 2)

    with pytest.raises(RuntimeError, match="Refusing to narrow Gmail IMAP") as excinfo:
        downgrade(_alembic_config(), PRE_FIX_REVISION)
    message = str(excinfo.value)
    assert str(MAX) not in message
    for table, column in COLUMNS:
        assert f"'{table}.{column}': 1" in message

    assert _revision(engine) == _head()
    assert set(_types(engine).values()) == {"bigint"}
