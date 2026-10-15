"""Stage 14A: application destination model + READY_FOR_SEND_PACKAGE gate.

Pure/in-memory only. Nothing here can send: the modules under test have no
transport, and the gate requires OUTBOUND_SENDING_ENABLED to be false.
"""

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.profile_readiness import evaluate_profile_readiness
from app.core.config import Settings
from app.db.base import Base
from app.db.models import CandidateProfileRecord, JobRecord
from app.models.application_destination import (
    ApplicationDestination,
    is_channel_verified,
    is_email_send_ready,
    unknown_destination,
)
from app.models.candidate_profile import (
    CandidateEducation,
    CandidateExperience,
    CandidateLanguage,
    CandidateProfile,
    CandidateSkill,
    FieldTrust,
)
from app.models.cv_draft import CVExperienceItem, CVHeader, TailoredCVDraft
from app.models.review_package import (
    ReviewedBewerbungContent,
    ReviewedCVContent,
    ReviewPackage,
    ReviewSectionOrderField,
    ReviewTextField,
)
from app.services import send_readiness
from app.services.send_readiness import (
    CHECK_APPROVED_IS_CURRENT,
    CHECK_APPROVED_REVISION,
    CHECK_CHANNEL_VERIFIED,
    CHECK_LOCATION_CONSISTENT,
    CHECK_OUTBOUND_DISABLED,
    CHECK_RECIPIENT_TYPE,
    CHECK_RELEVANT_EVIDENCE,
    CHECK_REQUIRED_CV_DATA,
    CHECK_TRUSTED_TITLE,
    LocationEvidence,
    SendReadinessInputs,
    evaluate_send_readiness,
    load_send_readiness_inputs,
    location_conflicts,
    render_send_readiness,
)

_NOW = datetime(2026, 10, 15, tzinfo=UTC)
_AD = "https://careers.example.com/jobs/ai-engineer"
_TRUSTED = FieldTrust()


def _destination(**overrides) -> ApplicationDestination:
    data = dict(
        job_id=1,
        kind="APPLICATION_PORTAL",
        value=_AD + "#apply",
        source_url=_AD,
        source_type="EMPLOYER_JOB_AD",
        verification_status="VERIFIED",
        confidence="HIGH",
        verified_by="OPERATOR",
        vacancy_specific=True,
        checked_at=_NOW,
    )
    data.update(overrides)
    return ApplicationDestination(**data)


# --- destination model --------------------------------------------------


def test_unknown_destination_requires_reason_and_is_never_verified():
    with pytest.raises(ValidationError):
        ApplicationDestination(job_id=1)
    dest = unknown_destination(1, "no channel published")
    assert not is_channel_verified(dest)
    with pytest.raises(ValidationError):
        unknown_destination(1, "x", verification_status="VERIFIED", checked_at=_NOW)


@pytest.mark.parametrize("source_type", ["JOB_BOARD_LISTING", "AGGREGATOR", "UNKNOWN"])
def test_non_employer_sources_cannot_verify(source_type):
    with pytest.raises(ValidationError):
        _destination(source_type=source_type)


def test_verified_requires_checked_at_and_https_source():
    with pytest.raises(ValidationError):
        _destination(checked_at=None)
    with pytest.raises(ValidationError):
        _destination(source_url="http://careers.example.com/x")
    with pytest.raises(ValidationError):
        _destination(source_url=None)


@pytest.mark.parametrize(
    "value", ["not-an-email", "a@b", "x@example.com\r\nBcc: y@example.com", "a@b.c, d@e.f"]
)
def test_email_shape_and_injection_rejected(value):
    with pytest.raises(ValidationError):
        _destination(kind="EMAIL", value=value)


def test_portal_requires_https_value():
    with pytest.raises(ValidationError):
        _destination(value="mailto:jobs@example.com")


def test_scraped_email_is_never_send_ready():
    scraped = _destination(
        kind="EMAIL",
        value="jobs@example.com",
        verification_status="UNVERIFIED",
        verified_by="NONE",
        confidence="LOW",
    )
    assert not is_channel_verified(scraped)
    assert not is_email_send_ready(scraped)
    # Even marked VERIFIED, without an operator it is not send-ready.
    auto = _destination(kind="EMAIL", value="jobs@example.com", verified_by="NONE")
    assert is_channel_verified(auto)
    assert not is_email_send_ready(auto)


def test_general_inbox_email_is_not_send_ready():
    general = _destination(kind="EMAIL", value="careers@example.com", vacancy_specific=False)
    assert not is_channel_verified(general)
    assert not is_email_send_ready(general)


def test_operator_verified_vacancy_email_is_send_ready_but_portal_is_not_email():
    email = _destination(kind="EMAIL", value="ai-jobs@example.com")
    assert is_email_send_ready(email)
    assert is_channel_verified(_destination())
    assert not is_email_send_ready(_destination())


def test_committed_e362b_research_artifacts_parse_and_fail_closed():
    root = Path(__file__).resolve().parents[1] / "docs" / "research"
    dest = ApplicationDestination.model_validate(
        json.loads((root / "stage14a_e362b_destination.json").read_text(encoding="utf-8"))
    )
    locations = [
        LocationEvidence.model_validate(item)
        for item in json.loads((root / "stage14a_e362b_locations.json").read_text(encoding="utf-8"))
    ]
    assert dest.kind == "APPLICATION_PORTAL"
    assert not is_channel_verified(dest)  # not operator-verified yet
    assert not is_email_send_ready(dest)
    assert location_conflicts("Frankfurt am Main", locations) == [
        "JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS",
        "SOURCE_LISTS_OTHER_LOCATIONS:JOB_BOARD_LISTING",
    ]
    assert location_conflicts("Hannover", locations) == [
        "SOURCE_LISTS_OTHER_LOCATIONS:JOB_BOARD_LISTING"
    ]


# --- location evidence --------------------------------------------------


def test_no_authoritative_location_evidence_is_a_conflict():
    assert location_conflicts("Hannover", []) == ["NO_AUTHORITATIVE_LOCATION_EVIDENCE"]


def test_consistent_locations_casefolded():
    evidence = [
        LocationEvidence(source_type="EMPLOYER_JOB_AD", locations=["Hannover"], authoritative=True)
    ]
    assert location_conflicts(" hannover ", evidence) == []


# --- gate ---------------------------------------------------------------


def _profile(complete: bool = True) -> CandidateProfile:
    data = dict(
        id=1,
        profile_version=5,
        created_at=_NOW,
        updated_at=_NOW,
        first_name="Anna",
        last_name="Example",
        field_trust={"first_name": _TRUSTED, "last_name": _TRUSTED},
        skills=[CandidateSkill(id=1, name="Python")],
    )
    if complete:
        data.update(
            professional_title="Python Developer",
            field_trust={**data["field_trust"], "professional_title": _TRUSTED},
            experiences=[CandidateExperience(id=2, company="X", job_title="Dev")],
            education=[CandidateEducation(id=3, institution="School", completed=False)],
            languages=[CandidateLanguage(id=4, language="Deutsch", level="C1")],
        )
    return CandidateProfile(**data)


def _package(title: str | None = "Python Developer", version: int = 5) -> ReviewPackage:
    text = ReviewTextField
    return ReviewPackage(
        id=9,
        current_revision_id=12,
        created_at=_NOW,
        updated_at=_NOW,
        job_id=1,
        cv_draft_id=3,
        bewerbung_draft_id=4,
        match_id=2,
        candidate_profile_version=version,
        job_snapshot_fingerprint="f" * 64,
        match_algorithm_version="v1",
        cv_adapter_version="v2",
        bewerbung_generator_version="v3",
        status="APPROVED",
        review_version=1,
        has_manual_overrides=False,
        verification_state="EVIDENCE_BOUND",
        reviewed_cv=ReviewedCVContent(
            professional_title=text(value=title),
            professional_summary=text(value=None),
            section_order=ReviewSectionOrderField(value=[]),
        ),
        reviewed_bewerbung=ReviewedBewerbungContent(
            subject=text(value="s"),
            salutation=text(value="s"),
            opening=text(value="o"),
            closing=text(value="c"),
            signature_name=text(value="Anna Example"),
        ),
        approved_revision_id=12,
        decided_at=_NOW,
    )


def _cv(with_experience: bool = True) -> TailoredCVDraft:
    experience = (
        [
            CVExperienceItem(
                source_id=2,
                company="X",
                job_title="Dev",
                start_date=None,
                end_date=None,
                is_current=False,
                location=None,
                description=None,
                responsibilities=[],
                achievements=[],
                technologies=[],
                matched_skills=["Python"],
                emphasis="HIGH",
            )
        ]
        if with_experience
        else []
    )
    return TailoredCVDraft(
        id=3,
        created_at=_NOW,
        job_id=1,
        match_id=2,
        candidate_profile_version=5,
        match_algorithm_version="v1",
        cv_adapter_version="v2",
        header=CVHeader(),
        professional_summary=None,
        section_order=[],
        projects_emphasis="STANDARD",
        experience=experience,
    )


def _ready_inputs(**overrides) -> SendReadinessInputs:
    data = dict(
        job_id=1,
        job_location="Hannover",
        profile_report=evaluate_profile_readiness(_profile()),
        outbound_sending_enabled=False,
        approved_package=_package(),
        approved_cv=_cv(),
        destination=_destination(),
        location_evidence=[
            LocationEvidence(
                source_type="EMPLOYER_JOB_AD", locations=["Hannover"], authoritative=True
            )
        ],
    )
    data.update(overrides)
    return SendReadinessInputs(**data)


def _failed(result) -> set[str]:
    return {check.code for check in result.blockers}


def test_fully_ready_inputs_pass_every_check():
    result = evaluate_send_readiness(_ready_inputs())
    assert result.ready, render_send_readiness(result)
    assert not result.email_send_ready  # a portal is not an e-mail recipient
    assert render_send_readiness(result).startswith("READY_FOR_SEND_PACKAGE = YES")


def test_outbound_enabled_makes_gate_not_ready():
    result = evaluate_send_readiness(_ready_inputs(outbound_sending_enabled=True))
    assert not result.ready
    assert _failed(result) == {CHECK_OUTBOUND_DISABLED}


def test_stage13d_shape_fails_with_explicit_reasons():
    """Profile with name+skills only, approved package without a title,
    CV without experience, BA-only location, unverified portal."""
    result = evaluate_send_readiness(
        _ready_inputs(
            profile_report=evaluate_profile_readiness(_profile(complete=False)),
            approved_package=_package(title=None, version=5),
            approved_cv=_cv(with_experience=False),
            destination=_destination(verification_status="UNVERIFIED", verified_by="NONE"),
            job_location="Frankfurt am Main",
            location_evidence=[
                LocationEvidence(
                    source_type="EMPLOYER_JOB_AD",
                    locations=["Hannover", "Münster"],
                    authoritative=True,
                ),
                LocationEvidence(
                    source_type="JOB_BOARD_LISTING",
                    locations=["Hannover", "Frankfurt am Main", "Münster"],
                ),
            ],
        )
    )
    assert not result.ready
    assert _failed(result) == {
        CHECK_TRUSTED_TITLE,
        CHECK_RELEVANT_EVIDENCE,
        CHECK_REQUIRED_CV_DATA,
        CHECK_CHANNEL_VERIFIED,
        CHECK_LOCATION_CONSISTENT,
    }


def test_missing_approval_and_stale_package_fail():
    assert {CHECK_APPROVED_REVISION, CHECK_APPROVED_IS_CURRENT} <= _failed(
        evaluate_send_readiness(_ready_inputs(approved_package=None))
    )
    assert _failed(
        evaluate_send_readiness(_ready_inputs(approved_package=_package(version=4)))
    ) == {CHECK_APPROVED_IS_CURRENT}


def test_unknown_destination_fails_channel_and_type():
    result = evaluate_send_readiness(
        _ready_inputs(destination=unknown_destination(1, "nothing published"))
    )
    assert _failed(result) == {CHECK_CHANNEL_VERIFIED, CHECK_RECIPIENT_TYPE}
    assert _failed(evaluate_send_readiness(_ready_inputs(destination=None))) == {
        CHECK_CHANNEL_VERIFIED,
        CHECK_RECIPIENT_TYPE,
    }


def test_title_only_in_package_is_not_enough():
    """A human-typed title in the review does not replace a trusted
    profile title (and vice versa)."""
    profile = _profile()
    profile.field_trust = {
        **profile.field_trust,
        "professional_title": FieldTrust(source="INFERRED"),
    }
    result = evaluate_send_readiness(
        _ready_inputs(profile_report=evaluate_profile_readiness(profile))
    )
    assert _failed(result) == {CHECK_TRUSTED_TITLE}


# --- loader: read-only, no network ---------------------------------------


def test_loader_reads_without_writing_or_network(monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(CandidateProfileRecord(id=1, profile_version=1))
        db.add(
            JobRecord(
                id=1,
                fingerprint="f" * 64,
                source="bundesagentur",
                title="AI Engineer",
                company="Example",
                location="Frankfurt am Main",
                url="https://example.com/1",
                description="",
                skills_json="[]",
                score=83,
                recommendation="APPLY",
                status="NEW",
            )
        )
        db.commit()
        job = db.get(JobRecord, 1)
        inputs = load_send_readiness_inputs(
            db,
            job,
            settings=Settings(_env_file=None),
            destination=None,
            location_evidence=[],
        )
        assert not db.new and not db.dirty and not db.deleted
        assert inputs.approved_package is None
        assert inputs.outbound_sending_enabled is False
        result = evaluate_send_readiness(inputs)
        assert not result.ready
        with pytest.raises(ValueError):
            load_send_readiness_inputs(
                db,
                job,
                settings=Settings(_env_file=None),
                destination=unknown_destination(2, "other job"),
                location_evidence=[],
            )


def test_gate_module_has_no_transport_imports():
    source = Path(send_readiness.__file__).read_text(encoding="utf-8")
    for forbidden in ("smtplib", "imaplib", "httpx", "send_telegram_message", "requests"):
        assert forbidden not in source
