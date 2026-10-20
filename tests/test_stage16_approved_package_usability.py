"""Stage 16 (Codex P1): the operator's CURRENT decision controls whether an
approved Stage 6E package is usable downstream.

Telegram APPROVE moves the review to Stage 6E APPROVED (history). An audited
APPROVE -> SKIP override returns the application to PACKAGE_READY and must
also withdraw the package from every approved-package consumer
(`get_approved_package` / GET /jobs/{id}/approved-package / send readiness)
-- without rewriting the review's APPROVED status or any decision row. A
later SKIP -> APPROVE override makes it usable again; a review with no
Stage 16 decision (legacy/direct Stage 6E approval) is unaffected; LATER is
not SKIP. No network: every socket connect fails the test.
"""

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db import telegram_operator_repository as repo
from app.db.base import Base
from app.db.models import (
    ApplicationEventRecord,
    ApplicationOperatorDecisionRecord,
    ApplicationPackageReviewRecord,
    ApplicationRecord,
    JobRecord,
    TelegramOperatorCardRecord,
)
from app.db.review_package_repository import decide_review, get_latest_revision
from app.db.session import get_db
from app.db.session_engine import enable_sqlite_foreign_keys
from app.main import app
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.telegram_operator import OperatorAction, OperatorDecision
from app.security import rate_limit as rate_limit_module
from app.services import telegram_operator_control as control
from app.services.review_package import get_approved_package
from app.services.send_readiness import (
    NO_APPROVED_REVIEW,
    evaluate_send_readiness,
    load_send_readiness_inputs,
)
from app.services.telegram_operator_render import build_callback_data
from tests.stage16_fixtures import NOW, FakeTransport, build_target, callback, operator_settings

API_KEY = "test-api-key"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any non-loopback connect fails the test (asyncio's Windows event
    loop needs a loopback socketpair, which stays allowed)."""
    real_connect = socket.socket.connect
    attempts = []

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(host)
            raise AssertionError("no employer/network outbound is allowed in this test")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield
    assert attempts == []


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'usability.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
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
def api(factory, monkeypatch):
    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    fake = Settings(_env_file=None, api_key=API_KEY, rate_limit_requests=1000)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake)
    rate_limit_module._requests.clear()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


@pytest.fixture()
def carded(db):
    settings = operator_settings()
    target = build_target(db)
    result = asyncio.run(
        control.send_card(db, settings, target.application_id, FakeTransport(), now=NOW)
    )
    token = db.get(TelegramOperatorCardRecord, result.card_id).callback_token
    return {"target": target, "token": token, "settings": settings}


def _press(db, carded, action):
    return control.handle_callback(
        db,
        carded["settings"],
        callback(build_callback_data(action, carded["token"])),
        now=NOW,
    )


def _override(db, carded, decision):
    return control.override_decision(
        db, carded["target"].application_id, decision, reason="operator changed mind", now=NOW
    )


def _job(db, carded):
    db.expire_all()
    review = db.get(ApplicationPackageReviewRecord, carded["target"].review_id)
    return db.get(JobRecord, review.job_id)


def _service_usable(db, carded) -> bool:
    package = get_approved_package(db, _job(db, carded))
    return package is not None and package.id == carded["target"].review_id


def _api_status(api, db, carded) -> int:
    job_id = _job(db, carded).id
    return api.get(
        f"/api/v1/jobs/{job_id}/approved-package", headers={"X-API-Key": API_KEY}
    ).status_code


def _readiness(db, carded):
    job = _job(db, carded)
    inputs = load_send_readiness_inputs(
        db,
        job,
        settings=Settings(_env_file=None),
        destination=None,
        location_evidence=[],
    )
    db.rollback()
    return inputs, evaluate_send_readiness(inputs)


def _state(db, carded):
    db.expire_all()
    target = carded["target"]
    key = repo.target_key(target.application_id, target.review_id)
    final = control.decision_state(db, key).effective_final
    return (
        db.get(ApplicationRecord, target.application_id).current_status,
        db.get(ApplicationPackageReviewRecord, target.review_id).status,
        final.decision if final is not None else None,
    )


def _approve_then_skip(db, carded):
    assert _press(db, carded, OperatorAction.APPROVE).code is control.CallbackCode.APPROVED
    assert _state(db, carded) == (S.APPROVED.value, "APPROVED", "APPROVE")
    assert _override(db, carded, OperatorDecision.SKIP).code is control.OverrideCode.OVERRIDDEN
    assert _state(db, carded) == (S.PACKAGE_READY.value, "APPROVED", "SKIP")


class TestApproveThenSkipOverride:
    def test_approved_package_is_usable_after_approve(self, db, api, carded):
        _press(db, carded, OperatorAction.APPROVE)
        assert _service_usable(db, carded)
        assert _api_status(api, db, carded) == 200
        inputs, _ = _readiness(db, carded)
        assert inputs.approved_package is not None
        assert NO_APPROVED_REVIEW not in inputs.package_problems

    def test_a_service_does_not_return_skipped_package(self, db, carded):
        _approve_then_skip(db, carded)
        assert get_approved_package(db, _job(db, carded)) is None

    def test_b_api_reports_no_approved_package(self, db, api, carded):
        _approve_then_skip(db, carded)
        assert _api_status(api, db, carded) == 404

    def test_c_send_readiness_fails_closed(self, db, carded):
        _approve_then_skip(db, carded)
        inputs, result = _readiness(db, carded)
        assert inputs.approved_package is None
        assert inputs.package_problems == (NO_APPROVED_REVIEW,)
        assert result.ready is False
        assert result.email_send_ready is False

    def test_audit_history_is_preserved(self, db, carded):
        target = carded["target"]
        revision = get_latest_revision(db, target.review_id)
        _approve_then_skip(db, carded)
        review = db.get(ApplicationPackageReviewRecord, target.review_id)
        assert review.status == "APPROVED"
        assert review.approved_revision_id == revision.id
        decisions = db.scalars(
            select(ApplicationOperatorDecisionRecord.decision).order_by(
                ApplicationOperatorDecisionRecord.id
            )
        ).all()
        assert decisions == ["APPROVE", "SKIP"]
        statuses = db.scalars(
            select(ApplicationEventRecord.new_status)
            .where(ApplicationEventRecord.application_id == target.application_id)
            .order_by(ApplicationEventRecord.id)
        ).all()
        assert statuses == [S.PACKAGE_READY.value, S.APPROVED.value, S.PACKAGE_READY.value]

    def test_skip_reapprove_makes_package_usable_again(self, db, api, carded):
        _approve_then_skip(db, carded)
        assert _override(db, carded, OperatorDecision.APPROVE).code is (
            control.OverrideCode.OVERRIDDEN
        )
        assert _state(db, carded) == (S.APPROVED.value, "APPROVED", "APPROVE")
        assert _service_usable(db, carded)
        assert _api_status(api, db, carded) == 200
        inputs, _ = _readiness(db, carded)
        assert inputs.approved_package is not None
        assert NO_APPROVED_REVIEW not in inputs.package_problems


class TestOtherDecisionHistories:
    def test_legacy_stage6e_approval_without_stage16_decision_is_unchanged(self, db, api, carded):
        target = carded["target"]
        review = db.get(ApplicationPackageReviewRecord, target.review_id)
        decide_review(
            db,
            review_id=review.id,
            expected_review_version=review.review_version,
            new_status="APPROVED",
            decision_note="direct Stage 6E approval",
            approved_revision_id=get_latest_revision(db, review.id).id,
        )
        assert db.scalar(select(func.count()).select_from(ApplicationOperatorDecisionRecord)) == 0
        assert _service_usable(db, carded)
        assert _api_status(api, db, carded) == 200
        inputs, _ = _readiness(db, carded)
        assert inputs.approved_package is not None
        assert NO_APPROVED_REVIEW not in inputs.package_problems

    def test_direct_skip_without_prior_approve_also_withholds_a_stage6e_approval(self, db, carded):
        target = carded["target"]
        assert _press(db, carded, OperatorAction.SKIP).code is control.CallbackCode.SKIPPED
        review = db.get(ApplicationPackageReviewRecord, target.review_id)
        decide_review(
            db,
            review_id=review.id,
            expected_review_version=review.review_version,
            new_status="APPROVED",
            decision_note="approved via API after the operator skipped",
            approved_revision_id=get_latest_revision(db, review.id).id,
        )
        assert get_approved_package(db, _job(db, carded)) is None

    def test_later_is_not_skip(self, db, carded):
        target = carded["target"]
        assert _press(db, carded, OperatorAction.LATER).code is (
            control.CallbackCode.LATER_RECORDED
        )
        review = db.get(ApplicationPackageReviewRecord, target.review_id)
        decide_review(
            db,
            review_id=review.id,
            expected_review_version=review.review_version,
            new_status="APPROVED",
            decision_note="direct approval",
            approved_revision_id=get_latest_revision(db, review.id).id,
        )
        assert repo.review_withdrawn_by_operator(db, target.review_id) is False
        assert _service_usable(db, carded)


class TestEffectiveFinalDecision:
    @staticmethod
    def _row(id_, decision, supersedes=None):
        return ApplicationOperatorDecisionRecord(
            id=id_, decision=decision, supersedes_decision_id=supersedes, target_key="t"
        )

    def test_follows_the_supersede_chain(self):
        rows = [
            self._row(1, "APPROVE"),
            self._row(2, "SKIP", supersedes=1),
            self._row(3, "APPROVE", supersedes=2),
        ]
        assert repo.effective_final_decision(rows).id == 3
        assert repo.effective_final_decision(rows[:2]).decision == "SKIP"

    def test_later_never_replaces_a_final_decision(self):
        rows = [self._row(1, "SKIP"), self._row(2, "LATER")]
        assert repo.effective_final_decision(rows).decision == "SKIP"
        assert repo.effective_final_decision([self._row(1, "LATER")]) is None
        assert repo.effective_final_decision([]) is None
