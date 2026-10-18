"""Stage 16 (Codex P1): a Stage 16 SKIP is serialized with an ARMED Stage 9D
Gmail-draft attempt through the existing durable ledger state.

The in-flight state is the Stage 9D ledger row `CREATING` with the network
fence set (`append_started_at`, written by `begin_append` and committed
before the provider is called). While it exists for the package's review,
SKIP (Telegram callback and the APPROVE -> SKIP override) refuses and
records nothing; once the attempt resolved (CREATED / FAILED / UNCERTAIN)
SKIP proceeds under the existing policy. A SKIP that commits before the arm
makes Stage 9D refuse without calling the provider -- including a SKIP that
lands between Stage 9D's revalidation read and its fence write (SQLite).

Every interleaving is forced deterministically: inside the fake provider's
`create_draft` (the in-flight moment) or inside the arm-phase revalidation.
No sleeps, no real Gmail; the Stage 9C network guard is autouse.
"""

import asyncio

from sqlalchemy import func, select

import app.services.gmail_application_draft as gd
from app.db import telegram_operator_repository as op_repo
from app.db.models import ApplicationOperatorDecisionRecord
from app.models.telegram_operator import OperatorAction, OperatorDecision
from app.providers.email.draft_base import (
    DraftCreateOutcomeUnknownError,
    DraftCreateRejectedError,
)
from app.services import telegram_operator_control as control
from tests import test_gmail_application_draft as s9d
from tests.test_stage16_stage9d_operator_skip import (
    _drafts,
    _link_application,
    _override,
    _press,
)

network_guard = s9d.network_guard
clock = s9d.clock
requested = s9d.requested
approved = s9d.approved
seeded = s9d.seeded
sender = s9d.sender
session_factory = s9d.session_factory
provider = s9d.provider


def _create(session_factory, capability, provider):
    return asyncio.run(s9d._create(session_factory, capability, provider))


def _effective(session_factory, operator, review_id):
    db = session_factory()
    try:
        key = op_repo.target_key(operator["application_id"], review_id)
        final = control.decision_state(db, key).effective_final
        return final.decision if final is not None else None
    finally:
        db.close()


def _decision_count(session_factory):
    db = session_factory()
    try:
        return db.scalar(select(func.count()).select_from(ApplicationOperatorDecisionRecord))
    finally:
        db.close()


def _ledger_state(session_factory):
    (row,) = _drafts(session_factory)
    return row.state


def _approved_operator(session_factory, link):
    operator = _link_application(session_factory, link)
    outcome = _press(session_factory, operator, OperatorAction.APPROVE, "q-approve")
    assert outcome.code is control.CallbackCode.APPROVED, outcome
    return operator


def test_arm_first_override_skip_is_refused_while_the_provider_call_is_in_flight(
    session_factory, approved, provider
):
    """Codex sequence: revalidation OK, arm committed, provider in flight,
    APPROVE -> SKIP attempted. Before the fix SKIP was OVERRIDDEN and the
    draft was still CREATED afterwards."""
    link = approved["link"]
    operator = _approved_operator(session_factory, link)
    during = []
    provider.on_create = lambda: during.append(
        _override(session_factory, operator, OperatorDecision.SKIP)
    )

    outcome = _create(session_factory, approved["capability"], provider)

    (skip,) = during
    assert skip.code is control.OverrideCode.STATE_CHANGED, skip
    assert skip.detail == control.GMAIL_DRAFT_IN_FLIGHT
    assert outcome.code == "CREATED"
    assert len(provider.creates) == 1
    assert _ledger_state(session_factory) == "CREATED"
    # The SKIP never became effective while the attempt was unresolved.
    assert _effective(session_factory, operator, link.review_id) == "APPROVE"
    assert _decision_count(session_factory) == 1

    # Resolved -> SKIP proceeds under the existing policy; nothing re-appends.
    after = _override(session_factory, operator, OperatorDecision.SKIP)
    assert after.code is control.OverrideCode.OVERRIDDEN, after
    assert _effective(session_factory, operator, link.review_id) == "SKIP"
    again = _create(session_factory, approved["capability"], provider)
    assert again.code == "ALREADY_CREATED", again
    assert len(provider.creates) == 1


def test_arm_first_direct_telegram_skip_is_refused_while_in_flight(
    session_factory, approved, provider
):
    link = approved["link"]
    operator = _link_application(session_factory, link)  # no Stage 16 decision yet
    during = []
    provider.on_create = lambda: during.append(
        _press(session_factory, operator, OperatorAction.SKIP, "q-skip-1")
    )

    outcome = _create(session_factory, approved["capability"], provider)

    (skip,) = during
    assert skip.code is control.CallbackCode.STATE_CHANGED, skip
    assert skip.answer == control.GMAIL_DRAFT_IN_FLIGHT_ANSWER
    assert outcome.code == "CREATED"
    assert _decision_count(session_factory) == 0
    after = _press(session_factory, operator, OperatorAction.SKIP, "q-skip-2")
    assert after.code is control.CallbackCode.SKIPPED, after
    assert len(provider.creates) == 1


def test_skip_first_then_stage9d_refuses_without_calling_the_provider(
    session_factory, approved, provider
):
    link = approved["link"]
    operator = _approved_operator(session_factory, link)
    skip = _override(session_factory, operator, OperatorDecision.SKIP)
    assert skip.code is control.OverrideCode.OVERRIDDEN, skip
    outcome = _create(session_factory, approved["capability"], provider)
    assert outcome.code == "NOT_APPROVED", outcome
    assert provider.creates == []
    assert all(row.state != "CREATED" for row in _drafts(session_factory))


def test_skip_committed_between_revalidation_and_arm_is_seen_by_the_arm(
    session_factory, approved, provider, monkeypatch
):
    """SQLite: Stage 9D's revalidation reads hold no lock, so a SKIP can
    commit before the fence write. The post-write re-check refuses the arm;
    the provider is never called."""
    link = approved["link"]
    operator = _approved_operator(session_factory, link)
    real = gd.revalidate_approved_link_locked
    calls = []

    def revalidate_then_skip(db, link_id):
        result = real(db, link_id)
        calls.append(result)
        if len(calls) == 2:  # the arm phase (1 = claim phase)
            skip = _override(session_factory, operator, OperatorDecision.SKIP)
            assert skip.code is control.OverrideCode.OVERRIDDEN, skip
        return result

    monkeypatch.setattr(gd, "revalidate_approved_link_locked", revalidate_then_skip)
    outcome = _create(session_factory, approved["capability"], provider)

    assert len(calls) == 2 and not isinstance(calls[1], str)  # revalidation itself passed
    assert outcome.code == "NOT_APPROVED", outcome
    assert provider.creates == []
    assert _ledger_state(session_factory) == "FAILED"  # released unarmed claim
    assert _effective(session_factory, operator, link.review_id) == "SKIP"


def test_provider_definite_failure_resolves_in_flight_state_and_unblocks_skip(
    session_factory, approved, provider
):
    link = approved["link"]
    operator = _approved_operator(session_factory, link)
    provider.create_exc = DraftCreateRejectedError("x")
    during = []
    provider.on_create = lambda: during.append(
        _override(session_factory, operator, OperatorDecision.SKIP)
    )

    outcome = _create(session_factory, approved["capability"], provider)

    assert outcome.code == "FAILED"
    assert during[0].code is control.OverrideCode.STATE_CHANGED
    assert _ledger_state(session_factory) == "FAILED"
    provider.on_create = None
    after = _override(session_factory, operator, OperatorDecision.SKIP)
    assert after.code is control.OverrideCode.OVERRIDDEN, after  # no longer blocked
    provider.create_exc = None
    retry = _create(session_factory, approved["capability"], provider)
    assert retry.code == "NOT_APPROVED", retry  # the explicit retry honors SKIP
    assert len(provider.creates) == 1  # no duplicate provider call


def test_uncertain_outcome_is_preserved_and_never_retried_after_skip(
    session_factory, approved, provider
):
    link = approved["link"]
    operator = _approved_operator(session_factory, link)
    provider.create_exc = DraftCreateOutcomeUnknownError("x")
    during = []
    provider.on_create = lambda: during.append(
        _override(session_factory, operator, OperatorDecision.SKIP)
    )

    outcome = _create(session_factory, approved["capability"], provider)

    assert outcome.code == "UNCERTAIN"
    assert during[0].code is control.OverrideCode.STATE_CHANGED
    provider.on_create = None
    provider.create_exc = None
    # UNCERTAIN is a resolved state (it never re-appends), so SKIP proceeds...
    after = _override(session_factory, operator, OperatorDecision.SKIP)
    assert after.code is control.OverrideCode.OVERRIDDEN, after
    # ...and the uncertainty is neither erased nor retried.
    again = _create(session_factory, approved["capability"], provider)
    assert again.code != "CREATED", again
    assert _ledger_state(session_factory) == "UNCERTAIN"
    assert len(provider.creates) == 1
