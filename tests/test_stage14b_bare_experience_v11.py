"""Stage 14B bare technology experience (matcher v11).

Codex v10 P1: "Python experience" with a trusted Python skill and no
experience or project scored overall 80 (shortlist threshold) -- masking
the Python SKILL span left a bare "experience" head, which was dropped.

Module attributes are used (not `from ... import`) so this file also runs
against the previous implementation and fails per test, not at collection.
"""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.agents.candidate_job_matcher as matcher
from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint, create_match
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import CandidateJobMatchRecord, JobRecord
from app.models.candidate_job_match import CandidateJobMatchData
from app.models.candidate_profile import (
    CandidateJobPreferences,
    CandidateProfile,
    CandidateSkill,
)
from app.services.candidate_preparation import prepare_candidate_job_match

AS_OF = date(2026, 10, 15)


def _skills_only(*names):
    """Trusted skills only: no experience rows, no projects."""
    return CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
        skills=[CandidateSkill(id=i, name=name) for i, name in enumerate(names, 1)],
    )


ALL_SKILLS = _skills_only("Python", "Java", "Docker", "Kubernetes", "Django")


def _match(description, profile=ALL_SKILLS):
    job = matcher.JobMatchInput(job_id=1, title="Developer", description=description)
    return matcher.compute_match(job, profile, company_research_id=None, as_of=AS_OF)


def _by_norm(result):
    return {
        r.normalized_requirement: r
        for r in [
            *result.matched_requirements,
            *result.partial_requirements,
            *result.missing_requirements,
            *result.unknown_requirements,
        ]
    }


def _profil(*lines: str) -> str:
    return "Profil:\n" + "\n".join(f"- {line}" for line in lines)


def _threshold() -> int:
    return Settings.model_fields["automation_shortlist_min_match_score"].default


def test_codex_reproduction_python_experience_is_not_a_bare_skill_match():
    result = _match(_profil("Python experience"), _skills_only("Python"))
    reqs = _by_norm(result)
    assert set(reqs) == {"python", "python experience"}
    assert reqs["python"].match_status == "MATCH"
    row = reqs["python experience"]
    assert (row.requirement, row.requirement_type) == ("Python experience", "OTHER")
    assert (row.importance, row.match_status) == ("REQUIRED", "UNKNOWN")
    assert "HUMAN_REVIEW_REQUIRED" in row.reason
    assert result.overall_score < _threshold()  # v10: 80


@pytest.mark.parametrize(
    ("line", "skill"),
    [
        ("Java experience", "java"),
        ("Docker experience", "docker"),
        ("Kubernetes experience", "kubernetes"),
        ("Django experience", "django"),
    ],
)
def test_bare_technology_experience_keeps_its_requirement(line, skill):
    result = _match(_profil(line))
    reqs = _by_norm(result)
    assert set(reqs) == {skill, line.casefold()}
    assert reqs[line.casefold()].match_status == "UNKNOWN"
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Kenntnisse in Python, experience", {"python", "experience"}),
        ("Python; experience", {"python", "experience"}),
        ("Kenntnisse in Java; Python experience", {"java", "python", "python experience"}),
    ],
)
def test_technology_subject_stays_clause_local(line, expected):
    assert set(_by_norm(_match(_profil(line)))) == expected


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("professional Python experience", "professional python experience"),
        ("practical Python experience", "practical python experience"),
        ("strong Python experience", "strong python experience"),
        ("Python professional experience", "python professional experience"),
        ("Python practical experience", "python practical experience"),
        ("Python- und Docker-Erfahrung", "python- und docker-erfahrung"),
        ("Python-, Java- und Docker-Erfahrung", "python-, java- und docker-erfahrung"),
        ("gute Python-Erfahrung", "gute python-erfahrung"),
    ],
)
def test_v10_subject_preservation_is_unchanged(line, kept):
    reqs = _by_norm(_match(_profil(line)))
    assert kept in reqs
    assert [r.requirement_type for r in reqs.values()].count("OTHER") == 1


@pytest.mark.parametrize(
    "line",
    [
        "3 years Python experience",
        "3 years of Python experience",
        "3+ years of professional Python experience",
        "Python-Erfahrung von mindestens 3 Jahren",
    ],
)
def test_a_typed_duration_owns_the_phrase_without_a_duplicate(line):
    result = _match(_profil(line), _skills_only("Python"))
    reqs = _by_norm(result)
    assert set(reqs) == {"python", "experience-3y"}
    assert reqs["experience-3y"].match_status == "MISSING"
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("experience", "experience"),
        ("professional experience", "professional experience"),
        ("practical experience", "practical experience"),
    ],
)
def test_generic_experience_gets_no_technology_subject(line, kept):
    assert set(_by_norm(_match(_profil(line)))) == {kept}


# --- v11 cache identity ----------------------------------------------------------------------


def test_v10_row_is_kept_and_cache_lookup_uses_the_current_version():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v11",
            source="bundesagentur",
            title="Developer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("Python experience"),
            skills_json="[]",
            data_confidence=0.9,
            must_have_skills_json=json.dumps(["python"]),
            nice_to_have_skills_json="[]",
            score=80,
            recommendation="APPLY",
            status="NEW",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(job)
        db.commit()
        profile_version = get_or_create_candidate_profile(db).profile_version
        v10_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v10",
            overall_score=80,
            coverage_score=100,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=0,
        )
        create_match(db, v10_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert result.algorithm_version == matcher.ALGORITHM_VERSION
        assert "Python experience" in [r.requirement for r in result.unknown_requirements]
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v10", matcher.ALGORITHM_VERSION]
