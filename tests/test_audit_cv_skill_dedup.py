"""Audit FLOW-12: a skill matched by two requirements appears once in the CV."""

from types import SimpleNamespace

from app.agents.cv_adapter import _select_skills


def _req(text, importance, skill_id, status="MATCH", kind="SKILL"):
    return SimpleNamespace(
        requirement=text,
        requirement_type=kind,
        match_status=status,
        importance=importance,
        candidate_evidence=[SimpleNamespace(entity_id=skill_id)] if skill_id is not None else [],
    )


def _profile(*ids_and_names):
    return SimpleNamespace(
        skills=[
            SimpleNamespace(
                id=i, name=name, category="backend", proficiency="ADVANCED", years_experience=3
            )
            for i, name in ids_and_names
        ]
    )


def test_a_skill_named_individually_and_by_category_is_listed_once():
    match = SimpleNamespace(
        matched_requirements=[
            _req("django", "REQUIRED", 1),
            _req("sql", "REQUIRED", 3),
            _req("Python-Frameworks (examples: Django, Flask)", "REQUIRED", 1),
        ]
    )
    items = _select_skills(match, _profile((1, "Django"), (3, "SQL")))
    assert [item.source_id for item in items] == [1, 3]
    assert items[0].match_requirement == "django"  # the first (strongest) requirement wins


def test_the_required_requirement_beats_a_preferred_duplicate():
    match = SimpleNamespace(
        matched_requirements=[
            _req("Python-Frameworks (examples: Django)", "PREFERRED", 1),
            _req("django", "REQUIRED", 1),
        ]
    )
    (item,) = _select_skills(match, _profile((1, "Django")))
    assert item.match_requirement == "django" and item.importance == "REQUIRED"


def test_distinct_skills_and_non_matches_are_untouched():
    match = SimpleNamespace(
        matched_requirements=[
            _req("python", "REQUIRED", 1),
            _req("sql", "REQUIRED", 2),
            _req("go", "REQUIRED", None, status="MISSING"),
            _req("5 years", "REQUIRED", None, kind="EXPERIENCE"),
        ]
    )
    items = _select_skills(match, _profile((1, "Python"), (2, "SQL")))
    assert [item.source_id for item in items] == [1, 2]
