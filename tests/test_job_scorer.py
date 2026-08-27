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
