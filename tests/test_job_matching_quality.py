"""Job matching quality: role direction is separate from data completeness.

An unconfirmed route keeps a developer vacancy VISIBLE (MANUAL_REVIEW with a reason) but never
admits it to automatic preparation; adjacent roles rank after Python/backend roles; confirmed
barriers (administration, support, senior, out of reach) stay excluded. Nothing here sends
anything, writes a status or touches a Bewerbung.
"""

import pytest

from app.services.job_fit_assessment import Criterion, FitAssessment
from app.services.shortlist_gate import (
    automatic_block_reason,
    automatic_preparation_check,
)
from app.services.triage_category import (
    CONFIRMED_BARRIER,
    DATA_REVIEW_REQUIRED,
    HYBRID_ADMIN_ROLE,
    NOT_TARGET_ROLE,
    ROLE_UNCLEAR,
    ROUTE_REVIEW_REQUIRED,
    STATED_GAP,
    classify,
)


def _assessment(
    *,
    title="Junior Python Developer (m/w/d)",
    role="PRIMARY",
    skills="STRONG",
    location="ESTIMATE_WITHIN",
    entry="OPEN",
    language="NO_REQUIREMENT",
    salary="UNKNOWN",
    start="UNKNOWN",
    experience="UNKNOWN",
) -> FitAssessment:
    a = FitAssessment(job_id=1, company="Example GmbH", title=title)
    a.criteria = [
        Criterion("role", role, "r"),
        Criterion("skills", skills, "s"),
        Criterion("location", location, "l"),
        Criterion("entry_level", entry, "e"),
        Criterion("language", language, "g"),
        Criterion("salary", salary, "p"),
        Criterion("start_date", start, "d"),
        Criterion("experience", experience, "x"),
    ]
    return a


class TestUnconfirmedRoute:
    @pytest.mark.parametrize(
        "location",
        ["DISTANCE_UNVERIFIED", "LOCATION_UNVERIFIED", "UNKNOWN", "ESTIMATE_NEAR_LIMIT"],
    )
    def test_developer_role_stays_visible_but_is_not_prepared(self, location):
        a = _assessment(location=location)
        result = classify(a)
        assert result.category == "MANUAL_REVIEW"
        assert result.code == ROUTE_REVIEW_REQUIRED
        assert result.reason  # the operator sees why
        # an unknown route is never consent to prepare
        assert automatic_block_reason(a) == "FIT_TIER_NEEDS_OPERATOR"

    def test_other_open_fact_is_a_data_review_not_a_route_review(self):
        a = _assessment(skills="NOT_EVALUATED")
        result = classify(a)
        assert (result.category, result.code) == ("MANUAL_REVIEW", DATA_REVIEW_REQUIRED)
        assert automatic_block_reason(a) is not None

    def test_confirmed_route_lets_the_vacancy_through_the_existing_gate(self):
        a = _assessment(location="ESTIMATE_WITHIN")
        assert classify(a).category == "PRIMARY"
        assert automatic_block_reason(a) is None  # still no approval to apply


class TestGeographyAndRemote:
    def test_remote_developer_role_is_primary(self):
        a = _assessment(location="REMOTE_OK")
        assert classify(a).category == "PRIMARY"
        assert automatic_block_reason(a) is None

    def test_hessen_commute_is_primary(self):
        assert classify(_assessment(location="ESTIMATE_WITHIN")).category == "PRIMARY"

    def test_relocation_only_region_stays_a_manual_decision(self):
        result = classify(_assessment(location="ESTIMATE_BEYOND_RELOCATION"))
        assert (result.category, result.code) == ("MANUAL_REVIEW", ROUTE_REVIEW_REQUIRED)

    def test_far_away_workplace_is_excluded(self):
        result = classify(_assessment(location="ESTIMATE_BEYOND"))
        assert (result.category, result.code) == ("EXCLUDED", CONFIRMED_BARRIER)


class TestSeniority:
    def test_junior_is_primary(self):
        assert classify(_assessment(entry="ENTRY_LEVEL")).category == "PRIMARY"

    def test_senior_title_is_excluded_whatever_the_skills(self):
        result = classify(_assessment(entry="SENIOR_TITLE", skills="STRONG"))
        assert result.category == "EXCLUDED"


class TestMissingSecondaryFacts:
    def test_missing_salary_and_start_date_do_not_hold_a_developer_role(self):
        a = _assessment(salary="UNKNOWN", start="UNKNOWN")
        assert classify(a).category == "PRIMARY"
        assert automatic_block_reason(a) is None


class TestRoleFamily:
    def test_python_developer_is_primary_and_ranks_first(self):
        assert (
            classify(_assessment(role="PRIMARY")).rank
            < classify(_assessment(title="Datenanalyst (m/w/d)", role="OTHER")).rank
        )

    def test_data_or_devops_title_is_adjacent_not_primary(self):
        result = classify(
            _assessment(title="DevOps Engineer (m/w/d)", role="OTHER", skills="STRONG")
        )
        assert result.category == "ADJACENT"

    def test_other_role_without_skill_evidence_is_excluded(self):
        result = classify(_assessment(title="Datenanalyst (m/w/d)", role="OTHER", skills="WEAK"))
        assert (result.category, result.code) == ("EXCLUDED", NOT_TARGET_ROLE)

    @pytest.mark.parametrize("skills", ["STRONG", "PARTIAL"])
    def test_title_without_it_signal_is_a_question_not_a_verdict(self, skills):
        result = classify(
            _assessment(title="Sachbearbeiter (m/w/d)", role="NO_IT_SIGNAL", skills=skills)
        )
        assert (result.category, result.code) == ("MANUAL_REVIEW", ROLE_UNCLEAR)

    def test_administration_and_support_are_excluded(self):
        a = _assessment(role="OFF_ROLE", skills="STRONG")
        result = classify(a)
        assert result.category == "EXCLUDED"
        assert automatic_block_reason(a) == "FIT_TIER_NOT_SUITABLE"

    def test_stated_gap_goes_to_the_operator_and_is_not_prepared(self):
        a = _assessment(language="GAP")
        result = classify(a)
        assert (result.category, result.code) == ("MANUAL_REVIEW", STATED_GAP)
        assert automatic_block_reason(a) == "FIT_TIER_STRETCH"

    @pytest.mark.parametrize(
        "title",
        [
            "IT-Administrator / Softwareentwickler (m/w/d)",
            "IT-Systemadministrator / Softwareentwickler m/w/d",
            "IT-Administrator / (Softwareentwickler)",
            "IT-Support für Softwareentwickler",
            "IT-Support - Anwendungsentwickler",
            "IT-Support - Platform / Python Entwickler",
            "IT-Support - Software-Entwickler",
            "IT-Support (für unsere) Plattform / Python Entwickler",
            "Systemadministrator und Python Entwickler",
        ],
    )
    def test_hybrid_admin_and_developer_title_is_never_prepared_automatically(self, title):
        a = _assessment(location="ESTIMATE_WITHIN")
        a.title = title
        assert a.fit_tier in {"GOOD_MATCH", "PARTIAL_MATCH"}  # the tier alone would admit it
        result = classify(a)
        assert (result.category, result.code) == ("MANUAL_REVIEW", HYBRID_ADMIN_ROLE)
        assert automatic_block_reason(a) == "HYBRID_ADMIN_ROLE"

    @pytest.mark.parametrize(
        "title",
        [
            "Junior Python Developer (m/w/d)",
            "Backend Entwickler mit Docker und Linux",
            "FastAPI Developer",
            "Backend Entwickler Linux/Docker (m/w/d)",
            "Junior Python Entwickler Service Desk Tools (m/w/d)",
            "Junior Software Developer IT-Support Tools",
            "Software Development Engineer - IT-Support Tools",
            "Junior Python Entwickler für IT-Support-Plattform",
            "Junior Python Entwickler Applikation für Service Desk Lösung",
        ],
    )
    def test_developer_title_with_infrastructure_words_is_not_a_hybrid(self, title):
        a = _assessment()
        a.title = title
        assert classify(a).category == "PRIMARY"
        assert automatic_block_reason(a) is None


# --- ranking through the real shortlist step ------------------------------------------------
import asyncio  # noqa: E402

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.models import ApplicationRecord  # noqa: E402
from app.services.automation_shortlist import prepare_shortlist_drafts  # noqa: E402
from tests.test_automation_shortlist import (  # noqa: E402
    TIER_0,
    TIER_60,
    _seed_job,
    _settings,
    _touch,
)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quality.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, autocommit=False)() as session:
        yield session
    engine.dispose()


def test_primary_role_is_not_displaced_by_a_higher_scoring_adjacent_role(db, monkeypatch):
    from app.services import shortlist_gate

    adjacent = _seed_job(db, tier=TIER_60, title="Datenanalyst (m/w/d)")  # higher match score
    primary = _seed_job(db, tier=TIER_0, title="Junior Python Developer (m/w/d)")
    by_id = {
        adjacent.id: _assessment(title=adjacent.title, role="OTHER"),
        primary.id: _assessment(title=primary.title, role="PRIMARY"),
    }
    # the REAL check runs: assessment -> block reason and category rank (no stubbed ranks)
    monkeypatch.setattr(shortlist_gate, "_assess_isolated", lambda d, job_id: by_id[job_id])
    result = asyncio.run(
        prepare_shortlist_drafts(
            db,
            touched_jobs=[_touch(adjacent), _touch(primary)],
            settings=_settings(automation_shortlist_max_per_run=2),
        )
    )
    items = {item["job_id"]: item for item in result["items"]}
    # precondition: score-only ordering would have put the adjacent role first
    assert items[adjacent.id]["match_score"] > items[primary.id]["match_score"]
    assert [item["job_id"] for item in result["items"]] == [primary.id, adjacent.id]
    # drafts only: no application row is created or changed by the shortlist step
    assert db.scalar(select(func.count()).select_from(ApplicationRecord)) == 0


def test_check_fails_closed_and_neutral_when_the_assessment_is_unavailable(db):
    from app.services.triage_category import UNKNOWN_RANK

    check = automatic_preparation_check(db, 999999)  # no such job
    assert check.block == "ASSESSMENT_UNAVAILABLE"
    assert check.rank == UNKNOWN_RANK


# --- skill provenance: learned is not commercial -------------------------------------------


def test_course_skills_cover_the_skill_but_never_count_as_professional_years():
    """AWS/Terraform learned in a course can match a junior posting's skill list, but a stated
    multi-year professional requirement is MISSING without dated, relevant work experience."""
    from datetime import UTC, date, datetime

    from app.agents.candidate_job_matcher import JobMatchInput, compute_match
    from app.models.candidate_profile import (
        CandidateJobPreferences,
        CandidateProfile,
        CandidateProject,
        CandidateSkill,
    )

    profile = CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
        skills=[
            CandidateSkill(id=1, name="AWS", notes="Learning/project experience only"),
            CandidateSkill(id=2, name="Terraform", notes="Learning/project experience only"),
        ],
        projects=[CandidateProject(id=1, name="Course lab", technologies=["AWS", "Terraform"])],
    )
    job = JobMatchInput(
        job_id=1,
        title="Cloud Engineer",
        description="Ihr Profil:\n- Mindestens 3 Jahre Berufserfahrung mit AWS und Terraform",
        must_have_skills=["aws", "terraform"],
        nice_to_have_skills=[],
    )
    result = compute_match(job, profile, company_research_id=None, as_of=date(2026, 11, 7))
    by_req = {
        r.normalized_requirement: r
        for r in (
            *result.matched_requirements,
            *result.partial_requirements,
            *result.missing_requirements,
            *result.unknown_requirements,
        )
    }
    years = [r for key, r in by_req.items() if key.startswith("experience-")]
    assert years, "the multi-year requirement must be extracted"
    assert all(r.match_status == "MISSING" for r in years)
    assert result.experience_support_score == 0


# --- the professional direction comes from the title, not from configured priorities ---------


def _from_real_role_criterion(title, *, primary, secondary):
    from app.models.candidate_search import CandidateSearchSettings
    from app.services.job_fit_assessment import role_criterion

    settings = CandidateSearchSettings(primary_roles=primary, secondary_roles=secondary)
    a = _assessment(title=title)
    a.criteria = [c for c in a.criteria if c.name != "role"] + [role_criterion(title, settings)]
    return a


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Junior Python Entwickler (m/w/d)", "PRIMARY"),
        ("Backend Entwickler (m/w/d)", "PRIMARY"),
        ("FastAPI Developer", "PRIMARY"),
        ("DevOps Engineer (m/w/d)", "ADJACENT"),
        ("Datenanalyst (m/w/d)", "ADJACENT"),
    ],
)
@pytest.mark.parametrize(
    "primary,secondary",
    [
        (["Junior Python Developer"], ["DevOps Engineer", "Datenanalyst"]),  # adjacent configured
        ([], []),  # nothing configured
        (["DevOps Engineer", "Datenanalyst"], []),  # adjacent configured as the priority
    ],
)
def test_direction_follows_the_title_whatever_roles_are_configured(
    title, expected, primary, secondary
):
    a = _from_real_role_criterion(title, primary=primary, secondary=secondary)
    assert classify(a).category == expected


def test_developer_title_with_weak_skills_and_a_gap_stays_a_manual_review():
    a = _assessment(title="Softwareentwickler (m/w/d)", role="OTHER", skills="WEAK", language="GAP")
    assert a.fit_tier == "STRETCH"
    assert classify(a).category == "MANUAL_REVIEW"


@pytest.mark.parametrize(
    "title", ["Linux Admin Python", "Database Admin Python (m/w/d)", "Windows Admin / Powershell"]
)
def test_explicit_admin_titles_are_excluded_before_role_priorities(title):
    a = _from_real_role_criterion(title, primary=[], secondary=["Python"])
    assert a.criterion("role").status == "OFF_ROLE"
    assert classify(a).category == "EXCLUDED"
    assert automatic_block_reason(a) == "FIT_TIER_NOT_SUITABLE"
