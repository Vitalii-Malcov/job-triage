"""Stage 14B bare experience with a technology complement (matcher v12).

Codex v11 P1: "Erfahrung mit Python", "Knowledge and experience with
Python" and "Kenntnisse und Erfahrung in Python" with a trusted Python skill
and no experience or project scored overall 80 (shortlist threshold) --
bare experience WITH a technology folded into that technology's SKILL
requirement. A shared complement shares the subject, not the requirement:
knowledge != experience.

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


PYTHON_ONLY = _skills_only("Python")


def _match(description, profile=PYTHON_ONLY):
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


CODEX_REPRODUCTIONS = [
    ("Erfahrung mit Python", "Erfahrung mit Python"),
    ("Knowledge and experience with Python", "experience with Python"),
    ("Kenntnisse und Erfahrung in Python", "Erfahrung in Python"),
]


@pytest.mark.parametrize(("line", "experience"), CODEX_REPRODUCTIONS)
def test_codex_reproduction_keeps_experience_with_python(line, experience):
    result = _match(_profil(line))
    reqs = _by_norm(result)
    assert set(reqs) == {"python", experience.casefold()}
    assert reqs["python"].match_status == "MATCH"
    row = reqs[experience.casefold()]
    assert (row.requirement, row.requirement_type) == (experience, "OTHER")
    assert (row.importance, row.match_status) == ("REQUIRED", "UNKNOWN")
    assert "HUMAN_REVIEW_REQUIRED" in row.reason
    assert "python" in row.normalized_requirement  # technology-specific identity
    assert result.overall_score == 50  # v11: 80
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    ("line", "experience"),
    [
        ("Erfahrung in Python", "Erfahrung in Python"),
        ("experience with Python", "experience with Python"),
        ("experience in Python", "experience in Python"),
        ("experience using Python", "experience using Python"),
        ("Skills and experience with Python", "experience with Python"),
        ("Python knowledge and experience", "Python knowledge and experience"),
        ("Erfahrung und Kenntnisse in Python", "Erfahrung und Kenntnisse in Python"),
        ("Knowledge and practical experience with Python", "practical experience with Python"),
    ],
)
def test_experience_with_a_technology_is_its_own_requirement(line, experience):
    result = _match(_profil(line))
    reqs = _by_norm(result)
    assert set(reqs) == {"python", experience.casefold()}
    assert reqs[experience.casefold()].match_status == "UNKNOWN"
    assert result.overall_score < _threshold()


def test_knowledge_alone_still_folds_into_the_skill():
    result = _match(_profil("Kenntnisse in Python"))
    assert set(_by_norm(result)) == {"python"}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Kenntnisse in Python, Erfahrung", {"python", "erfahrung"}),
        ("Python; experience", {"python", "experience"}),
        ("Kenntnisse in Java; Erfahrung mit Python", {"java", "python", "erfahrung mit python"}),
    ],
)
def test_technology_subject_stays_clause_local(line, expected):
    assert set(_by_norm(_match(_profil(line), _skills_only("Python", "Java")))) == expected


@pytest.mark.parametrize(
    "line",
    [
        "3 Jahre Erfahrung mit Python",
        "3 years Python experience",
        "3 years of Python experience",
        "Python-Erfahrung von mindestens 3 Jahren",
    ],
)
def test_a_typed_duration_owns_the_phrase_without_a_duplicate(line):
    reqs = _by_norm(_match(_profil(line)))
    assert set(reqs) == {"python", "experience-3y"}


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        # Codex P2-A / P2-B, deliberately unchanged in v12
        ("Python and Java experience", "java experience"),
        ("3 Jahre Python-Erfahrung", "3 jahre python-erfahrung"),
    ],
)
def test_known_p2_representations_are_unchanged(line, kept):
    reqs = _by_norm(_match(_profil(line), _skills_only("Python", "Java")))
    assert reqs[kept].requirement_type == "OTHER"


# --- v12 cache identity ----------------------------------------------------------------------


def test_v11_row_is_kept_and_cache_lookup_uses_v12():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v12",
            source="bundesagentur",
            title="Developer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("Erfahrung mit Python"),
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
        v11_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v11",
            overall_score=80,
            coverage_score=100,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=0,
        )
        create_match(db, v11_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert matcher.ALGORITHM_VERSION == "v12"
        assert result.algorithm_version == "v12"
        assert "Erfahrung mit Python" in [r.requirement for r in result.unknown_requirements]
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v11", "v12"]
