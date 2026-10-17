"""Stage 16: Telegram operator control -- authorization, the four buttons,
idempotency, lifecycle integration, rendering limits and the PII boundary.

Matrix letters (A-T) refer to the Stage 16 specification's test matrix;
lifecycle notifications (U-X) and the no-employer-outbound proof live in
test_stage16_operator_notifications.py.
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.db.base import Base
from app.db.models import (
    ApplicationEventRecord,
    ApplicationOperatorDecisionRecord,
    ApplicationRecord,
    OperatorDecisionImmutableError,
    TelegramOperatorCardRecord,
)
from app.db.session_engine import enable_sqlite_foreign_keys
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.telegram_operator import OperatorAction, OperatorDecision
from app.services import telegram_operator_control as control
from app.services.telegram import (
    TELEGRAM_MESSAGE_HARD_LIMIT,
    TelegramSendOutcome,
    TelegramSendResult,
)
from app.services.telegram_operator_control import CallbackCode, CardSendCode
from app.services.telegram_operator_render import (
    CALLBACK_DATA_LIMIT,
    CARD_SOFT_LIMIT,
    DETAILS_SOFT_LIMIT,
    MatchSummary,
    OperatorCardView,
    PackageSummary,
    build_callback_data,
    build_card_keyboard,
    parse_callback_data,
    render_card,
    render_details,
    scrub,
)
from app.services.telegram_operator_transport import (
    HttpTelegramOperatorTransport,
    OperatorMessageTooLongError,
    OperatorTransportNotConfiguredError,
)
from tests.stage16_fixtures import (
    CANDIDATE_FIRST_NAME,
    CANDIDATE_LAST_NAME,
    CV_SECRET_SUMMARY,
    LETTER_SECRET_OPENING,
    NOW,
    OPERATOR_CHAT,
    RESEARCH_EMAIL,
    RESEARCH_PHONE,
    STRANGER_CHAT,
    FakeTransport,
    build_target,
    callback,
    operator_settings,
)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage16.db'}", poolclass=NullPool)
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine)
    engine.dispose()


@pytest.fixture()
def db(factory):
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def settings():
    return operator_settings()


def _send(db, settings, application_id, transport=None):
    transport = transport or FakeTransport()
    result = asyncio.run(control.send_card(db, settings, application_id, transport, now=NOW))
    return result, transport


@pytest.fixture()
def carded(db, settings):
    """A PACKAGE_READY application whose card was delivered."""
    target = build_target(db)
    result, transport = _send(db, settings, target.application_id)
    assert result.code is CardSendCode.SENT
    card = db.get(TelegramOperatorCardRecord, result.card_id)
    return {"target": target, "token": card.callback_token, "card_id": card.id, "sent": transport}


def _data(action: OperatorAction, token: str) -> str:
    return build_callback_data(action, token)


def _press(db, settings, action, token, **kwargs):
    return control.handle_callback(db, settings, callback(_data(action, token), **kwargs), now=NOW)


def _counts(db, application_id):
    decisions = db.scalar(
        select(func.count())
        .select_from(ApplicationOperatorDecisionRecord)
        .where(ApplicationOperatorDecisionRecord.application_id == application_id)
    )
    events = db.scalar(
        select(func.count())
        .select_from(ApplicationEventRecord)
        .where(ApplicationEventRecord.application_id == application_id)
    )
    status = db.scalar(
        select(ApplicationRecord.current_status).where(ApplicationRecord.id == application_id)
    )
    return decisions, events, status


# --- authorized operator (A-D) ------------------------------------------------------


class TestAuthorizedOperator:
    def test_a_details_shows_information_and_changes_nothing(self, db, settings, carded):
        app_id = carded["target"].application_id
        before = _counts(db, app_id)
        outcome = _press(db, settings, OperatorAction.DETAILS, carded["token"])
        assert outcome.code is CallbackCode.DETAILS
        assert outcome.details_text and "Acme Vacuum GmbH" in outcome.details_text
        assert "overall 67%" in outcome.details_text
        assert "https://example.com/jobs/" in outcome.details_text
        assert not outcome.state_changed
        assert _counts(db, app_id) == before

    def test_b_approve_records_exactly_one_approval(self, db, settings, carded):
        app_id = carded["target"].application_id
        outcome = _press(db, settings, OperatorAction.APPROVE, carded["token"])
        assert outcome.code is CallbackCode.APPROVED
        decisions, events, status = _counts(db, app_id)
        assert (decisions, events, status) == (1, 2, S.APPROVED.value)
        decision = db.scalars(select(ApplicationOperatorDecisionRecord)).one()
        assert decision.decision == "APPROVE"
        assert decision.actor == "TELEGRAM_OPERATOR"
        assert decision.card_id == carded["card_id"]
        assert decision.review_package_id == carded["target"].review_id
        event = db.scalars(
            select(ApplicationEventRecord).where(ApplicationEventRecord.sequence == 2)
        ).one()
        assert (event.old_status, event.new_status) == ("PACKAGE_READY", "APPROVED")
        assert (event.origin, event.actor) == ("OPERATOR", "TELEGRAM_OPERATOR")
        assert event.correlation_key == decision.lifecycle_correlation_key
        # The card is re-rendered final: only DETAILS stays active.
        assert "✅ APPROVED" in outcome.card.text
        buttons = [b["text"] for row in outcome.card.reply_markup["inline_keyboard"] for b in row]
        assert buttons == ["📄 DETAILS"]

    def test_c_skip_records_exactly_one_skip_and_keeps_lifecycle(self, db, settings, carded):
        app_id = carded["target"].application_id
        outcome = _press(db, settings, OperatorAction.SKIP, carded["token"])
        assert outcome.code is CallbackCode.SKIPPED
        assert _counts(db, app_id) == (1, 1, S.PACKAGE_READY.value)
        assert db.scalars(select(ApplicationOperatorDecisionRecord)).one().decision == "SKIP"
        assert "⏭ SKIPPED" in outcome.card.text

    def test_d_later_is_non_final_and_keeps_buttons(self, db, settings, carded):
        app_id = carded["target"].application_id
        outcome = _press(db, settings, OperatorAction.LATER, carded["token"])
        assert outcome.code is CallbackCode.LATER_RECORDED
        assert _counts(db, app_id) == (1, 1, S.PACKAGE_READY.value)
        buttons = [b["text"] for row in outcome.card.reply_markup["inline_keyboard"] for b in row]
        assert "✅ APPROVE" in buttons and "⏭ SKIP" in buttons
        # LATER is non-final: APPROVE is still possible afterwards.
        approve = _press(db, settings, OperatorAction.APPROVE, carded["token"], query_id="cbq-2")
        assert approve.code is CallbackCode.APPROVED
        assert _counts(db, app_id) == (2, 2, S.APPROVED.value)
        state = control.decision_state(db, f"app:{app_id}:pkg:{carded['target'].review_id}")
        assert state.display is OperatorDecision.APPROVE

    def test_second_later_press_is_deduplicated(self, db, settings, carded):
        _press(db, settings, OperatorAction.LATER, carded["token"])
        again = _press(db, settings, OperatorAction.LATER, carded["token"], query_id="cbq-2")
        assert again.code is CallbackCode.ALREADY_LATER
        assert _counts(db, carded["target"].application_id)[0] == 1


# --- unauthorized (E-H) ----------------------------------------------------------------

_STRANGERS = [
    pytest.param({"chat_id": STRANGER_CHAT, "user_id": STRANGER_CHAT}, id="other-chat"),
    pytest.param(
        {"chat_id": OPERATOR_CHAT, "user_id": "999", "chat_type": "group"}, id="group-member"
    ),
    pytest.param({"chat_id": OPERATOR_CHAT, "user_id": None}, id="no-user"),
]


class TestUnauthorized:
    @pytest.mark.parametrize("who", _STRANGERS)
    def test_e_wrong_chat_cannot_see_details(self, db, settings, carded, who):
        outcome = _press(db, settings, OperatorAction.DETAILS, carded["token"], **who)
        assert outcome.code is CallbackCode.UNAUTHORIZED
        assert outcome.answer is None and outcome.details_text is None and outcome.card is None

    @pytest.mark.parametrize("action", [OperatorAction.APPROVE, OperatorAction.SKIP])
    @pytest.mark.parametrize("who", _STRANGERS)
    def test_f_g_h_wrong_chat_cannot_mutate(self, db, settings, carded, who, action):
        app_id = carded["target"].application_id
        before = _counts(db, app_id)
        outcome = _press(db, settings, action, carded["token"], **who)
        assert outcome.code is CallbackCode.UNAUTHORIZED
        assert outcome.answer is None
        assert _counts(db, app_id) == before

    def test_h_later_from_stranger_mutates_nothing(self, db, settings, carded):
        before = _counts(db, carded["target"].application_id)
        _press(db, settings, OperatorAction.LATER, carded["token"], chat_id=STRANGER_CHAT)
        assert _counts(db, carded["target"].application_id) == before

    def test_configured_operator_user_is_required_when_set(self, db, carded):
        settings = operator_settings(telegram_operator_user_id="31337")
        app_id = carded["target"].application_id
        wrong = _press(db, settings, OperatorAction.APPROVE, carded["token"])
        assert wrong.code is CallbackCode.UNAUTHORIZED
        assert _counts(db, app_id)[0] == 0
        right = _press(
            db,
            settings,
            OperatorAction.APPROVE,
            carded["token"],
            user_id="31337",
            chat_type="group",
            query_id="cbq-9",
        )
        assert right.code is CallbackCode.APPROVED

    def test_unconfigured_chat_rejects_everyone(self, db, carded):
        settings = operator_settings(telegram_chat_id="")
        outcome = _press(db, settings, OperatorAction.DETAILS, carded["token"])
        assert outcome.code is CallbackCode.UNAUTHORIZED

    def test_disabled_flag_refuses_without_change(self, db, carded):
        settings = operator_settings(telegram_operator_control_enabled=False)
        before = _counts(db, carded["target"].application_id)
        outcome = _press(db, settings, OperatorAction.APPROVE, carded["token"])
        assert outcome.code is CallbackCode.DISABLED
        assert _counts(db, carded["target"].application_id) == before


# --- callback validation ------------------------------------------------------------------


class TestCallbackValidation:
    @pytest.mark.parametrize(
        "data",
        [
            None,
            "",
            "s16",
            "s16:a",
            "s16:x:AAAAAAAAAAAAAAAA",
            "vf:a:AAAAAAAAAAAAAAAA",
            "s16:a:AAAAAAAAAAAAAAAA:extra",
            "s16:a:" + "A" * 80,
        ],
    )
    def test_malformed_data_is_refused(self, db, settings, carded, data):
        outcome = control.handle_callback(db, settings, callback(data), now=NOW)
        assert outcome.code is CallbackCode.MALFORMED
        assert _counts(db, carded["target"].application_id)[0] == 0

    @pytest.mark.parametrize("token", ["AAAAAAAAAAAAAAAA", "short", "../../etc/passwd!!"])
    def test_unknown_token_is_refused(self, db, settings, carded, token):
        outcome = control.handle_callback(db, settings, callback(f"s16:a:{token}"), now=NOW)
        assert outcome.code in (CallbackCode.UNKNOWN_CARD, CallbackCode.MALFORMED)
        assert _counts(db, carded["target"].application_id)[0] == 0

    def test_undelivered_card_cannot_be_pressed(self, db, settings):
        target = build_target(db)
        result, _ = _send(
            db, settings, target.application_id, FakeTransport([TelegramSendOutcome.FAILED] * 3)
        )
        assert result.code is CardSendCode.FAILED
        token = db.get(TelegramOperatorCardRecord, result.card_id).callback_token
        outcome = _press(db, settings, OperatorAction.APPROVE, token)
        assert outcome.code is CallbackCode.UNKNOWN_CARD
        assert _counts(db, target.application_id) == (0, 1, S.PACKAGE_READY.value)

    def test_stale_card_after_package_change_is_refused(self, db, settings, carded):
        app_id = carded["target"].application_id
        db.execute(
            update(ApplicationRecord)
            .where(ApplicationRecord.id == app_id)
            .values(review_package_id=carded["target"].review_id + 100)
        )
        db.commit()
        outcome = _press(db, settings, OperatorAction.APPROVE, carded["token"])
        assert outcome.code is CallbackCode.STALE_CARD
        assert _counts(db, app_id)[0] == 0


# --- idempotency (I-K) --------------------------------------------------------------------


class TestIdempotency:
    def test_i_same_callback_twice_is_one_decision(self, db, settings, carded):
        first = _press(db, settings, OperatorAction.SKIP, carded["token"], query_id="same")
        second = _press(db, settings, OperatorAction.SKIP, carded["token"], query_id="same")
        assert first.code is CallbackCode.SKIPPED
        assert second.code is CallbackCode.DUPLICATE_CALLBACK
        assert second.decision_id == first.decision_id
        assert _counts(db, carded["target"].application_id)[0] == 1

    def test_i_same_approve_callback_replayed_is_one_event(self, db, settings, carded):
        _press(db, settings, OperatorAction.APPROVE, carded["token"], query_id="same")
        again = _press(db, settings, OperatorAction.APPROVE, carded["token"], query_id="same")
        assert again.code is CallbackCode.DUPLICATE_CALLBACK
        assert _counts(db, carded["target"].application_id) == (1, 2, S.APPROVED.value)

    def test_j_double_approve_is_one_decision_and_one_event(self, db, settings, carded):
        first = _press(db, settings, OperatorAction.APPROVE, carded["token"], query_id="q1")
        second = _press(db, settings, OperatorAction.APPROVE, carded["token"], query_id="q2")
        assert first.code is CallbackCode.APPROVED
        assert second.code is CallbackCode.ALREADY_APPROVED
        assert second.card is None  # no repeated card edit / spam
        assert _counts(db, carded["target"].application_id) == (1, 2, S.APPROVED.value)

    def test_double_skip_is_one_decision(self, db, settings, carded):
        _press(db, settings, OperatorAction.SKIP, carded["token"], query_id="q1")
        second = _press(db, settings, OperatorAction.SKIP, carded["token"], query_id="q2")
        assert second.code is CallbackCode.ALREADY_SKIPPED
        assert _counts(db, carded["target"].application_id)[0] == 1

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            (OperatorAction.APPROVE, OperatorAction.SKIP),
            (OperatorAction.SKIP, OperatorAction.APPROVE),
            (OperatorAction.APPROVE, OperatorAction.LATER),
            (OperatorAction.SKIP, OperatorAction.LATER),
        ],
    )
    def test_k_m_conflicting_decision_is_refused(self, db, settings, carded, first, second):
        app_id = carded["target"].application_id
        _press(db, settings, first, carded["token"], query_id="q1")
        before = _counts(db, app_id)
        outcome = _press(db, settings, second, carded["token"], query_id="q2")
        assert outcome.code is CallbackCode.CONFLICT_REQUIRES_OVERRIDE
        assert outcome.alert
        assert _counts(db, app_id) == before

    def test_concurrent_final_decisions_are_serialized_by_the_database(
        self, db, factory, settings, carded
    ):
        """Two presses that both passed the read checks: the UNIQUE
        idempotency key lets exactly one commit."""
        target = carded["target"]
        key = f"app:{target.application_id}:pkg:{target.review_id}"
        other = factory()
        try:
            control.repo.add_decision(
                other,
                application_id=target.application_id,
                card_id=carded["card_id"],
                review_package_id=target.review_id,
                target_key=key,
                decision="SKIP",
                idempotency_key=f"{key}:final:0",
                actor="TELEGRAM_OPERATOR",
            )
            other.commit()
        finally:
            other.close()
        # This session's read happened "before" the competing commit:
        original = control.decision_state
        stale = control.DecisionState(generation=0, effective_final=None, pending_later=None)
        # Two reads before the write (card view + decision check) see the
        # pre-commit state; the re-read after the UNIQUE violation is real.
        calls = iter([stale, stale])

        def first_call_stale(session, k):
            return next(calls, None) or original(session, k)

        try:
            control.decision_state = first_call_stale
            outcome = _press(db, settings, OperatorAction.APPROVE, carded["token"], query_id="z")
        finally:
            control.decision_state = original
        assert outcome.code is CallbackCode.CONFLICT_REQUIRES_OVERRIDE
        assert _counts(db, target.application_id) == (1, 1, S.PACKAGE_READY.value)


# --- operator override (explicit policy for K/M) ------------------------------------------


class TestOverride:
    def test_override_skip_to_approve_is_explicit_and_audited(self, db, settings, carded):
        app_id = carded["target"].application_id
        _press(db, settings, OperatorAction.SKIP, carded["token"])
        plan = control.plan_override(db, app_id, OperatorDecision.APPROVE)
        assert plan.code is control.OverrideCode.OVERRIDDEN
        assert _counts(db, app_id)[0] == 1  # the plan writes nothing
        result = control.override_decision(
            db, app_id, OperatorDecision.APPROVE, reason="changed my mind", now=NOW
        )
        assert result.code is control.OverrideCode.OVERRIDDEN
        rows = db.scalars(
            select(ApplicationOperatorDecisionRecord).order_by(ApplicationOperatorDecisionRecord.id)
        ).all()
        assert [r.decision for r in rows] == ["SKIP", "APPROVE"]
        assert rows[1].supersedes_decision_id == rows[0].id
        assert rows[1].reason == "changed my mind"
        assert _counts(db, app_id) == (2, 2, S.APPROVED.value)

    def test_override_approve_to_skip_returns_to_package_ready(self, db, settings, carded):
        app_id = carded["target"].application_id
        _press(db, settings, OperatorAction.APPROVE, carded["token"])
        result = control.override_decision(
            db, app_id, OperatorDecision.SKIP, reason="employer closed the role", now=NOW
        )
        assert result.code is control.OverrideCode.OVERRIDDEN
        assert _counts(db, app_id) == (2, 3, S.PACKAGE_READY.value)
        last = db.scalars(
            select(ApplicationEventRecord).order_by(ApplicationEventRecord.id.desc())
        ).first()
        assert last.event_type == "STATUS_OVERRIDDEN" and last.origin == "OPERATOR"

    def test_override_requires_reason_and_a_final_decision(self, db, settings, carded):
        app_id = carded["target"].application_id
        with pytest.raises(control.ApplicationInputError):
            control.override_decision(db, app_id, OperatorDecision.SKIP, reason="  ")
        result = control.override_decision(db, app_id, OperatorDecision.SKIP, reason="x")
        assert result.code is control.OverrideCode.NO_FINAL_DECISION

    def test_override_never_touches_a_submitted_application(self, db, settings, carded):
        app_id = carded["target"].application_id
        _press(db, settings, OperatorAction.APPROVE, carded["token"])
        control.ApplicationTracker().transition_application(
            db,
            app_id,
            S.SUBMITTED,
            origin=control.ApplicationEventOrigin.OPERATOR,
            actor="test",
        )
        result = control.override_decision(db, app_id, OperatorDecision.SKIP, reason="late")
        assert result.code is control.OverrideCode.NOT_PERMITTED
        assert _counts(db, app_id)[2] == S.SUBMITTED.value

    def test_decisions_are_append_only(self, db, settings, carded):
        _press(db, settings, OperatorAction.SKIP, carded["token"])
        record = db.scalars(select(ApplicationOperatorDecisionRecord)).one()
        record.decision = "APPROVE"
        with pytest.raises(OperatorDecisionImmutableError):
            db.commit()
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("UPDATE application_operator_decisions SET decision = 'APPROVE'"))
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("DELETE FROM application_operator_decisions"))
        db.rollback()


# --- state (L-P) ------------------------------------------------------------------------


class TestState:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"with_package": False},
            {"status": S.MATCHED},
            {"manual_overrides": True},
        ],
        ids=["no-package", "not-package-ready", "unacknowledged-manual-edits"],
    )
    def test_l_package_not_ready_cannot_be_approved(self, db, settings, kwargs):
        target = build_target(db, **kwargs)
        result, transport = _send(db, settings, target.application_id)
        assert result.code is CardSendCode.SENT
        buttons = [b["text"] for row in transport.sent[0][1]["inline_keyboard"] for b in row]
        assert "✅ APPROVE" not in buttons
        assert "PACKAGE_NOT_READY" in transport.sent[0][0]
        token = db.get(TelegramOperatorCardRecord, result.card_id).callback_token
        before = _counts(db, target.application_id)
        outcome = _press(db, settings, OperatorAction.APPROVE, token)
        assert outcome.code is CallbackCode.PACKAGE_NOT_READY
        assert outcome.answer.startswith("PACKAGE_NOT_READY")
        assert _counts(db, target.application_id) == before

    def test_l_package_that_went_stale_after_carding_is_refused(self, db, settings, carded):
        from app.db.models import JobRecord

        job = db.get(JobRecord, carded["target"].job_id)
        job.description = job.description + " Changed requirements."
        db.commit()
        outcome = _press(db, settings, OperatorAction.APPROVE, carded["token"])
        assert outcome.code is CallbackCode.PACKAGE_NOT_READY
        assert "JOB_CHANGED" in outcome.answer

    def test_n_approve_does_not_produce_submitted(self, db, settings, carded):
        _press(db, settings, OperatorAction.APPROVE, carded["token"])
        record = db.get(ApplicationRecord, carded["target"].application_id)
        assert record.current_status == S.APPROVED.value
        assert record.submitted_at is None
        statuses = db.scalars(select(ApplicationEventRecord.new_status)).all()
        assert S.SUBMITTED.value not in statuses

    def test_o_approve_never_touches_an_employer_provider(self, db, settings, carded, monkeypatch):
        """Every outbound employer path fails loudly if reached."""
        import app.providers.email.smtp as smtp
        import app.services.follow_up_send as follow_up_send
        import app.services.gmail_application_draft as gmail_draft
        import app.services.response_draft_send as response_send

        def boom(*args, **kwargs):
            raise AssertionError("employer-facing path invoked")

        monkeypatch.setattr(smtp.GmailSmtpProvider, "send", boom)
        monkeypatch.setattr(gmail_draft, "handle_create", boom)
        monkeypatch.setattr(response_send, "send_response_draft", boom)
        monkeypatch.setattr(follow_up_send, "send_follow_up", boom)
        sent_before = len(carded["sent"].sent)
        outcome = _press(db, settings, OperatorAction.APPROVE, carded["token"])
        assert outcome.code is CallbackCode.APPROVED
        assert len(carded["sent"].sent) == sent_before  # not even a Telegram message

    def test_p_skip_does_not_become_rejected(self, db, settings, carded):
        _press(db, settings, OperatorAction.SKIP, carded["token"])
        record = db.get(ApplicationRecord, carded["target"].application_id)
        assert record.current_status == S.PACKAGE_READY.value
        statuses = set(db.scalars(select(ApplicationEventRecord.new_status)).all())
        assert not statuses & {S.REJECTED.value, S.WITHDRAWN.value}

    def test_decisions_refused_after_submission(self, db, settings, carded):
        app_id = carded["target"].application_id
        tracker = control.ApplicationTracker()
        tracker.transition_application(
            db, app_id, S.APPROVED, origin=control.ApplicationEventOrigin.OPERATOR, actor="t"
        )
        tracker.transition_application(
            db, app_id, S.SUBMITTED, origin=control.ApplicationEventOrigin.OPERATOR, actor="t"
        )
        for action in (OperatorAction.SKIP, OperatorAction.LATER, OperatorAction.APPROVE):
            outcome = _press(db, settings, action, carded["token"], query_id=f"q-{action}")
            assert outcome.code is CallbackCode.NOT_DECIDABLE
        assert _counts(db, app_id) == (0, 3, S.SUBMITTED.value)

    def test_concurrent_lifecycle_change_rolls_back_the_decision(
        self, db, settings, carded, monkeypatch
    ):
        tracker = control.ApplicationTracker()

        def lost_cas(*args, **kwargs):
            raise control.ApplicationConcurrencyError(1)

        monkeypatch.setattr(tracker, "transition_application", lost_cas)
        outcome = control.handle_callback(
            db,
            settings,
            callback(_data(OperatorAction.APPROVE, carded["token"])),
            tracker=tracker,
            now=NOW,
        )
        assert outcome.code is CallbackCode.STATE_CHANGED
        assert _counts(db, carded["target"].application_id) == (0, 1, S.PACKAGE_READY.value)


# --- card delivery ----------------------------------------------------------------------


class TestCardDelivery:
    def test_one_card_per_target(self, db, settings):
        target = build_target(db)
        first, transport = _send(db, settings, target.application_id)
        again, _ = _send(db, settings, target.application_id, transport)
        assert first.code is CardSendCode.SENT
        assert again.code is CardSendCode.ALREADY_SENT
        assert len(transport.sent) == 1

    def test_t_transport_failure_never_mutates_workflow_state(self, db, settings):
        target = build_target(db)
        before = _counts(db, target.application_id)
        for outcome in (TelegramSendOutcome.FAILED, TelegramSendOutcome.UNCERTAIN):
            ref = f"20385-77777{outcome.value[:2]}-S"
            other = build_target(db, reference=ref)
            result, _ = _send(db, settings, other.application_id, FakeTransport([outcome] * 3))
            assert result.code.value == outcome.value
            assert _counts(db, other.application_id) == (0, 1, S.PACKAGE_READY.value)
        assert _counts(db, target.application_id) == before

    def test_failed_card_is_retried_on_the_same_row_and_uncertain_is_not(self, db, settings):
        target = build_target(db)
        failed, _ = _send(
            db, settings, target.application_id, FakeTransport([TelegramSendOutcome.FAILED])
        )
        retry, transport = _send(db, settings, target.application_id)
        assert (failed.code, retry.code) == (CardSendCode.FAILED, CardSendCode.SENT)
        assert retry.card_id == failed.card_id
        card = db.get(TelegramOperatorCardRecord, retry.card_id)
        assert card.attempt_count == 2 and card.telegram_message_id is not None

        other = build_target(db, reference="20385-0000002-S")
        uncertain, _ = _send(
            db, settings, other.application_id, FakeTransport([TelegramSendOutcome.UNCERTAIN])
        )
        again, transport = _send(db, settings, other.application_id)
        assert uncertain.code is CardSendCode.UNCERTAIN
        assert again.code is CardSendCode.ALREADY_SENT and transport.sent == []

    def test_retries_are_bounded(self, db, settings):
        target = build_target(db)
        codes = [
            _send(db, settings, target.application_id, FakeTransport([TelegramSendOutcome.FAILED]))[
                0
            ].code
            for _ in range(control.repo.MAX_DELIVERY_ATTEMPTS + 1)
        ]
        assert codes[-1] is CardSendCode.RETRIES_EXHAUSTED
        assert codes.count(CardSendCode.FAILED) == control.repo.MAX_DELIVERY_ATTEMPTS

    def test_send_refused_when_disabled_or_unconfigured(self, db):
        target = build_target(db)
        disabled = operator_settings(telegram_operator_control_enabled=False)
        unconfigured = operator_settings(telegram_bot_token="")
        assert _send(db, disabled, target.application_id)[0].code is CardSendCode.DISABLED
        assert _send(db, unconfigured, target.application_id)[0].code is CardSendCode.NOT_CONFIGURED
        assert db.scalar(select(func.count()).select_from(TelegramOperatorCardRecord)) == 0

    def test_no_card_for_a_submitted_application(self, db, settings):
        target = build_target(db, status=S.MATCHED)
        tracker = control.ApplicationTracker()
        for status in (S.PACKAGE_READY, S.APPROVED, S.SUBMITTED):
            tracker.transition_application(
                db,
                target.application_id,
                status,
                origin=control.ApplicationEventOrigin.OPERATOR,
                actor="t",
            )
        assert _send(db, settings, target.application_id)[0].code is CardSendCode.NOT_DECIDABLE

    def test_preview_is_read_only(self, db, settings):
        target = build_target(db)
        preview = control.preview_card(db, target.application_id, now=NOW)
        assert preview.approve_offered and preview.existing_card_state is None
        assert db.scalar(select(func.count()).select_from(TelegramOperatorCardRecord)) == 0
        assert "Match: 67%" in preview.message.text

    def test_preview_never_reveals_a_real_callback_token(self, db, settings, carded):
        preview = control.preview_card(db, carded["target"].application_id, now=NOW)
        assert preview.existing_card_state == "SENT"
        datas = [
            b["callback_data"] for r in preview.message.reply_markup["inline_keyboard"] for b in r
        ]
        assert all(d.endswith(control.PREVIEW_TOKEN) for d in datas)
        assert not any(carded["token"] in d for d in datas)

    def test_transport_only_addresses_the_configured_chat(self):
        calls = []

        async def sender(token, chat_id, text_value, **kwargs):
            calls.append(chat_id)
            return TelegramSendResult(TelegramSendOutcome.SENT, 5)

        transport = HttpTelegramOperatorTransport(operator_settings(), sender=sender)
        asyncio.run(transport.send("hello", None))
        assert calls == [OPERATOR_CHAT]
        with pytest.raises(OperatorTransportNotConfiguredError):
            HttpTelegramOperatorTransport(operator_settings(telegram_chat_id=""))

    def test_transport_retries_only_provable_failures(self):
        outcomes = [
            TelegramSendOutcome.FAILED,
            TelegramSendOutcome.FAILED,
            TelegramSendOutcome.SENT,
        ]
        calls = []

        async def sender(*args, **kwargs):
            calls.append(1)
            return TelegramSendResult(outcomes.pop(0))

        async def no_sleep(_):
            return None

        transport = HttpTelegramOperatorTransport(
            operator_settings(), sender=sender, sleep=no_sleep
        )
        assert asyncio.run(transport.send("x")).outcome is TelegramSendOutcome.SENT
        assert len(calls) == 3

        uncertain_calls = []

        async def uncertain(*args, **kwargs):
            uncertain_calls.append(1)
            return TelegramSendResult(TelegramSendOutcome.UNCERTAIN)

        transport = HttpTelegramOperatorTransport(
            operator_settings(), sender=uncertain, sleep=no_sleep
        )
        assert asyncio.run(transport.send("x")).outcome is TelegramSendOutcome.UNCERTAIN
        assert len(uncertain_calls) == 1  # never retried: could duplicate

        with pytest.raises(OperatorMessageTooLongError):
            asyncio.run(transport.send("x" * (TELEGRAM_MESSAGE_HARD_LIMIT + 1)))


# --- rendering limits and PII (Q-S) -----------------------------------------------------


def _huge_view(**overrides) -> OperatorCardView:
    many = tuple(f"Requirement number {i} " + "x" * 60 for i in range(300))
    values = dict(
        application_id=1,
        company="C" * 900,
        role="R" * 900,
        location="L" * 900,
        source="bundesagentur",
        reference="20385-2347902-S",
        channel="EMPLOYER_PORTAL",
        status="PACKAGE_READY",
        package=PackageSummary(ready=False, reasons=tuple(f"R{i}" for i in range(200))),
        match=MatchSummary(
            overall=1,
            required=2,
            preferred=3,
            experience=4,
            matched=many,
            missing=many,
            partial=many,
            language_issues=many,
            language_requirements=many,
            experience_requirements=many,
            warnings=many,
        ),
        job_url="https://example.com/" + "p" * 5000,
        research_summary="S" * 10000,
        research_risks=many,
    )
    values.update(overrides)
    return OperatorCardView(**values)


class TestRendering:
    def test_q_callback_payload_under_limit(self):
        token = control.repo.new_callback_token()
        for action in OperatorAction:
            data = build_callback_data(action, token)
            assert len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT
            assert len(data) == 22
            assert parse_callback_data(data) == (action, token)
        with pytest.raises(ValueError):
            build_callback_data(OperatorAction.APPROVE, "x" * 70)

    def test_q_real_card_buttons_carry_only_opaque_tokens(self, db, settings, carded):
        markup = carded["sent"].sent[0][1]
        for row in markup["inline_keyboard"]:
            for button in row:
                data = button["callback_data"]
                assert data.endswith(carded["token"])
                assert str(carded["target"].application_id) not in data.split(":")[:2]
                assert len(data.encode()) <= CALLBACK_DATA_LIMIT

    def test_r_card_and_details_stay_below_limits(self):
        view = _huge_view()
        card = render_card(view, now=NOW)
        details = render_details(view, now=NOW)
        assert len(card) <= CARD_SOFT_LIMIT < TELEGRAM_MESSAGE_HARD_LIMIT
        assert len(details) <= DETAILS_SOFT_LIMIT < TELEGRAM_MESSAGE_HARD_LIMIT
        assert card.startswith("💼 ")

    def test_card_matches_the_specified_layout(self, db, settings, carded):
        text_value = carded["sent"].sent[0][0]
        for line in (
            "Acme Vacuum GmbH — Backend Developer (m/w/d)",
            "📍 Hanau",
            "Match: 67%",
            "Required: 67%",
            "Preferred: 100%",
            "Experience: 33%",
            "Matched:\nPython · PostgreSQL · Git · Docker",
            "Missing:\nCI/CD · German C1 · Kubernetes",
            "Channel: Employer portal",
            "Package: CV READY · ANSCHREIBEN READY",
            "Status: PACKAGE_READY",
            "Ref: 20385-0000001-S",
            "Telegram approval never sends an application.",
        ):
            assert line in text_value
        buttons = [[b["text"] for b in row] for row in carded["sent"].sent[0][1]["inline_keyboard"]]
        assert buttons == [["✅ APPROVE", "⏭ SKIP"], ["📄 DETAILS", "🕒 LATER"]]

    def test_s_card_and_details_omit_private_data(self, db, settings, carded):
        details = _press(db, settings, OperatorAction.DETAILS, carded["token"]).details_text
        for rendered in (carded["sent"].sent[0][0], details):
            for secret in (
                CANDIDATE_FIRST_NAME,
                CANDIDATE_LAST_NAME,
                CV_SECRET_SUMMARY,
                LETTER_SECRET_OPENING,
                RESEARCH_EMAIL,
                RESEARCH_PHONE,
                "hr@acme-employer.example",
                "TEST-TOKEN",
            ):
                assert secret not in rendered
        assert "Industrial vacuum furnaces" in details
        assert "[e-mail removed]" in details and "[phone removed]" in details

    def test_scrub_keeps_references_years_and_percentages(self):
        assert scrub("Ref 2006-2021, 67%, v12") == "Ref 2006-2021, 67%, v12"
        assert scrub("call +49 170 1234567 now") == "call [phone removed] now"
        assert scrub("mail a.b@c.de") == "mail [e-mail removed]"

    def test_keyboard_variants(self):
        final = build_card_keyboard("A" * 16, approve_offered=True, final=True)
        assert [[b["text"] for b in r] for r in final["inline_keyboard"]] == [["📄 DETAILS"]]
        not_ready = build_card_keyboard("A" * 16, approve_offered=False, final=False)
        texts = [b["text"] for r in not_ready["inline_keyboard"] for b in r]
        assert "✅ APPROVE" not in texts and "⏭ SKIP" in texts

    def test_decision_metadata_holds_no_raw_telegram_identity(self, db, settings, carded):
        _press(db, settings, OperatorAction.APPROVE, carded["token"], query_id="raw-query-id")
        record = db.scalars(select(ApplicationOperatorDecisionRecord)).one()
        stored = json.dumps(
            [record.operator_ref, record.chat_ref, record.callback_ref, record.metadata_json]
        )
        assert OPERATOR_CHAT not in stored and "raw-query-id" not in stored
        assert len(record.operator_ref) == len(record.chat_ref) == 64
