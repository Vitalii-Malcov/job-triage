"""Stage 14B experience-subject preservation (matcher v10).

Masking an evaluated SKILL span must not remove that technology from the
EXPERIENCE requirement the same words state: v9 reduced "Python- und
Docker-Erfahrung" to "Docker-Erfahrung" and "professional Python
experience" to "professional experience".

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
    CandidateExperience,
    CandidateJobPreferences,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
)
from app.services.candidate_preparation import prepare_candidate_job_match

AS_OF = date(2026, 10, 15)


def _profile(skills=(), experiences=(), projects=()):
    return CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
        skills=[CandidateSkill(id=i, name=name) for i, name in enumerate(skills, 1)],
        experiences=list(experiences),
        projects=list(projects),
    )


def _work(technologies, start=date(2018, 1, 1)):
    return CandidateExperience(
        id=1,
        company="Company",
        job_title="Developer",
        start_date=start,
        is_current=True,
        technologies=list(technologies),
    )


def _match(description, profile):
    job = matcher.JobMatchInput(job_id=1, title="Developer", description=description)
    return matcher.compute_match(job, profile, company_research_id=None, as_of=AS_OF)


def _all(result):
    return [
        *result.matched_requirements,
        *result.partial_requirements,
        *result.missing_requirements,
        *result.unknown_requirements,
    ]


def _by_norm(result):
    return {r.normalized_requirement: r for r in _all(result)}


def _profil(*lines: str) -> str:
    return "Profil:\n" + "\n".join(f"- {line}" for line in lines)


def _threshold() -> int:
    return Settings.model_fields["automation_shortlist_min_match_score"].default


# Candidate: every skill trusted, a Python project, NO professional work.
SKILLS_AND_PROJECT = _profile(
    ["Python", "Docker", "Kubernetes", "Java"],
    projects=[CandidateProject(id=1, name="Tool", technologies=["Python"])],
)

CASES = [
    # line, skills evaluated, the experience requirement (whole phrase)
    ("Python-Erfahrung", {"python"}, "Python-Erfahrung"),
    ("gute Python-Erfahrung", {"python"}, "gute Python-Erfahrung"),
    ("Python- und Docker-Erfahrung", {"python", "docker"}, "Python- und Docker-Erfahrung"),
    ("Python- und Kubernetes-Erfahrung", {"python", "kubernetes"},
     "Python- und Kubernetes-Erfahrung"),
    ("Python-, Java- und Docker-Erfahrung", {"python", "java", "docker"},
     "Python-, Java- und Docker-Erfahrung"),
    ("gute Python- und Docker-Erfahrung", {"python", "docker"},
     "gute Python- und Docker-Erfahrung"),
    ("professional Python experience", {"python"}, "professional Python experience"),
    ("practical Python experience", {"python"}, "practical Python experience"),
    ("strong Python experience", {"python"}, "strong Python experience"),
    ("solid Python experience", {"python"}, "solid Python experience"),
    ("extensive Python experience", {"python"}, "extensive Python experience"),
    ("blorpy Python experience", {"python"}, "blorpy Python experience"),  # no list has it
    ("Python professional experience", {"python"}, "Python professional experience"),
    ("Python practical experience", {"python"}, "Python practical experience"),
    ("3 years professional Python experience", {"python"},
     "3 years professional Python experience"),
    ("Kenntnisse und gute Python-Erfahrung", {"python"}, "gute Python-Erfahrung"),
    ("Kenntnisse in Java und professional Docker experience", {"java", "docker"},
     "professional Docker experience"),
]  # fmt: skip


@pytest.mark.parametrize(("line", "skills", "experience"), CASES)
def test_experience_requirement_keeps_its_technology_subjects(line, skills, experience):
    result = _match(_profil(line), SKILLS_AND_PROJECT)
    reqs = _by_norm(result)
    # complete requirement set: the skills + ONE experience requirement
    assert set(reqs) == skills | {experience.casefold()}
    assert all(reqs[s].match_status == "MATCH" for s in skills)
    row = reqs[experience.casefold()]
    assert (row.requirement, row.requirement_type) == (experience, "OTHER")
    assert (row.importance, row.match_status) == ("REQUIRED", "UNKNOWN")
    assert "HUMAN_REVIEW_REQUIRED" in row.reason
    # every technology subject is in the experience requirement's identity
    # ("Kenntnisse in Java und ...": Java belongs to the other clause)
    subjects = {"docker"} if line.startswith("Kenntnisse in Java") else skills
    for skill in subjects:
        assert skill in row.normalized_requirement
    assert result.overall_score < _threshold()


def test_generic_professional_work_never_establishes_python_experience():
    """3 years of non-Python professional work + a Python project."""
    profile = _profile(
        ["Python", "Java"],
        [_work(["Java"], start=date(2022, 1, 1))],
        [CandidateProject(id=1, name="Tool", technologies=["Python"])],
    )
    result = _match(_profil("professional Python experience"), profile)
    row = _by_norm(result)["professional python experience"]
    assert row.match_status == "UNKNOWN"
    assert "professional experience" not in _by_norm(result)
    assert result.overall_score < _threshold()


def test_docker_only_work_never_establishes_python_and_docker_experience():
    profile = _profile(["Python", "Docker"], [_work(["Docker"])])
    result = _match(_profil("Python- und Docker-Erfahrung"), profile)
    reqs = _by_norm(result)
    assert reqs["python- und docker-erfahrung"].match_status == "UNKNOWN"
    assert "docker-erfahrung" not in reqs
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    "line",
    ["Python-Erfahrung von mindestens 3 Jahren", "Mindestens 3 Jahre Berufserfahrung mit Python"],
)
def test_a_typed_duration_still_owns_the_phrase_without_a_duplicate(line):
    reqs = _by_norm(_match(_profil(line), SKILLS_AND_PROJECT))
    assert set(reqs) == {"python", "experience-3y"}


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        # a separate clause: no technology is its subject
        ("Kenntnisse in Python, professional experience", "professional experience"),
        ("Kenntnisse in Python und Berufserfahrung", "Berufserfahrung"),
    ],
)
def test_technologies_of_another_clause_are_not_attached(line, kept):
    reqs = _by_norm(_match(_profil(line), SKILLS_AND_PROJECT))
    assert set(reqs) == {"python", kept.casefold()}


@pytest.mark.parametrize(
    "line",
    [
        "Kenntnisse in Python- und Docker-Umgebungen",  # an ellipsis without experience
        "Kenntnisse und Erfahrung in Python",  # bare shared complement
    ],
)
def test_no_experience_row_without_qualified_experience(line):
    reqs = _by_norm(_match(_profil(line), SKILLS_AND_PROJECT))
    assert all(r.requirement_type == "SKILL" for r in reqs.values())


# --- v10 cache identity ----------------------------------------------------------------------


def test_v9_row_is_kept_and_cache_lookup_uses_v10():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v10",
            source="bundesagentur",
            title="Developer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("professional Python experience"),
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
        v9_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v9",
            overall_score=100,
            coverage_score=100,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=100,
        )
        create_match(db, v9_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert matcher.ALGORITHM_VERSION == "v10"
        assert result.algorithm_version == "v10"
        assert "professional Python experience" in [
            r.requirement for r in result.unknown_requirements
        ]
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v9", "v10"]
