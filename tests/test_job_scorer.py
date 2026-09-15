from app.agents.job_scorer import JobScorer
from app.models.job import Job


def make_job(**overrides):
    data = {
        "source": "test",
        "title": "Junior Fast API Python Developer",
        "company": "Example GmbH",
        "url": "https://example.com/job/1",
        "description": ("We build APIs with Python and FastAPI. " * 15),
    }
    data.update(overrides)
    return Job(**data)


def test_aliases_and_must_have_weighting():
    scorer = JobScorer({"python", "fastapi", "git"})
    result = scorer.score(
        make_job(
            must_have_skills=["Python", "Fast API"],
            nice_to_have_skills=["Docker", "Git"],
        )
    )
    assert result.recommendation == "APPLY"
    assert "fastapi" in result.matched_must_have
    assert "docker" in result.missing_skills


def test_missing_majority_of_must_have_is_skip():
    scorer = JobScorer({"python"})
    result = scorer.score(make_job(must_have_skills=["Python", "FastAPI", "Docker", "AWS"]))
    assert result.recommendation == "SKIP"


def test_low_data_confidence_overrides_a_high_numeric_score():
    scorer = JobScorer({"python"})
    result = scorer.score(make_job(description="", must_have_skills=["Python"]))

    assert result.score >= 80
    assert result.data_confidence == 0.1
    assert result.recommendation == "NEEDS_ENRICHMENT"


def test_single_must_have_match_cannot_reach_apply_even_with_rich_description():
    # Stage 10 finding: alfatraining's "Programmierung mit Python" course
    # listing scored 91/APPLY purely because its extracted must-have set
    # was a single skill ("python"), trivially matched -- combined with a
    # long, skill-keyword-dense description (a real course syllabus, not
    # a requirements list) that kept data_confidence high. A must-have set
    # this sparse must never support an automatic APPLY, no matter how
    # rich the surrounding text is.
    scorer = JobScorer({"python", "fastapi", "flask", "sqlalchemy", "postgresql", "docker"})
    description = (
        "In diesem Kurs behandeln wir Python, FastAPI, Flask, SQLAlchemy, "
        "PostgreSQL und Docker im Detail. " * 20
    )
    result = scorer.score(
        make_job(
            title="Programmierung mit Python",
            description=description,
            must_have_skills=["Python"],
            nice_to_have_skills=[],
        )
    )
    assert result.score >= 80
    assert result.recommendation != "APPLY"


def test_empty_nice_to_have_set_is_not_treated_as_fully_satisfied():
    # Stage 10 finding: nice_score used to default to 1.0 (full credit)
    # for an EMPTY nice-to-have set -- absence of evidence is not proof of
    # satisfaction. A job with a borderline must-coverage and zero
    # extracted nice-to-haves should score lower than one with the exact
    # same must-coverage plus genuinely matched nice-to-haves.
    scorer = JobScorer({"python", "fastapi", "docker"})
    with_nice = scorer.score(
        make_job(
            must_have_skills=["Python", "FastAPI"],
            nice_to_have_skills=["Docker"],
        )
    )
    without_nice = scorer.score(
        make_job(
            must_have_skills=["Python", "FastAPI"],
            nice_to_have_skills=[],
        )
    )
    assert without_nice.score < with_nice.score


def test_legitimate_junior_python_backend_developer_still_scores_apply():
    # A real job with a normal (non-sparse) extracted must-have set must
    # be completely unaffected by the Stage 10 sparse-evidence fixes.
    scorer = JobScorer(
        {"python", "fastapi", "sqlalchemy", "postgresql", "git", "pytest", "docker", "rest-api"}
    )
    result = scorer.score(
        make_job(
            title="Junior Python Backend Developer",
            description=(
                "We are looking for a Junior Python Backend Developer to build "
                "REST APIs with FastAPI and SQLAlchemy against PostgreSQL. " * 10
            ),
            must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
            nice_to_have_skills=["Docker", "Pytest"],
        )
    )
    assert result.recommendation == "APPLY"
