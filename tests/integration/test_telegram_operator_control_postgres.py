"""Stage 16: real-PostgreSQL proof of the Telegram operator control tables
(migration e3a9c5d7f2b4):

- two operators' presses racing on one target: the UNIQUE idempotency key
  lets exactly one final decision commit, and the loser changes nothing
  (MVCC is where a check-then-act would race; SQLite serializes it);
- an APPROVE commits its decision and the PACKAGE_READY -> APPROVED event
  atomically, and a replayed callback stays one decision / one event;
- `application_operator_decisions` is append-only in the database (Core
  UPDATE/DELETE refused by the trigger);
- the notification ledger admits one row per lifecycle event.

No Telegram, IMAP or SMTP anywhere: transports are in-memory fakes.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first (no create_all).
**CI:** part of the `scheduler-postgres` job's zero-skip gate, at head.
"""

import asyncio
import os
import threading

import pytest
import sqlalchemy.exc
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.db import telegram_operator_repository as repo
from app.db.models import (
    ApplicationEventRecord,
    ApplicationOperatorDecisionRecord,
    ApplicationRecord,
    TelegramOperatorCardRecord,
)
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.telegram_operator import OperatorAction
from app.services import telegram_operator_control as control
from app.services.application_tracker import ApplicationTracker
from app.services.telegram_operator_render import build_callback_data
from tests.stage16_fixtures import NOW, FakeTransport, build_target, callback, operator_settings

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

_TRUNCATE = text(
    "TRUNCATE application_operator_decisions, telegram_lifecycle_notifications, "
    "telegram_operator_cards, application_events, applications, "
    "application_package_review_revisions, application_package_reviews, "
    "candidate_job_matches, company_research RESTART IDENTITY CASCADE"
)


@pytest.fixture()
def factory():
    engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    with engine.begin() as conn:
        # Decision/event DELETEs are refused by append-only triggers; test
        # isolation uses the privileged TRUNCATE instead.
        conn.execute(_TRUNCATE)
    yield sessionmaker(engine)
    with engine.begin() as conn:
        conn.execute(_TRUNCATE)
    engine.dispose()


@pytest.fixture()
def carded(factory):
    settings = operator_settings()
    session = factory()
    try:
        target = build_target(session)
        result = asyncio.run(
            control.send_card(session, settings, target.application_id, FakeTransport(), now=NOW)
        )
        token = session.get(TelegramOperatorCardRecord, result.card_id).callback_token
        return {"target": target, "token": token, "card_id": result.card_id, "settings": settings}
    finally:
        session.close()


def _counts(session, application_id):
    return (
        session.scalar(select(func.count()).select_from(ApplicationOperatorDecisionRecord)),
        session.scalar(
            select(func.count())
            .select_from(ApplicationEventRecord)
            .where(ApplicationEventRecord.application_id == application_id)
        ),
        session.scalar(
            select(ApplicationRecord.current_status).where(ApplicationRecord.id == application_id)
        ),
    )


def test_racing_final_decisions_commit_exactly_one(factory, carded):
    """Session A holds an uncommitted SKIP on the target's final key; B's
    APPROVE (which could not see A's row when it read) blocks on the unique
    index, then loses when A commits -- B changes nothing."""
    target = carded["target"]
    key = repo.target_key(target.application_id, target.review_id)
    flushed = threading.Event()
    results = {}

    def operator_a():
        session = factory()
        try:
            repo.add_decision(
                session,
                application_id=target.application_id,
                card_id=carded["card_id"],
                review_package_id=target.review_id,
                target_key=key,
                decision="SKIP",
                idempotency_key=f"{key}:final:0",
                actor="TELEGRAM_OPERATOR",
            )
            flushed.set()
            threading.Event().wait(1.0)  # B is now blocked on the unique index
            session.commit()
        finally:
            session.close()

    def operator_b():
        flushed.wait(5)
        session = factory()
        try:
            results["b"] = control.handle_callback(
                session,
                carded["settings"],
                callback(
                    build_callback_data(OperatorAction.APPROVE, carded["token"]), query_id="b"
                ),
                now=NOW,
            )
        finally:
            session.close()

    threads = [threading.Thread(target=operator_a), threading.Thread(target=operator_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert results["b"].code is control.CallbackCode.CONFLICT_REQUIRES_OVERRIDE
    session = factory()
    try:
        assert _counts(session, target.application_id) == (1, 1, S.PACKAGE_READY.value)
    finally:
        session.close()


def test_approve_is_atomic_and_replay_is_idempotent(factory, carded):
    target = carded["target"]
    data = build_callback_data(OperatorAction.APPROVE, carded["token"])
    session = factory()
    try:
        first = control.handle_callback(
            session, carded["settings"], callback(data, query_id="same"), now=NOW
        )
        replay = control.handle_callback(
            session, carded["settings"], callback(data, query_id="same"), now=NOW
        )
        second = control.handle_callback(
            session, carded["settings"], callback(data, query_id="other"), now=NOW
        )
        assert first.code is control.CallbackCode.APPROVED
        assert replay.code is control.CallbackCode.DUPLICATE_CALLBACK
        assert second.code is control.CallbackCode.ALREADY_APPROVED
        assert _counts(session, target.application_id) == (1, 2, S.APPROVED.value)
    finally:
        session.close()


def test_failed_lifecycle_write_rolls_back_the_decision(factory, carded):
    target = carded["target"]
    session = factory()
    try:
        # Someone else moved the application on between card and press.
        ApplicationTracker().transition_application(
            session, target.application_id, S.WITHDRAWN, origin=O.OPERATOR, actor="t", reason="x"
        )
        outcome = control.handle_callback(
            session,
            carded["settings"],
            callback(build_callback_data(OperatorAction.APPROVE, carded["token"])),
            now=NOW,
        )
        assert outcome.code is control.CallbackCode.NOT_DECIDABLE
        assert _counts(session, target.application_id) == (0, 2, S.WITHDRAWN.value)
    finally:
        session.close()


def test_decisions_are_append_only_in_postgres(factory, carded):
    session = factory()
    try:
        control.handle_callback(
            session,
            carded["settings"],
            callback(build_callback_data(OperatorAction.SKIP, carded["token"])),
            now=NOW,
        )
        for statement in (
            "UPDATE application_operator_decisions SET decision = 'APPROVE'",
            "DELETE FROM application_operator_decisions",
        ):
            with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
                session.execute(text(statement))
            session.rollback()
        assert session.scalar(select(ApplicationOperatorDecisionRecord.decision)) == "SKIP"
    finally:
        session.close()


def test_one_notification_ledger_row_per_event(factory, carded):
    target = carded["target"]
    session = factory()
    try:
        event_id = session.scalar(
            select(ApplicationEventRecord.id).where(
                ApplicationEventRecord.application_id == target.application_id
            )
        )
        repo.insert_sending_notification(
            session,
            application_event_id=event_id,
            application_id=target.application_id,
            text_sha256="0" * 64,
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            repo.insert_sending_notification(
                session,
                application_event_id=event_id,
                application_id=target.application_id,
                text_sha256="1" * 64,
            )
        session.rollback()
    finally:
        session.close()


def test_check_constraints_are_enforced(factory, carded):
    session = factory()
    try:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.execute(
                text("UPDATE telegram_operator_cards SET delivery_state = 'BOGUS' WHERE id = :id"),
                {"id": carded["card_id"]},
            )
        session.rollback()
    finally:
        session.close()
