"""Night audit 2026-10-27: German requirement provenance in the fit criteria.

"sehr gute Deutschkenntnisse" names no CEFR level and no certificate; it was
reported exactly like a stated "C1 zwingend" (MAJOR_GAP for a B1 candidate).
It is now an INFERRED_REQUIREMENT -> GAP_TO_VERIFY. The opposite mistake is
guarded too: a stated C1 stays a MAJOR_GAP for B1.
"""

import pytest

import app.services.job_fit_assessment as fit
from app.agents.job_fit_signals import entry_level_signals, extract_german_requirement


@pytest.mark.parametrize(
    ("text", "level", "basis"),
    [
        ("Deutsch auf C1-Niveau zwingend erforderlich.", "C1", "EXPLICIT_REQUIREMENT"),
        (
            "Verhandlungssichere Deutschkenntnisse (C2) setzen wir voraus.",
            "C2",
            "EXPLICIT_REQUIREMENT",
        ),
        ("Sehr gute Deutschkenntnisse in Wort und Schrift", "C1", "INFERRED_REQUIREMENT"),
        ("Fließend Deutsch", "C1", "INFERRED_REQUIREMENT"),
        ("Deutschkenntnisse (B2) wünschenswert.", "B2", "PREFERRED_REQUIREMENT"),
        ("Gute Deutschkenntnisse sind von Vorteil", "B2", "PREFERRED_REQUIREMENT"),
        ("Standort in Deutschland, gute Anbindung.", None, "UNKNOWN"),
    ],
)
def test_requirement_basis(text, level, basis):
    requirement = extract_german_requirement(text)
    assert (requirement.level, requirement.basis) == (level, basis)


def test_explicit_level_beats_same_level_inferred():
    requirement = extract_german_requirement(
        "Sehr gute Deutschkenntnisse. Deutsch mindestens C1 (Nachweis erforderlich)."
    )
    assert (requirement.level, requirement.basis) == ("C1", "EXPLICIT_REQUIREMENT")


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Sehr gute Deutschkenntnisse in Wort und Schrift", "GAP_TO_VERIFY"),
        ("Deutsch auf C1-Niveau zwingend erforderlich.", "MAJOR_GAP"),
        ("Verhandlungssichere Deutschkenntnisse (C2)", "MAJOR_GAP"),
        ("Deutschkenntnisse B2", "GAP"),
        ("Gute Deutschkenntnisse sind von Vorteil", "MINOR_GAP"),
        ("Deutschkenntnisse B1", "OK"),
        ("Keine Sprachangabe", "NO_REQUIREMENT"),
    ],
)
def test_language_criterion_for_confirmed_b1(text, status):
    criterion = fit.language_criterion(text, "B1")
    assert criterion.status == status


def test_inferred_reason_says_it_is_inferred():
    reason = fit.language_criterion("Sehr gute Deutschkenntnisse", "B1").reason
    assert "inferred" in reason and "B1" in reason


@pytest.mark.parametrize(
    ("title", "text", "positive"),
    [
        ("Junior Python Developer (m/w/d)", "", True),
        ("Trainee Softwareentwicklung", "", True),
        ("Softwareentwickler (m/w/d)", "Auch Quereinsteiger sind willkommen.", True),
        ("Senior Backend Engineer (m/w/d)", "mind. 5 Jahre Berufserfahrung", False),
    ],
)
def test_entry_level_signals(title, text, positive):
    signals = entry_level_signals(title, text)
    assert bool(signals.positive) is positive


def test_mandatory_level_beats_a_higher_optional_one():
    """Codex round 6: the optional C1 must not hide the mandatory B2."""
    text = "Deutsch B2 zwingend erforderlich. Deutsch C1 wünschenswert."
    requirement = extract_german_requirement(text)
    assert requirement.level == "B2" and requirement.mandatory
    assert requirement.basis == "EXPLICIT_REQUIREMENT"
    assert fit.language_criterion(text, "A2").status == "MAJOR_GAP"


def test_optional_level_still_reported_when_nothing_is_mandatory():
    requirement = extract_german_requirement("Deutsch C1 wünschenswert.")
    assert requirement.level == "C1" and requirement.mandatory is False
    assert fit.language_criterion("Deutsch C1 wünschenswert.", "A2").status == "MINOR_GAP"


@pytest.mark.parametrize(
    ("text", "level", "mandatory"),
    [
        # Codex round 7: the softener and the C1 belong to English.
        ("Deutsch B2 zwingend erforderlich, Englisch C1 wünschenswert.", "B2", True),
        ("Englisch C1 wünschenswert, Deutsch B2 erforderlich.", "B2", True),
        ("Deutsch (B2) und Englisch (C1)", "B2", True),
        ("Gute Englisch- und Deutschkenntnisse wünschenswert.", "B2", False),
        ("Deutschkenntnisse, idealerweise C1, von Vorteil.", "C1", False),
        ("Englisch- und Deutschkenntnisse auf C1-Niveau", "C1", True),
    ],
)
def test_level_and_softener_attach_to_their_own_language(text, level, mandatory):
    requirement = extract_german_requirement(text)
    assert (requirement.level, requirement.mandatory) == (level, mandatory)


def test_mandatory_b2_next_to_optional_english_is_a_major_gap_at_a2():
    text = "Deutsch B2 zwingend erforderlich, Englisch C1 wünschenswert."
    assert fit.language_criterion(text, "A2").status == "MAJOR_GAP"


@pytest.mark.parametrize(
    ("text", "level", "mandatory"),
    [
        # Codex round 8: "und" separates statements, and a stated mandatory
        # minimum is never hidden by a preferred level of the same mention.
        ("Deutsch B2 zwingend erforderlich und Englisch C1 wünschenswert.", "B2", True),
        ("Deutsch C1 wünschenswert, B2 zwingend erforderlich.", "B2", True),
        ("Englisch C1 erforderlich, Deutsch B1 wünschenswert.", "B1", False),
        ("Gute Deutsch- und Englischkenntnisse wünschenswert", "B2", False),
        ("Verhandlungssichere Deutsch- und idealerweise auch Englischkenntnisse", "C1", True),
        ("Deutsch B2 erforderlich, Kenntnisse in SAP von Vorteil.", "B2", True),
        ("Deutsch auf B2-Niveau, von Vorteil.", "B2", False),
        ("German B2 minimum, C1 preferred", "B2", True),
        ("gute Deutschkenntnisse, mindestens auf B2-, idealerweise auf C1-Niveau.", "B2", True),
        # Alternatives and ranges: the lowest level is the minimum.
        ("Sehr gute Deutschkenntnisse in Wort und Schrift (C1/C2)", "C1", True),
        ("Sehr gute Deutsch- und Englischkenntnisse (C2/C1).", "C1", True),
        ("verhandlungssichere Deutschkenntnisse (C1, C2 oder Muttersprache)", "C1", True),
        ("Fließende Deutschkenntnisse (mind. Level B2-C1)", "B2", True),
        # "mind." does not end the statement (real postings).
        ("Sichere Deutschkenntnisse (mind. B2), da Deutsch unsere Arbeitssprache ist", "B2", True),
        ("Deutschkenntnisse auf mind. B2-Niveau", "B2", True),
        ("Sehr gute Englischkenntnisse (mindestens C1) sowie gute Deutschkenntnisse", "B2", True),
    ],
)
def test_each_stated_level_is_judged_in_its_own_statement(text, level, mandatory):
    requirement = extract_german_requirement(text)
    assert (requirement.level, requirement.mandatory) == (level, mandatory)


def test_preferred_c1_beside_mandatory_b2_is_a_major_gap_at_a2():
    text = "Deutsch C1 wünschenswert, B2 zwingend erforderlich."
    assert fit.language_criterion(text, "A2").status == "MAJOR_GAP"


@pytest.mark.parametrize(
    ("text", "level", "mandatory"),
    [
        # Codex round 9: a level stated for both languages joined by "und".
        ("Deutsch und Englisch auf mindestens B2-Niveau erforderlich", "B2", True),
        ("Auf Deutsch und Englisch kommunizierst du jeweils mindestens auf C1-Niveau", "C1", True),
        # ... and a level stated well after the mention.
        (
            "Deutschkenntnisse für die telefonische und schriftliche Kommunikation mit "
            "Kunden auf mindestens B2-Niveau sind erforderlich",
            "B2",
            True,
        ),
        # A comma list keeps separate statements (real postings).
        ("Gute Deutschkenntnisse, Englischkenntnisse sind von Vorteil", "B2", True),
        # A driving licence class is not a language level.
        ("Deutschkenntnisse sowie Führerschein Klasse C1", None, None),
    ],
)
def test_shared_and_distant_levels(text, level, mandatory):
    requirement = extract_german_requirement(text)
    assert (requirement.level, requirement.mandatory) == (level, mandatory)


def test_shared_b2_is_a_major_gap_at_a2():
    text = "Deutsch und Englisch auf mindestens B2-Niveau erforderlich"
    assert fit.language_criterion(text, "A2").status == "MAJOR_GAP"
