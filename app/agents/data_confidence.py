from collections.abc import Collection

# Calibration constants, not universal truths. Revisit these thresholds after
# 100-200 real vacancies have both extracted data and a manual sufficiency
# assessment; until then they intentionally remain simple and auditable.
RICH_DESCRIPTION_LENGTH = 2_000
MEDIUM_DESCRIPTION_LENGTH = 500

EMPTY_DESCRIPTION_BASE = 0.10
SHORT_DESCRIPTION_BASE = 0.30
MEDIUM_DESCRIPTION_BASE = 0.55
RICH_DESCRIPTION_BASE = 0.75

NO_SKILLS_MODIFIER = -0.20
THREE_TO_FOUR_SKILLS_MODIFIER = 0.10
FIVE_OR_MORE_SKILLS_MODIFIER = 0.18

# 1.0 is kept as a theoretical ideal for evidence stronger than the current
# heuristic can establish. Description length plus keyword matches alone may
# be excellent, but should not claim absolute certainty.
MAX_HEURISTIC_CONFIDENCE = 0.97


def calculate_data_confidence(
    description: str | None,
    skills: Collection[str],
) -> float:
    """Return evidence sufficiency from description content and found skills.

    Provenance is deliberately absent from this formula. A literal technology
    mention in free text is evidence regardless of which collector supplied
    that text; only the amount of usable evidence affects confidence.
    """
    description_length = len((description or "").strip())
    if description_length == 0:
        description_base = EMPTY_DESCRIPTION_BASE
    elif description_length < MEDIUM_DESCRIPTION_LENGTH:
        description_base = SHORT_DESCRIPTION_BASE
    elif description_length < RICH_DESCRIPTION_LENGTH:
        description_base = MEDIUM_DESCRIPTION_BASE
    else:
        description_base = RICH_DESCRIPTION_BASE

    skill_count = len({skill.strip().casefold() for skill in skills if skill.strip()})
    if skill_count == 0:
        skills_modifier = NO_SKILLS_MODIFIER
    elif skill_count < 3:
        skills_modifier = 0.0
    elif skill_count < 5:
        skills_modifier = THREE_TO_FOUR_SKILLS_MODIFIER
    else:
        skills_modifier = FIVE_OR_MORE_SKILLS_MODIFIER

    confidence = description_base + skills_modifier
    return round(max(0.0, min(confidence, MAX_HEURISTIC_CONFIDENCE)), 2)
