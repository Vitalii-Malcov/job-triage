from app.agents.skill_extractor import extract_skills


def test_extracts_literal_technologies_from_realistic_german_text():
    result = extract_skills(
        "Backend Entwickler",
        "Erfahrung mit Python, Flask und PostgreSQL ist erforderlich. "
        "Docker und Kubernetes sind wünschenswert.",
    )

    assert result.must_have_skills == ["flask", "postgresql", "python"]
    assert result.nice_to_have_skills == ["docker", "kubernetes"]
    assert result.skill_source == "description_extracted"


def test_does_not_infer_related_technologies_that_are_absent():
    result = extract_skills(
        "Junior Python Developer",
        "You will maintain Python services and collaborate with the team.",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == []
    assert "fastapi" not in result.must_have_skills
    assert "docker" not in result.must_have_skills
    assert "postgresql" not in result.must_have_skills


def test_empty_or_none_description_is_safe_and_keeps_extracted_provenance():
    result = extract_skills("Backend Developer", None)

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == []
    assert result.skill_source == "description_extracted"


def test_literal_title_match_is_allowed_without_inferring_adjacent_skills():
    result = extract_skills("Django Developer", "")

    assert result.must_have_skills == ["django"]
    assert result.nice_to_have_skills == []
