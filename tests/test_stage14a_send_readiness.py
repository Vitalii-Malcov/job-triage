"""Stage 14A: application destination model + READY_FOR_SEND_PACKAGE gate.

Pure/in-memory only. Nothing here can send: the modules under test have no
transport, and the gate requires OUTBOUND_SENDING_ENABLED to be false.
DB-backed package-currency / loader / CLI tests live in
tests/test_stage14a_send_readiness_db.py.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.application_destination import (
    ApplicationDestination,
    is_channel_verified,
    is_email_send_ready,
    is_strict_https_url,
    is_valid_email_address,
    unknown_destination,
)
from app.models.candidate_job_match import (
    CandidateEvidenceRef,
    CandidateJobMatch,
    RelevantExperience,
    RelevantProject,
    RequirementMatch,
)
from app.models.candidate_profile import (
    CandidateEducation,
    CandidateExperience,
    CandidateLanguage,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
    FieldTrust,
)
from app.models.cv_draft import (
    CVEducationItem,
    CVExperienceItem,
    CVHeader,
    CVProjectItem,
    TailoredCVDraft,
)
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
    CHECK_EDUCATION_REQUIREMENT,
    CHECK_LANGUAGE_REQUIREMENT,
    CHECK_LOCATION_CONSISTENT,
    CHECK_OUTBOUND_DISABLED,
    CHECK_RECIPIENT_TYPE,
    CHECK_RELEVANT_EVIDENCE,
    CHECK_REQUIRED_CV_DATA,
    CHECK_TRUSTED_TITLE,
    PACKAGE_CURRENCY_NOT_CHECKED,
    LocationEvidence,
    SendReadinessInputs,
    evaluate_send_readiness,
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


def _unvalidated_destination(**overrides) -> ApplicationDestination:
    """Bypasses the model validator (as `model_construct` would) to prove the
    predicates re-check every condition themselves."""
    base = _destination().model_dump()
    base.update(overrides)
    return ApplicationDestination.model_construct(**base)


# --- destination model --------------------------------------------------


def test_unknown_destination_requires_reason_and_is_never_verified():
    with pytest.raises(ValidationError):
        ApplicationDestination(job_id=1)
    dest = unknown_destination(1, "no channel published")
    assert not is_channel_verified(dest)
    with pytest.raises(ValidationError):
        unknown_destination(
            1, "x", verification_status="VERIFIED", checked_at=_NOW, verified_by="OPERATOR"
        )


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


@pytest.mark.parametrize("kind", ["APPLICATION_PORTAL", "EMAIL", "CAREERS_PAGE"])
def test_verified_without_operator_is_rejected_by_the_model(kind):
    value = "ai-jobs@example.com" if kind == "EMAIL" else _AD
    with pytest.raises(ValidationError, match="operator"):
        _destination(kind=kind, value=value, verified_by="NONE")


@pytest.mark.parametrize(
    "kind,value", [("APPLICATION_PORTAL", _AD + "#apply"), ("EMAIL", "ai-jobs@example.com")]
)
def test_verified_with_verified_by_none_is_not_ready_even_if_validation_bypassed(kind, value):
    dest = _unvalidated_destination(kind=kind, value=value, verified_by="NONE")
    assert not is_channel_verified(dest)
    assert not is_email_send_ready(dest)
    result = evaluate_send_readiness(_ready_inputs(destination=dest))
    assert _failed(result) == {CHECK_CHANNEL_VERIFIED}


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_type": "JOB_BOARD_LISTING"},
        {"checked_at": None},
        {"source_url": None},
        {"vacancy_specific": False},
        {"confidence": "LOW"},
        {"verification_status": "UNVERIFIED"},
    ],
)
def test_portal_predicate_rechecks_every_condition(overrides):
    assert not is_channel_verified(_unvalidated_destination(**overrides))


@pytest.mark.parametrize(
    "value", ["not-an-email", "a@b", "x@example.com\r\nBcc: y@example.com", "a@b.c, d@e.f"]
)
def test_email_shape_and_injection_rejected(value):
    with pytest.raises(ValidationError):
        _destination(kind="EMAIL", value=value)


def test_portal_requires_https_value():
    with pytest.raises(ValidationError):
        _destination(value="mailto:jobs@example.com")


# --- strict URL / email syntax (Codex P1) --------------------------------

_MALFORMED_URLS = [
    "https://",
    "https:///path",
    "https://?x=1",
    "https://#fragment",
    "http://example.com",
    "javascript:alert(1)",
    "not-a-url",
    "HTTPS://example.com",
    "https://user:pw@careers.example.com/x",
    "https://user@careers.example.com/x",
    "https://careers.example.com\\@evil.example/x",
    "https://careers.example.com/x\r\nX: y",
    "https://careers.example.com/x\x00",
    "https://careers example.com/x",
    "https://localhost/x",
    "https://10.0.0.1/x",
    "https://[::1]/x",
    "https://-careers.example.com/x",
    "https://careers..example.com/x",
    "https://careers.example.com:99999/x",
    "https://careers.example.com:abc/x",
    "https://careers%2eexample.com/x",
    "https://careers.example.com:",
    "https://careers.example.com:/jobs",
    "https://careers.example.com:?x=1",
    "https://careers.example.com:#apply",
    "https://careers.example.com:0",
    "https://careers.example.com:00000",
    "https://careers.example.com:65536",
    "https://careers.example.com:99999",
    "https://careers.example.com:-1",
    "https://careers.example.com:+443",
    "https://careers.example.com: 443",
    "https://careers.example.com:443:443",
    "https://careers.example.com:abc",
    "https://careers.example.com:٤٤٣",
]


@pytest.mark.parametrize("url", _MALFORMED_URLS)
def test_strict_https_url_rejects_malformed_values(url):
    assert not is_strict_https_url(url)


@pytest.mark.parametrize(
    "url",
    [
        _AD,
        _AD + "#apply",
        "https://careers.example.com",
        "https://careers.example.com:1",
        "https://careers.example.com:443",
        "https://careers.example.com:65535/",
        "https://careers.example.com:8443/jobs/123",
        "https://careers.example.com:8443/jobs?id=1",
        "https://xn--bcher-kva.de/",
    ],
)
def test_strict_https_url_accepts_real_https_urls(url):
    assert is_strict_https_url(url)


@pytest.mark.parametrize("url", _MALFORMED_URLS)
def test_model_rejects_malformed_portal_value_and_source_url(url):
    with pytest.raises(ValidationError):
        _destination(value=url)
    with pytest.raises(ValidationError):
        _destination(source_url=url)
    with pytest.raises(ValidationError):
        _destination(kind="CAREERS_PAGE", value=url, verification_status="UNVERIFIED")


@pytest.mark.parametrize(
    "overrides",
    [
        {"value": "https://"},
        {"value": "not-a-url"},
        {"value": "https:///path"},
        {"value": "https://user:pw@careers.example.com/x"},
        {"value": None},
        {"value": 42},
        {"source_url": "https://"},
        {"source_url": "not-a-url"},
        {"source_url": "https://?x=1"},
        {"source_type": "OPERATOR_ENTRY", "source_url": "https://"},
        {"vacancy_specific": "yes"},
        {"value": "https://careers.example.com:"},
        {"value": "https://careers.example.com:0"},
        {"source_url": "https://careers.example.com:"},
        {"source_url": "https://careers.example.com:0"},
        {"checked_at": "not-a-date"},
        {"checked_at": "2026-10-15T00:00:00Z"},
        {"checked_at": 0},
        {"checked_at": None},
    ],
)
def test_bypassed_validation_portal_with_malformed_fields_is_never_verified(overrides):
    dest = _unvalidated_destination(**overrides)
    assert not is_channel_verified(dest)
    assert not is_email_send_ready(dest)
    result = evaluate_send_readiness(_ready_inputs(destination=dest))
    assert CHECK_CHANNEL_VERIFIED in _failed(result)
    assert not result.ready


@pytest.mark.parametrize(
    "value",
    [
        "not-an-email",
        "a@b",
        "a@@example.com",
        "ai-jobs@example.com\r\nBcc: attacker@example.net",
        "ai-jobs@example.com\nBcc: attacker@example.net",
        "ai-jobs@example.com\x00",
        "ai-jobs@example.com, attacker@example.net",
        "ai-jobs@example.com;attacker@example.net",
        "ai-jobs@example.com attacker@example.net",
        None,
        42,
    ],
)
def test_bypassed_validation_malformed_or_injected_email_is_never_ready(value):
    assert not is_valid_email_address(value)
    dest = _unvalidated_destination(kind="EMAIL", value=value)
    assert not is_channel_verified(dest)
    assert not is_email_send_ready(dest)
    assert not evaluate_send_readiness(_ready_inputs(destination=dest)).ready


@pytest.mark.parametrize(
    "overrides",
    [
        {"vacancy_specific": False},
        {"verified_by": "NONE"},
        {"verification_status": "UNVERIFIED"},
        {"source_type": "EMPLOYER_CAREERS_SITE"},
        {"source_type": "OPERATOR_ENTRY"},
        {"source_type": "JOB_BOARD_LISTING"},
        {"confidence": "MEDIUM"},
        {"checked_at": None},
        {"checked_at": "not-a-date"},
        {"checked_at": "2026-10-15T00:00:00Z"},
        {"source_url": "https://"},
        {"source_url": "https://careers.example.com:"},
        {"source_url": "https://careers.example.com:0"},
    ],
)
def test_bypassed_validation_email_rechecks_every_condition(overrides):
    base = {"kind": "EMAIL", "value": "ai-jobs@example.com"}
    assert is_email_send_ready(_unvalidated_destination(**base))
    dest = _unvalidated_destination(**base, **overrides)
    assert not is_email_send_ready(dest)
    assert not is_channel_verified(dest)


def test_scraped_email_is_never_ready():
    scraped = _destination(
        kind="EMAIL",
        value="jobs@example.com",
        verification_status="UNVERIFIED",
        verified_by="NONE",
        confidence="LOW",
    )
    assert not is_channel_verified(scraped)
    assert not is_email_send_ready(scraped)
    result = evaluate_send_readiness(_ready_inputs(destination=scraped))
    assert CHECK_CHANNEL_VERIFIED in _failed(result)
    assert not result.ready


def test_general_careers_email_is_never_ready():
    general = _destination(kind="EMAIL", value="careers@example.com", vacancy_specific=False)
    assert not is_channel_verified(general)
    assert not is_email_send_ready(general)
    # Operator-verified but found on the company-level careers site.
    company_level = _destination(
        kind="EMAIL",
        value="careers@example.com",
        source_type="EMPLOYER_CAREERS_SITE",
        source_url="https://careers.example.com/faq",
    )
    assert not is_channel_verified(company_level)
    assert not is_email_send_ready(company_level)
    for dest in (general, company_level):
        assert _failed(evaluate_send_readiness(_ready_inputs(destination=dest))) == {
            CHECK_CHANNEL_VERIFIED
        }


def test_medium_confidence_email_fails_the_strict_email_rule():
    medium = _destination(kind="EMAIL", value="ai-jobs@example.com", confidence="MEDIUM")
    assert not is_email_send_ready(medium)
    assert not is_channel_verified(medium)  # no weaker email path


def test_careers_page_is_never_package_ready():
    page = _destination(kind="CAREERS_PAGE", value="https://careers.example.com/")
    assert not is_channel_verified(page)


def test_operator_verified_vacancy_specific_channels_are_eligible():
    email = _destination(kind="EMAIL", value="ai-jobs@example.com")
    assert is_email_send_ready(email)
    assert is_channel_verified(email)
    assert is_channel_verified(_destination())
    assert not is_email_send_ready(_destination())  # a portal is not an e-mail
    result = evaluate_send_readiness(_ready_inputs(destination=email))
    assert result.ready, render_send_readiness(result)
    assert result.email_send_ready


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
    assert dest.verification_status == "UNVERIFIED"
    assert dest.verified_by == "NONE"
    assert not is_channel_verified(dest)  # not operator-verified yet
    assert not is_email_send_ready(dest)
    # Stored/BA Frankfurt vs employer Hannover/Muenster: NOT ready.
    assert location_conflicts("Frankfurt am Main", locations) == [
        "JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS",
        "SOURCE_LISTS_OTHER_LOCATIONS:JOB_BOARD_LISTING",
    ]
    assert location_conflicts("Hannover", locations) == [
        "SOURCE_LISTS_OTHER_LOCATIONS:JOB_BOARD_LISTING"
    ]


# --- location evidence --------------------------------------------------


def _employer(locations, **overrides) -> LocationEvidence:
    data = dict(
        source_type="EMPLOYER_JOB_AD",
        source_url=_AD,
        locations=locations,
        authoritative=True,
        checked_at=_NOW,
    )
    data.update(overrides)
    return LocationEvidence(**data)


def test_no_authoritative_location_evidence_is_a_conflict():
    assert location_conflicts("Hannover", []) == ["NO_AUTHORITATIVE_LOCATION_EVIDENCE"]
    board_only = [LocationEvidence(source_type="JOB_BOARD_LISTING", locations=["Hannover"])]
    assert location_conflicts("Hannover", board_only) == ["NO_AUTHORITATIVE_LOCATION_EVIDENCE"]


def test_explicit_consistent_locations_pass():
    evidence = [
        _employer(["Hannover"]),
        _employer(
            [" hannover "],
            source_type="EMPLOYER_CAREERS_SITE",
            source_url="https://careers.example.com/locations",
        ),
        LocationEvidence(source_type="JOB_BOARD_LISTING", locations=["Hannover"]),
    ]
    assert location_conflicts(" hannover ", evidence) == []


def test_two_authoritative_sources_disagree_is_unresolved():
    evidence = [
        _employer(["Hannover"]),
        _employer(
            ["Berlin"],
            source_type="EMPLOYER_CAREERS_SITE",
            source_url="https://careers.example.com/locations",
        ),
    ]
    conflicts = location_conflicts("Hannover", evidence)
    assert "AUTHORITATIVE_SOURCES_DISAGREE" in conflicts
    # Not unioned: "Berlin" is not accepted because one source lists it.
    assert "JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS" in location_conflicts("Berlin", evidence)


def test_employer_vs_board_disagreement_and_board_cannot_overrule():
    evidence = [
        _employer(["Hannover", "Münster"]),
        LocationEvidence(source_type="JOB_BOARD_LISTING", locations=["Frankfurt am Main"]),
    ]
    assert location_conflicts("Frankfurt am Main", evidence) == [
        "JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS",
        "SOURCE_LISTS_OTHER_LOCATIONS:JOB_BOARD_LISTING",
    ]
    # A board/aggregator can never be made authoritative.
    for source_type in ("JOB_BOARD_LISTING", "AGGREGATOR", "OPERATOR_ENTRY", "UNKNOWN"):
        with pytest.raises(ValidationError):
            _employer(["Frankfurt am Main"], source_type=source_type)
    # ...even when validation is bypassed.
    smuggled = LocationEvidence.model_construct(
        source_type="AGGREGATOR",
        source_url=_AD,
        locations=["Frankfurt am Main"],
        authoritative=True,
        checked_at=_NOW,
        note=None,
    )
    assert location_conflicts("Frankfurt am Main", [smuggled]) == [
        "AUTHORITY_NOT_ALLOWED:AGGREGATOR",
        "NO_AUTHORITATIVE_LOCATION_EVIDENCE",
    ]


def test_authoritative_requires_provenance():
    with pytest.raises(ValidationError):
        _employer(["Hannover"], source_url=None)
    with pytest.raises(ValidationError):
        _employer(["Hannover"], checked_at=None)
    with pytest.raises(ValidationError):
        _employer(["Hannover"], source_url="http://careers.example.com/x")


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https:///x",
        "https://?x=1",
        "not-a-url",
        "https://careers.example.com:",
        "https://careers.example.com:0",
    ],
)
def test_malformed_employer_location_source_url_rejected_by_model(url):
    with pytest.raises(ValidationError):
        _employer(["Hannover"], source_url=url)
    with pytest.raises(ValidationError):
        LocationEvidence(source_type="JOB_BOARD_LISTING", source_url=url, locations=["Hannover"])


@pytest.mark.parametrize(
    "url,checked_at",
    [
        ("https://", _NOW),
        ("https:///x", _NOW),
        ("https://?x=1", _NOW),
        ("not-a-url", _NOW),
        (None, _NOW),
        ("https://careers.example.com:", _NOW),
        ("https://careers.example.com:/jobs", _NOW),
        ("https://careers.example.com:0", _NOW),
        ("https://careers.example.com:99999", _NOW),
        ("https://careers.example.com:abc", _NOW),
        (_AD, "not-a-date"),
        (_AD, "2026-10-15T00:00:00Z"),
        (_AD, 0),
        (_AD, None),
    ],
)
def test_malformed_location_source_via_model_construct_is_never_authoritative(url, checked_at):
    smuggled = LocationEvidence.model_construct(
        source_type="EMPLOYER_JOB_AD",
        source_url=url,
        locations=["Hannover"],
        authoritative=True,
        checked_at=checked_at,
        note=None,
    )
    assert not send_readiness._authority_ok(smuggled)
    conflicts = location_conflicts("Hannover", [smuggled])
    assert conflicts != []
    assert conflicts == [
        "AUTHORITY_NOT_ALLOWED:EMPLOYER_JOB_AD",
        "NO_AUTHORITATIVE_LOCATION_EVIDENCE",
    ]
    result = evaluate_send_readiness(_ready_inputs(location_evidence=[smuggled]))
    assert CHECK_LOCATION_CONSISTENT in _failed(result)
    assert not result.ready


@pytest.mark.parametrize("job_location", [None, "", "   "])
def test_missing_persisted_location_is_not_ready(job_location):
    assert location_conflicts(job_location, [_employer(["Hannover"])]) == ["JOB_LOCATION_MISSING"]
    result = evaluate_send_readiness(_ready_inputs(job_location=job_location))
    assert _failed(result) == {CHECK_LOCATION_CONSISTENT}


# --- gate fixtures --------------------------------------------------------


def _profile(complete: bool = True, **overrides) -> CandidateProfile:
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
            experiences=[
                CandidateExperience(id=2, company="X", job_title="Dev", technologies=["Python"]),
                CandidateExperience(id=6, company="Y", job_title="Cashier"),
            ],
            projects=[
                CandidateProject(id=7, name="API", technologies=["Python"]),
                CandidateProject(id=8, name="Garden"),
            ],
            education=[
                CandidateEducation(id=3, institution="School", completed=False),
                CandidateEducation(id=9, institution="University", completed=True),
            ],
            languages=[
                CandidateLanguage(id=4, language="Deutsch", level="C1"),
                CandidateLanguage(id=10, language="Englisch", level="UNKNOWN"),
            ],
        )
    data.update(overrides)
    return CandidateProfile(**data)


def _requirement(rtype: str, status: str, refs=(), importance="REQUIRED", name="x"):
    entity = {"EDUCATION": "EDUCATION", "LANGUAGE": "LANGUAGE"}[rtype]
    return RequirementMatch(
        requirement=name,
        normalized_requirement=name,
        requirement_type=rtype,
        importance=importance,
        match_status=status,
        candidate_evidence=[
            CandidateEvidenceRef(entity_type=entity, entity_id=ref, value="v") for ref in refs
        ],
        reason="r",
    )


def _match(
    *,
    experiences=((2, ["Python"]),),
    projects=(),
    requirements=(),
) -> CandidateJobMatch:
    buckets = {"MATCH": [], "PARTIAL": [], "MISSING": [], "UNKNOWN": []}
    for req in requirements:
        buckets[req.match_status].append(req)
    return CandidateJobMatch(
        id=2,
        created_at=_NOW,
        job_id=1,
        candidate_profile_version=5,
        company_research_id=None,
        algorithm_version="v1",
        overall_score=80,
        coverage_score=80,
        required_skill_score=80,
        preferred_skill_score=100,
        experience_support_score=100,
        matched_requirements=buckets["MATCH"],
        partial_requirements=buckets["PARTIAL"],
        missing_requirements=buckets["MISSING"],
        unknown_requirements=buckets["UNKNOWN"],
        relevant_experiences=[
            RelevantExperience(experience_id=i, company="X", job_title="Dev", matched_skills=s)
            for i, s in experiences
        ],
        relevant_projects=[
            RelevantProject(project_id=i, name="API", matched_skills=s) for i, s in projects
        ],
    )


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


def _cv_experience(source_id: int, matched=("Python",)) -> CVExperienceItem:
    return CVExperienceItem(
        source_id=source_id,
        company="X",
        job_title="Dev",
        start_date=None,
        end_date=None,
        is_current=False,
        location=None,
        description=None,
        matched_skills=list(matched),
        emphasis="HIGH" if matched else "STANDARD",
    )


def _cv_project(source_id: int, matched=("Python",)) -> CVProjectItem:
    return CVProjectItem(
        source_id=source_id,
        name="API",
        description=None,
        role=None,
        repository_url=None,
        demo_url=None,
        start_date=None,
        end_date=None,
        matched_skills=list(matched),
    )


def _cv_education(source_id: int, completed: bool) -> CVEducationItem:
    return CVEducationItem(
        source_id=source_id,
        institution="School",
        program=None,
        degree=None,
        field_of_study=None,
        start_date=None,
        end_date=None,
        completed=completed,
        location=None,
    )


def _cv(experience=None, projects=(), education=()) -> TailoredCVDraft:
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
        experience=[_cv_experience(2)] if experience is None else list(experience),
        projects=list(projects),
        education=list(education),
    )


def _ready_inputs(**overrides) -> SendReadinessInputs:
    data = dict(
        job_id=1,
        job_location="Hannover",
        profile=_profile(),
        outbound_sending_enabled=False,
        approved_package=_package(),
        approved_cv=_cv(),
        match=_match(),
        package_problems=(),
        destination=_destination(),
        location_evidence=[_employer(["Hannover"])],
    )
    data.update(overrides)
    return SendReadinessInputs(**data)


def _failed(result) -> set[str]:
    return {check.code for check in result.blockers}


# --- gate ---------------------------------------------------------------


def test_fully_ready_inputs_pass_every_check():
    result = evaluate_send_readiness(_ready_inputs())
    assert result.ready, render_send_readiness(result)
    assert not result.email_send_ready  # a portal is not an e-mail recipient
    assert render_send_readiness(result).startswith("READY_FOR_SEND_PACKAGE = YES")


def test_outbound_enabled_makes_gate_not_ready():
    result = evaluate_send_readiness(_ready_inputs(outbound_sending_enabled=True))
    assert not result.ready
    assert _failed(result) == {CHECK_OUTBOUND_DISABLED}


def test_inputs_without_currency_check_fail_closed():
    inputs = _ready_inputs()
    data = {**inputs.__dict__}
    data.pop("package_problems")
    result = evaluate_send_readiness(SendReadinessInputs(**data))
    assert _failed(result) == {CHECK_APPROVED_IS_CURRENT}
    assert PACKAGE_CURRENCY_NOT_CHECKED in result.blockers[0].detail


def test_package_problems_block_currency():
    result = evaluate_send_readiness(_ready_inputs(package_problems=("STALE_JOB_FINGERPRINT",)))
    assert _failed(result) == {CHECK_APPROVED_IS_CURRENT}


def test_stage13d_shape_fails_with_explicit_reasons():
    """Profile with name+skills only, approved package without a title,
    CV without experience, BA-only location, unverified portal."""
    result = evaluate_send_readiness(
        _ready_inputs(
            profile=_profile(complete=False),
            approved_package=_package(title=None, version=5),
            approved_cv=_cv(experience=[]),
            destination=_destination(verification_status="UNVERIFIED", verified_by="NONE"),
            job_location="Frankfurt am Main",
            location_evidence=[
                _employer(["Hannover", "Münster"]),
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


def test_package_displaying_a_non_approved_revision_fails():
    package = _package().model_copy(update={"current_revision_id": 13})
    assert CHECK_APPROVED_REVISION in _failed(
        evaluate_send_readiness(_ready_inputs(approved_package=package))
    )


def test_unknown_destination_fails_channel_and_type():
    result = evaluate_send_readiness(
        _ready_inputs(destination=unknown_destination(1, "nothing published"))
    )
    assert _failed(result) == {CHECK_CHANNEL_VERIFIED, CHECK_RECIPIENT_TYPE}
    assert _failed(evaluate_send_readiness(_ready_inputs(destination=None))) == {
        CHECK_CHANNEL_VERIFIED,
        CHECK_RECIPIENT_TYPE,
    }


# --- trusted title consistency -------------------------------------------


def test_untrusted_profile_title_fails_even_if_package_has_one():
    profile = _profile()
    profile.field_trust = {
        **profile.field_trust,
        "professional_title": FieldTrust(source="INFERRED"),
    }
    result = evaluate_send_readiness(_ready_inputs(profile=profile))
    assert _failed(result) == {CHECK_TRUSTED_TITLE}


@pytest.mark.parametrize("reviewed", ["Senior AI Architect", "Python Developer (Lead)", None, ""])
def test_reviewed_title_differing_from_trusted_title_is_not_ready(reviewed):
    result = evaluate_send_readiness(_ready_inputs(approved_package=_package(title=reviewed)))
    assert _failed(result) == {CHECK_TRUSTED_TITLE}


def test_user_edited_title_equal_to_trusted_title_passes():
    package = _package()
    edited = package.reviewed_cv.model_copy(
        update={"professional_title": ReviewTextField(value="Python Developer", origin="USER_EDIT")}
    )
    result = evaluate_send_readiness(
        _ready_inputs(approved_package=package.model_copy(update={"reviewed_cv": edited}))
    )
    assert result.ready, render_send_readiness(result)


def test_trusted_title_changed_after_approval_is_not_ready():
    result = evaluate_send_readiness(
        _ready_inputs(profile=_profile(professional_title="Data Engineer"))
    )
    assert CHECK_TRUSTED_TITLE in _failed(result)


# --- relevant structured evidence ----------------------------------------


def test_unrelated_trusted_experience_is_not_ready():
    """Experience 6 is trusted and cited, but the pinned match does not mark
    it relevant (no matched skills)."""
    result = evaluate_send_readiness(
        _ready_inputs(
            match=_match(experiences=()),
            approved_cv=_cv(experience=[_cv_experience(6, matched=())]),
        )
    )
    assert _failed(result) == {CHECK_RELEVANT_EVIDENCE}
    # A relevance entry without matched skills does not count either.
    result = evaluate_send_readiness(_ready_inputs(match=_match(experiences=((6, []),))))
    assert _failed(result) == {CHECK_RELEVANT_EVIDENCE}


def test_relevant_matched_experience_passes():
    assert evaluate_send_readiness(_ready_inputs()).ready


def test_relevant_experience_not_cited_by_the_approved_cv_is_not_ready():
    result = evaluate_send_readiness(_ready_inputs(approved_cv=_cv(experience=[])))
    assert _failed(result) == {CHECK_RELEVANT_EVIDENCE}


def test_relevant_experience_no_longer_trusted_is_not_ready():
    profile = _profile()
    profile.experiences[0].confidence = "UNCONFIRMED"
    result = evaluate_send_readiness(_ready_inputs(profile=profile))
    assert CHECK_RELEVANT_EVIDENCE in _failed(result)


def test_unrelated_project_is_not_ready():
    result = evaluate_send_readiness(
        _ready_inputs(
            match=_match(experiences=(), projects=()),
            approved_cv=_cv(experience=[], projects=[_cv_project(8, matched=())]),
        )
    )
    assert _failed(result) == {CHECK_RELEVANT_EVIDENCE}


def test_relevant_pinned_project_passes():
    result = evaluate_send_readiness(
        _ready_inputs(
            match=_match(experiences=(), projects=((7, ["Python"]),)),
            approved_cv=_cv(experience=[], projects=[_cv_project(7)]),
        )
    )
    assert result.ready, render_send_readiness(result)


def test_match_not_pinned_to_the_cv_is_not_ready():
    other = _match().model_copy(update={"id": 99})
    assert CHECK_RELEVANT_EVIDENCE in _failed(evaluate_send_readiness(_ready_inputs(match=other)))
    assert CHECK_RELEVANT_EVIDENCE in _failed(evaluate_send_readiness(_ready_inputs(match=None)))


# --- requirement-aware education / language -------------------------------


def test_no_education_or_language_requirement_does_not_require_them():
    profile = _profile(education=[], languages=[])
    result = evaluate_send_readiness(_ready_inputs(profile=profile))
    assert result.ready, render_send_readiness(result)


def test_name_is_always_required():
    profile = _profile(field_trust={"professional_title": _TRUSTED})
    assert _failed(evaluate_send_readiness(_ready_inputs(profile=profile))) == {
        CHECK_REQUIRED_CV_DATA
    }


def test_required_degree_satisfied_only_by_trusted_completed_education():
    ok = _requirement("EDUCATION", "MATCH", refs=[9], name="completed-degree")
    assert evaluate_send_readiness(_ready_inputs(match=_match(requirements=[ok]))).ready
    missing = _requirement("EDUCATION", "MISSING", name="completed-degree")
    assert _failed(
        evaluate_send_readiness(_ready_inputs(match=_match(requirements=[missing])))
    ) == {CHECK_EDUCATION_REQUIREMENT}
    # A MATCH that rests on the unfinished row 3 is refused.
    unfinished = _requirement("EDUCATION", "MATCH", refs=[3], name="completed-degree")
    assert _failed(
        evaluate_send_readiness(_ready_inputs(match=_match(requirements=[unfinished])))
    ) == {CHECK_EDUCATION_REQUIREMENT}
    # The completed row is no longer trusted.
    profile = _profile()
    profile.education[1].source = "INFERRED"
    assert _failed(
        evaluate_send_readiness(_ready_inputs(profile=profile, match=_match(requirements=[ok])))
    ) == {CHECK_EDUCATION_REQUIREMENT}


def test_preferred_degree_is_advisory_but_unknown_importance_binds():
    preferred = _requirement("EDUCATION", "MISSING", importance="PREFERRED")
    assert evaluate_send_readiness(_ready_inputs(match=_match(requirements=[preferred]))).ready
    unknown = _requirement("EDUCATION", "MISSING", importance="UNKNOWN")
    assert CHECK_EDUCATION_REQUIREMENT in _failed(
        evaluate_send_readiness(_ready_inputs(match=_match(requirements=[unknown])))
    )


def test_cv_never_presents_unfinished_education_as_completed():
    result = evaluate_send_readiness(
        _ready_inputs(approved_cv=_cv(education=[_cv_education(3, completed=True)]))
    )
    assert _failed(result) == {CHECK_EDUCATION_REQUIREMENT}
    ok = evaluate_send_readiness(
        _ready_inputs(
            approved_cv=_cv(
                education=[_cv_education(3, completed=False), _cv_education(9, completed=True)]
            )
        )
    )
    assert ok.ready, render_send_readiness(ok)


def test_required_language_level_must_be_known_and_sufficient():
    ok = _requirement("LANGUAGE", "MATCH", refs=[4], name="german-c1")
    assert evaluate_send_readiness(_ready_inputs(match=_match(requirements=[ok]))).ready
    for bad in (
        _requirement("LANGUAGE", "UNKNOWN", refs=[10], name="english-b2"),  # level unknown
        _requirement("LANGUAGE", "PARTIAL", refs=[4], name="german-c2"),  # level too low
        _requirement("LANGUAGE", "MISSING", name="english-b2"),  # no record
        _requirement("LANGUAGE", "MATCH", refs=[10], name="english-b2"),  # UNKNOWN row
    ):
        assert _failed(
            evaluate_send_readiness(_ready_inputs(match=_match(requirements=[bad])))
        ) == {CHECK_LANGUAGE_REQUIREMENT}, bad


def test_preferred_language_is_advisory():
    preferred = _requirement("LANGUAGE", "MISSING", importance="PREFERRED", name="english-b2")
    assert evaluate_send_readiness(_ready_inputs(match=_match(requirements=[preferred]))).ready


# --- module hygiene --------------------------------------------------------


def test_gate_module_has_no_transport_imports():
    source = Path(send_readiness.__file__).read_text(encoding="utf-8")
    for forbidden in ("smtplib", "imaplib", "httpx", "send_telegram_message", "requests"):
        assert forbidden not in source
