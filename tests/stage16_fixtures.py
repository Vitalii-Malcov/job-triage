"""Stage 16 test fixtures: a Stage 15 application at PACKAGE_READY backed by
a real Stage 6E review package (match, review, revision) on the
project-standard schema. Every identity here is fake."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint, create_match
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import CompanyResearchRecord
from app.db.repositories import upsert_job
from app.db.review_package_repository import create_review
from app.models.application_lifecycle import (
    ApplicationChannel,
    ApplicationEventOrigin,
    ApplicationLifecycleStatus,
)
from app.models.candidate_job_match import CandidateJobMatchData, RequirementMatch
from app.models.job import Job, JobScore
from app.models.review_package import (
    ReviewedBewerbungContent,
    ReviewedCVContent,
    ReviewSectionOrderField,
    ReviewTextField,
)
from app.services.application_tracker import ApplicationTracker
from app.services.telegram import TelegramSendOutcome, TelegramSendResult
from app.services.telegram_operator_control import OperatorCallback

OPERATOR_CHAT = "424242"
STRANGER_CHAT = "666"
NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CANDIDATE_FIRST_NAME = "Maxima"
CANDIDATE_LAST_NAME = "Testperson-Private"
CV_SECRET_SUMMARY = "CV-BODY-SECRET-SUMMARY"
LETTER_SECRET_OPENING = "ANSCHREIBEN-BODY-SECRET"
RESEARCH_EMAIL = "recruiting@acme-employer.example"
RESEARCH_PHONE = "+49 6181 1234567"


def operator_settings(**overrides) -> Settings:
    values = dict(
        telegram_bot_token="123456:TEST-TOKEN-NEVER-REAL",
        telegram_chat_id=OPERATOR_CHAT,
        telegram_operator_control_enabled=True,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _requirement(name: str, status: str, importance: str = "REQUIRED", kind: str = "SKILL"):
    return RequirementMatch(
        requirement=name,
        normalized_requirement=name.lower(),
        requirement_type=kind,
        importance=importance,
        match_status=status,
        reason="fixture",
    )


@dataclass
class Target:
    application_id: int
    job_id: int
    review_id: int
    match_id: int


def build_target(
    db,
    *,
    reference: str = "20385-0000001-S",
    status: ApplicationLifecycleStatus = ApplicationLifecycleStatus.PACKAGE_READY,
    with_package: bool = True,
    manual_overrides: bool = False,
) -> Target:
    profile = get_or_create_candidate_profile(db)
    profile.first_name = CANDIDATE_FIRST_NAME
    profile.last_name = CANDIDATE_LAST_NAME
    db.commit()
    research = CompanyResearchRecord(
        identity_key=f"name:acme-{reference}",
        normalized_company_name=f"acme {reference}",
        company_name="Acme Vacuum GmbH",
        short_summary=(
            f"Industrial vacuum furnaces. Contact {RESEARCH_EMAIL} or {RESEARCH_PHONE}."
        ),
        risk_signals_json=json.dumps(["Small IT team"]),
        research_status="COMPLETED",
        provider_name="fixture",
    )
    db.add(research)
    db.commit()
    job_record, _ = upsert_job(
        db,
        Job(
            source="bundesagentur",
            title="Backend Developer (m/w/d)",
            company="Acme Vacuum GmbH",
            location="Hanau",
            url=f"https://example.com/jobs/{reference}",
            description="Python, PostgreSQL. Kontakt: hr@acme-employer.example",
            source_reference=reference,
        ),
        JobScore(score=82, recommendation="APPLY"),
    )
    fingerprint = compute_job_snapshot_fingerprint(job_record)
    match_record, _ = create_match(
        db,
        CandidateJobMatchData(
            job_id=job_record.id,
            candidate_profile_version=profile.profile_version,
            company_research_id=research.id,
            algorithm_version="v12",
            overall_score=67,
            coverage_score=67,
            required_skill_score=67,
            preferred_skill_score=100,
            experience_support_score=33,
            matched_requirements=[
                _requirement("Python", "MATCH"),
                _requirement("PostgreSQL", "MATCH"),
                _requirement("Docker", "MATCH", "PREFERRED"),
                _requirement("Git", "MATCH"),
            ],
            missing_requirements=[
                _requirement("CI/CD", "MISSING"),
                _requirement("Kubernetes", "MISSING", "PREFERRED"),
                _requirement("German C1", "MISSING", kind="LANGUAGE"),
            ],
            partial_requirements=[_requirement("3 years experience", "PARTIAL", kind="EXPERIENCE")],
            warnings=["Experience support is low"],
        ),
        fingerprint,
    )
    review_id = 0
    if with_package:
        review, _ = create_review(
            db,
            job_id=job_record.id,
            cv_draft_id=11,
            bewerbung_draft_id=22,
            match_id=match_record.id,
            candidate_profile_version=profile.profile_version,
            job_snapshot_fingerprint=fingerprint,
            match_algorithm_version="v12",
            cv_adapter_version="v1",
            bewerbung_generator_version="v4",
            reviewed_cv=ReviewedCVContent(
                professional_title=ReviewTextField(value="Backend Developer"),
                professional_summary=ReviewTextField(value=CV_SECRET_SUMMARY),
                section_order=ReviewSectionOrderField(value=["skills"]),
            ),
            reviewed_bewerbung=ReviewedBewerbungContent(
                subject=ReviewTextField(value="Bewerbung"),
                salutation=ReviewTextField(value="Sehr geehrte Damen und Herren,"),
                opening=ReviewTextField(value=LETTER_SECRET_OPENING),
                closing=ReviewTextField(value="Mit freundlichen Grüßen"),
                signature_name=ReviewTextField(
                    value=f"{CANDIDATE_FIRST_NAME} {CANDIDATE_LAST_NAME}"
                ),
            ),
        )
        if manual_overrides:
            review.has_manual_overrides = True
            db.commit()
        review_id = review.id
    outcome = ApplicationTracker().create_application(
        db,
        company_name="Acme Vacuum GmbH",
        role_title="Backend Developer (m/w/d)",
        source="bundesagentur",
        source_reference=reference,
        application_channel=ApplicationChannel.EMPLOYER_PORTAL,
        initial_status=status,
        origin=ApplicationEventOrigin.SYSTEM,
        actor="stage16-fixture",
        location="Hanau",
        job_id=job_record.id,
        review_package_id=review_id or None,
    )
    return Target(
        application_id=outcome.application.id,
        job_id=job_record.id,
        review_id=review_id,
        match_id=match_record.id,
    )


@dataclass
class FakeTransport:
    """Records every message; returns scripted outcomes (default SENT)."""

    outcomes: list[TelegramSendOutcome] = field(default_factory=list)
    sent: list[tuple[str, dict | None]] = field(default_factory=list)
    next_message_id: int = 1000

    async def send(self, text: str, reply_markup: dict | None = None) -> TelegramSendResult:
        self.sent.append((text, reply_markup))
        outcome = self.outcomes.pop(0) if self.outcomes else TelegramSendOutcome.SENT
        self.next_message_id += 1
        message_id = self.next_message_id if outcome is TelegramSendOutcome.SENT else None
        return TelegramSendResult(outcome, message_id)


def callback(
    data: str,
    *,
    query_id: str = "cbq-1",
    chat_id: str = OPERATOR_CHAT,
    user_id: str | None = OPERATOR_CHAT,
    chat_type: str = "private",
) -> OperatorCallback:
    return OperatorCallback(
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        callback_query_id=query_id,
        data=data,
        message_id=77,
    )
