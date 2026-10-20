"""Stage 16 (Codex P1) on real PostgreSQL: Stage 16 SKIP vs. an armed Stage 9D
Gmail-draft attempt, both orders FORCED and proven by `pg_blocking_pids`
(never by timing):

- ARM FIRST: Stage 9D holds its arm transaction (review row lock + fenced
  ledger row, uncommitted); the APPROVE -> SKIP override provably waits on
  it, then -- with the provider call in flight -- sees the armed attempt
  and refuses. The draft is CREATED; the SKIP never became effective.
- SKIP FIRST: the SKIP transaction holds the review row lock; the Stage 9D
  press provably waits on it, then sees the effective SKIP and refuses.
  The provider is never called.

Fake provider only (no IMAP, no network). Skipped unless TEST_POSTGRES_URL.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.db import gmail_application_draft_repository as ledger_repo
from app.db import telegram_operator_repository as op_repo
from app.db.models import ApplicationOperatorDecisionRecord, TelegramBewerbungApprovalRecord
from app.models.telegram_operator import OperatorAction, OperatorDecision
from app.services import telegram_operator_control as control
from tests.integration import test_gmail_application_draft_postgres as s9d_pg
from tests.integration.test_gmail_application_draft_postgres import (
    JOIN_TIMEOUT,
    Provider,
    _approve,
    _approved,
    _create,
    _Hold,
    _join,
    _overlap,
    _rows,
    _start,
)
from tests.test_stage16_stage9d_operator_skip import _link_application, _override, _press

pytestmark = s9d_pg.pytestmark
pg = s9d_pg.pg
env = s9d_pg.env

_TRUNCATE_STAGE16 = text(
    "TRUNCATE application_operator_decisions, telegram_lifecycle_notifications, "
    "telegram_operator_cards, application_events, applications RESTART IDENTITY CASCADE"
)


@pytest.fixture()
def operator_env(env):
    factory = env[0]
    with factory() as db:  # append-only decision/event rows: privileged TRUNCATE
        db.execute(_TRUNCATE_STAGE16)
        db.commit()
    yield env
    with factory() as db:
        db.execute(_TRUNCATE_STAGE16)
        db.commit()


class _GatedProvider(Provider):
    """Runs `before_create` inside the provider call (the in-flight moment)."""

    before_create = None

    def create_draft(self, message, target, deadline_at):
        if self.before_create is not None:
            self.before_create()
        return super().create_draft(message, target, deadline_at)


def _approved_operator(operator_env):
    factory = operator_env[0]
    package = _approved(operator_env)
    _approve(operator_env, package)
    with factory() as db:
        review_id = db.get(TelegramBewerbungApprovalRecord, package["link_id"]).review_id
    operator = _link_application(factory, SimpleNamespace(review_id=review_id))
    outcome = _press(factory, operator, OperatorAction.APPROVE, "q-approve")
    assert outcome.code is control.CallbackCode.APPROVED, outcome
    return package, operator, review_id


def _effective(factory, operator, review_id):
    with factory() as db:
        key = op_repo.target_key(operator["application_id"], review_id)
        final = control.decision_state(db, key).effective_final
        return final.decision if final is not None else None


def _decision_count(factory):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(ApplicationOperatorDecisionRecord))


def test_arm_first_skip_waits_for_the_arm_then_refuses_while_in_flight(operator_env, monkeypatch):
    factory = operator_env[0]
    package, operator, review_id = _approved_operator(operator_env)
    provider = _GatedProvider()
    hold = _Hold(monkeypatch, ledger_repo, "begin_append", "A")  # arm txn held open
    skipper = {}

    def finish_skip_inside_provider_call():
        thread, _ = skipper["b"]
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive()

    provider.before_create = finish_skip_inside_provider_call

    first = _start("A", _create, factory, package["cap"], provider)
    hold.wait_held()
    skipper["b"] = _start("B", _override, factory, operator, OperatorDecision.SKIP)
    _overlap(factory, hold, skipper["b"])  # B provably waits on A's arm transaction
    a = _join(*first)
    b = _join(*skipper["b"])

    assert a["result"].code == "CREATED", a
    assert b["result"].code is control.OverrideCode.STATE_CHANGED, b
    assert b["result"].detail == control.GMAIL_DRAFT_IN_FLIGHT
    assert provider.creates == 1
    (row,) = _rows(factory, package["link_id"])
    assert row.state == "CREATED"
    assert _effective(factory, operator, review_id) == "APPROVE"
    assert _decision_count(factory) == 1


def test_skip_first_arm_waits_for_the_skip_then_refuses(operator_env, monkeypatch):
    factory = operator_env[0]
    package, operator, review_id = _approved_operator(operator_env)
    provider = _GatedProvider()
    hold = _Hold(monkeypatch, op_repo, "add_decision", "B")  # SKIP txn: review lock held

    second = _start("B", _override, factory, operator, OperatorDecision.SKIP)
    hold.wait_held()
    first = _start("A", _create, factory, package["cap"], provider)
    _overlap(factory, hold, first)  # Stage 9D provably waits on the SKIP transaction
    b = _join(*second)
    a = _join(*first)

    assert b["result"].code is control.OverrideCode.OVERRIDDEN, b
    assert a["result"].code == "NOT_APPROVED", a
    assert provider.creates == 0
    assert all(row.state != "CREATED" for row in _rows(factory, package["link_id"]))
    assert _effective(factory, operator, review_id) == "SKIP"
