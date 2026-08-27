from app.agents.data_confidence import calculate_data_confidence


def test_rich_description_with_five_skills_has_high_explicit_confidence():
    confidence = calculate_data_confidence("x" * 2_100, ["python", "flask", "sql", "git", "linux"])

    assert confidence == 0.93


def test_rich_description_without_skills_is_reduced_but_not_empty_level():
    confidence = calculate_data_confidence("x" * 2_100, [])

    assert confidence == 0.55


def test_empty_description_without_skills_has_zero_confidence():
    confidence = calculate_data_confidence(None, [])

    assert confidence == 0.0


def test_medium_description_with_three_skills_has_concrete_confidence():
    confidence = calculate_data_confidence("x" * 800, ["python", "flask", "postgresql"])

    assert confidence == 0.65
