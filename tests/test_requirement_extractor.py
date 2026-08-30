import pytest

from app.agents.requirement_extractor import (
    canonicalize_language_name,
    extract_education_requirement,
    extract_language_requirements,
)

# --- language extraction -----------------------------------------------


def test_language_required_context_is_classified_required():
    reqs = extract_language_requirements(None, "Voraussetzung: Deutsch B2.")
    assert len(reqs) == 1
    assert reqs[0].language == "German"
    assert reqs[0].level == "B2"
    assert reqs[0].importance == "REQUIRED"


def test_language_nice_context_is_classified_preferred():
    reqs = extract_language_requirements(None, "Englisch B1 ist von Vorteil.")
    assert len(reqs) == 1
    assert reqs[0].language == "English"
    assert reqs[0].level == "B1"
    assert reqs[0].importance == "PREFERRED"


def test_language_with_no_must_nice_marker_is_unknown_importance():
    reqs = extract_language_requirements(None, "German B2 for this role.")
    assert len(reqs) == 1
    assert reqs[0].importance == "UNKNOWN"


def test_language_english_recognized():
    reqs = extract_language_requirements(None, "English C1 required.")
    assert len(reqs) == 1
    assert reqs[0].language == "English"
    assert reqs[0].level == "C1"


def test_native_speaker_maps_to_native_level():
    reqs = extract_language_requirements(None, "German native speaker required.")
    assert len(reqs) == 1
    assert reqs[0].level == "NATIVE"
    assert reqs[0].importance == "REQUIRED"


def test_muttersprache_maps_to_native_level():
    reqs = extract_language_requirements(None, "Deutsch als Muttersprache erforderlich.")
    assert len(reqs) == 1
    assert reqs[0].language == "German"
    assert reqs[0].level == "NATIVE"


def test_segment_with_two_languages_is_skipped_to_avoid_misattribution():
    reqs = extract_language_requirements(None, "German B2 and English B2 needed.")
    assert reqs == []


def test_segment_with_two_levels_is_skipped():
    reqs = extract_language_requirements(None, "German B1 or B2 accepted.")
    assert reqs == []


def test_no_language_mention_returns_empty_list():
    reqs = extract_language_requirements("Python Developer", "We use Python and Docker.")
    assert reqs == []


# --- M-01: negated language requirements produce no requirement at all ----


@pytest.mark.parametrize(
    "text",
    [
        "No German B2 required.",
        "German B2 is not required.",
        "Deutsch B2 nicht erforderlich.",
        "Keine Deutschkenntnisse erforderlich.",
        "No English B2 required.",
        "English B2 is not required.",
        "Englisch B2 nicht erforderlich.",
        "Keine Englischkenntnisse erforderlich.",
        "No English required.",
        "English is not required.",
    ],
)
def test_negated_language_requirement_produces_no_requirement(text):
    assert extract_language_requirements(None, text) == []


@pytest.mark.parametrize(
    "text",
    [
        "German is not preferred.",
        "German is not necessary.",
        "Deutsch ist nicht erforderlich.",
        "Deutsch ist nicht notwendig.",
        # Level-bearing variant: same negation words, but with an explicit
        # CEFR level present, exercising the negation check itself rather
        # than the (separate, already-existing) "no level found" guard.
        "German B2 is not preferred.",
    ],
)
def test_negated_preferred_or_necessary_wording_produces_no_requirement(text):
    assert extract_language_requirements(None, text) == []


@pytest.mark.parametrize(
    ("text", "language", "level", "importance"),
    [
        ("German B2 required.", "German", "B2", "REQUIRED"),
        ("German B2 is required.", "German", "B2", "REQUIRED"),
        ("Deutsch B2 erforderlich.", "German", "B2", "REQUIRED"),
        ("Deutschkenntnisse B2 erforderlich.", "German", "B2", "REQUIRED"),
        ("Deutsch B2 wird vorausgesetzt.", "German", "B2", "REQUIRED"),
        ("English B1 required.", "English", "B1", "REQUIRED"),
        ("Englisch B1 erforderlich.", "English", "B1", "REQUIRED"),
        ("German B2 preferred.", "German", "B2", "PREFERRED"),
        ("German B2 nice to have.", "German", "B2", "PREFERRED"),
        ("Deutsch B2 von Vorteil.", "German", "B2", "PREFERRED"),
        ("English B1 preferred.", "English", "B1", "PREFERRED"),
        ("Englisch B1 von Vorteil.", "English", "B1", "PREFERRED"),
    ],
)
def test_positive_and_preferred_wording_still_classified_correctly(
    text, language, level, importance
):
    """M-01 regression guard: the negation fix must not affect any
    non-negated required/preferred phrasing that already worked."""
    reqs = extract_language_requirements(None, text)
    assert len(reqs) == 1
    assert reqs[0].language == language
    assert reqs[0].level == level
    assert reqs[0].importance == importance


def test_negation_in_one_sentence_does_not_suppress_a_later_sentence():
    reqs = extract_language_requirements(None, "German is not required. English B2 is required.")
    assert len(reqs) == 1
    assert reqs[0].language == "English"
    assert reqs[0].level == "B2"
    assert reqs[0].importance == "REQUIRED"


def test_negation_in_german_sentence_does_not_suppress_english_sentence():
    reqs = extract_language_requirements(
        None, "Deutsch ist nicht erforderlich. Englisch B2 ist erforderlich."
    )
    assert len(reqs) == 1
    assert reqs[0].language == "English"
    assert reqs[0].level == "B2"


def test_negated_language_in_same_segment_does_not_suppress_other_language():
    """Section 10: a negated mention must not cause the *whole segment* to
    be discarded when it also contains a genuine, unrelated requirement
    for a different language — only German is ignored here.
    """
    reqs = extract_language_requirements(
        None, "German is not required, but English B2 is required."
    )
    assert len(reqs) == 1
    assert reqs[0].language == "English"
    assert reqs[0].level == "B2"
    assert reqs[0].importance == "REQUIRED"


def test_negated_language_with_level_in_same_segment_does_not_suppress_other_language():
    reqs = extract_language_requirements(None, "No German B2 required, but English B2 is required.")
    assert len(reqs) == 1
    assert reqs[0].language == "English"
    assert reqs[0].importance == "REQUIRED"


def test_multiple_segments_each_produce_their_own_requirement():
    description = "Dein Profil:\nDeutsch B2 ist erforderlich.\nEnglisch B1 ist wünschenswert."
    reqs = extract_language_requirements(None, description)
    languages = {(r.language, r.level, r.importance) for r in reqs}
    assert ("German", "B2", "REQUIRED") in languages
    assert ("English", "B1", "PREFERRED") in languages


def test_canonicalize_language_name_recognizes_german_variants():
    assert canonicalize_language_name("Deutsch") == "German"
    assert canonicalize_language_name("German (business)") == "German"


def test_canonicalize_language_name_recognizes_english_variants():
    assert canonicalize_language_name("English") == "English"
    assert canonicalize_language_name("Englisch") == "English"


def test_canonicalize_language_name_returns_none_for_unrecognized_language():
    assert canonicalize_language_name("Klingon") is None


# --- education extraction -----------------------------------------------


def test_education_bachelor_required_context():
    req = extract_education_requirement(None, "Ein abgeschlossenes Studium ist erforderlich.")
    assert req is not None
    assert req.importance == "REQUIRED"


def test_education_completed_degree_english_phrase():
    req = extract_education_requirement(None, "A completed degree is required for this role.")
    assert req is not None
    assert req.importance == "REQUIRED"


def test_education_nice_to_have_context():
    req = extract_education_requirement(None, "A university degree is a plus.")
    assert req is not None
    assert req.importance == "PREFERRED"


def test_education_no_mention_returns_none():
    req = extract_education_requirement("Python Developer", "We use Python and Docker.")
    assert req is None


def test_education_first_matching_segment_wins():
    description = "Bachelor required. Master ist von Vorteil."
    req = extract_education_requirement(None, description)
    assert req is not None
    assert req.importance == "REQUIRED"
    assert "Bachelor" in req.evidence_text
