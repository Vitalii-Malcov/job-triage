"""Stage 16 (Codex P1): Stage 9D Gmail-draft creation honors the CURRENT
Stage 16 operator decision at its final locked revalidation boundary.

Codex sequence: a real Stage 9B package + Stage 9C approval, linked to a
Stage 16 application; Stage 16 APPROVE; audited APPROVE -> SKIP override.
`revalidate_approved_link_locked` must then refuse (NOT_APPROVED) and no
Gmail draft path may proceed -- the same shared rule
(`review_withdrawn_by_operator` / `effective_final_decision`) the
approved-package reader and send readiness use. Legacy Stage 9C approvals
(no Stage 16 decision), an effective APPROVE, SKIP -> APPROVE and LATER keep
working; a SKIP recorded BEFORE the Stage 9C approval still fails closed.

Fake provider only (no IMAP); the Stage 9C network guard is autouse.
"""

import asyncio

import pytest
from sqlalchemy import select

from app.db.models import (
    ApplicationPackageReviewRecord,
    GmailApplicationDraftRecord,
    TelegramOperatorCardRecord,
)
from app.models.application_lifecycle import (
    ApplicationChannel,
    ApplicationEventOrigin,
)
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.telegram_operator import OperatorAction, OperatorDecision
from app.services import telegram_operator_control as control
from app.services.application_tracker import ApplicationTracker
from app.services.telegram_bewerbung_approval import (
    ApprovedLinkSnapshot,
    revalidate_approved_link_locked,
)
from app.services.telegram_operator_render import build_callback_data
from tests import test_gmail_application_draft as s9d
from tests.stage16_fixtures import NOW, FakeTransport, callback, operator_settings

# Real 9B/9C/6E flow on SQLite + autouse network guard and Stage 9D clock.
network_guard = s9d.network_guard
clock = s9d.clock
requested = s9d.requested
approved = s9d.approved
seeded = s9d.seeded
sender = s9d.sender
session_factory = s9d.session_factory
provider = s9d.provider


def _link_application(session_factory, link) -> dict:
    """A Stage 16 application bound to the Stage 9C link's review package,
    with a delivered operator card (fake transport)."""
    settings = operator_settings()
    db = session_factory()
    try:
        review = db.get(ApplicationPackageReviewRecord, link.review_id)
        outcome = ApplicationTracker().create_application(
            db,
            company_name="Acme GmbH",
            role_title="Junior Python Developer",
            source="bundesagentur",
            source_reference="20385-0000099-S",
            application_channel=ApplicationChannel.EMPLOYER_PORTAL,
            initial_status=S.PACKAGE_READY,
            origin=ApplicationEventOrigin.SYSTEM,
            actor="stage16-9d-test",
            job_id=review.job_id,
            review_package_id=review.id,
        )
        application_id = outcome.application.id
        sent = asyncio.run(
            control.send_card(db, settings, application_id, FakeTransport(), now=NOW)
        )
        token = db.get(TelegramOperatorCardRecord, sent.card_id).callback_token
    finally:
        db.close()
    return {"application_id": application_id, "token": token, "settings": settings}


def _press(session_factory, operator, action, query_id):
    db = session_factory()
    try:
        return control.handle_callback(
            db,
            operator["settings"],
            callback(build_callback_data(action, operator["token"]), query_id=query_id),
            now=NOW,
        )
    finally:
        db.close()


def _override(session_factory, operator, decision):
    db = session_factory()
    try:
        return control.override_decision(
            db, operator["application_id"], decision, reason="operator changed mind", now=NOW
        )
    finally:
        db.close()


def _revalidate(session_factory, link_id):
    db = session_factory()
    try:
        return revalidate_approved_link_locked(db, link_id)
    finally:
        db.rollback()
        db.close()


def _drafts(session_factory):
    db = session_factory()
    try:
        return list(db.scalars(select(GmailApplicationDraftRecord)))
    finally:
        db.close()


def _assert_refused(session_factory, link, capability, provider):
    assert _revalidate(session_factory, link.id) == "NOT_APPROVED"
    outcome = asyncio.run(s9d._create(session_factory, capability, provider))
    assert outcome.code == "NOT_APPROVED", outcome
    assert provider.creates == []
    assert all(row.state != "CREATED" for row in _drafts(session_factory))


def _assert_usable(session_factory, link, capability, provider):
    assert isinstance(_revalidate(session_factory, link.id), ApprovedLinkSnapshot)
    outcome = asyncio.run(s9d._create(session_factory, capability, provider))
    assert outcome.code == "CREATED", outcome
    assert len(provider.creates) == 1  # the in-memory fake only


def _approve_then_skip(session_factory, operator):
    approve = _press(session_factory, operator, OperatorAction.APPROVE, "q-approve")
    assert approve.code is control.CallbackCode.APPROVED, approve
    skip = _override(session_factory, operator, OperatorDecision.SKIP)
    assert skip.code is control.OverrideCode.OVERRIDDEN, skip


def test_codex_sequence_approve_then_skip_override_refuses_stage9d(
    session_factory, approved, provider
):
    link = approved["link"]
    operator = _link_application(session_factory, link)
    _approve_then_skip(session_factory, operator)
    db = session_factory()
    try:
        assert db.get(ApplicationPackageReviewRecord, link.review_id).status == "APPROVED"
    finally:
        db.close()
    _assert_refused(session_factory, link, approved["capability"], provider)


def test_legacy_stage9c_approval_without_stage16_decision_still_creates(
    session_factory, approved, provider
):
    _assert_usable(session_factory, approved["link"], approved["capability"], provider)


def test_effective_stage16_approve_still_creates(session_factory, approved, provider):
    link = approved["link"]
    operator = _link_application(session_factory, link)
    outcome = _press(session_factory, operator, OperatorAction.APPROVE, "q-approve")
    assert outcome.code is control.CallbackCode.APPROVED, outcome
    _assert_usable(session_factory, link, approved["capability"], provider)


def test_skip_then_reapprove_override_makes_stage9d_usable_again(
    session_factory, approved, provider
):
    link = approved["link"]
    operator = _link_application(session_factory, link)
    _approve_then_skip(session_factory, operator)
    assert _revalidate(session_factory, link.id) == "NOT_APPROVED"
    again = _override(session_factory, operator, OperatorDecision.APPROVE)
    assert again.code is control.OverrideCode.OVERRIDDEN, again
    _assert_usable(session_factory, link, approved["capability"], provider)


@pytest.mark.asyncio
async def test_skip_recorded_before_stage9c_approval_still_fails_closed(
    session_factory, requested, provider
):
    link = requested["link"]
    operator = await asyncio.to_thread(_link_application, session_factory, link)
    skip = await asyncio.to_thread(_press, session_factory, operator, OperatorAction.SKIP, "q-skip")
    assert skip.code is control.CallbackCode.SKIPPED, skip
    decided = await s9d._decide(session_factory, requested["capability"], "f")
    assert decided.code == "APPROVED", decided  # Stage 9C itself is unchanged
    await asyncio.to_thread(
        _assert_refused, session_factory, link, requested["capability"], provider
    )


@pytest.mark.asyncio
async def test_later_is_not_skip(session_factory, requested, provider):
    link = requested["link"]
    operator = await asyncio.to_thread(_link_application, session_factory, link)
    later = await asyncio.to_thread(
        _press, session_factory, operator, OperatorAction.LATER, "q-later"
    )
    assert later.code is control.CallbackCode.LATER_RECORDED, later
    decided = await s9d._decide(session_factory, requested["capability"], "f")
    assert decided.code == "APPROVED", decided
    await asyncio.to_thread(
        _assert_usable, session_factory, link, requested["capability"], provider
    )
