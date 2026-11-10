"""Stage 59: a FUTURE full-time possibility is not evidence of today's full time.

"Teilzeit, später Vollzeit möglich" is a part-time vacancy. Only a CURRENT full-time statement may
open the automatic gate (AUTO_PREPARE needs confirmed FULL_TIME + duplicate CLEAR).
"""

from types import SimpleNamespace

import pytest

from app.agents.job_fit_signals import classify_work_time
from app.services.job_fit_assessment import work_time_criterion
from app.services.shortlist_gate import automatic_block_reason
from tests.test_remote_work_eligibility import _assessment, _status


def mode(text: str) -> str:
    return classify_work_time(text).mode


def _row(flag):
    return SimpleNamespace(full_time_offered=flag)


@pytest.mark.parametrize(
    "text",
    [
        "Teilzeit, später Vollzeit möglich",
        "Teilzeit mit späterer Vollzeitoption",
        "Teilzeit, Vollzeit nach Einarbeitung möglich",
        "Zunächst Teilzeit, später Vollzeit",
        "Part-time initially, full-time possible later",
        "Part-time role with full-time opportunity",
        "Aktuell Teilzeit, später Vollzeit",
        "Nur Teilzeit, Vollzeit später möglich",
        "Anfangs Teilzeit, danach Vollzeit",
        "Part-time now, full-time in the future",
        "Stelle: Teilzeit 20h. Später Vollzeit möglich.",
        "Teilzeit (30h), Aufstockung auf Vollzeit möglich",
        "Vollzeit ab 2027, aktuell Teilzeit",
        "Vollzeit ab 2027, aktuell Teilzeit möglich",
        "Teilzeit, später in Vollzeit möglich",
        "Teilzeit, Vollzeit nach Probezeit möglich",
        "Teilzeit mit Option auf Vollzeit",
        "Teilzeit mit Übergang in Vollzeit",
        "Teilzeit, Vollzeit ab 2027",
        "Teilzeit, Vollzeit nach erfolgreicher Einarbeitung",
        "Teilzeit, Vollzeit ist aber erst nach der Probezeit möglich",
        "Teilzeit mit Option auf eine unbefristete Vollzeitstelle",
        "Teilzeit, später: Vollzeit",
        ("Teilzeitoptionen. " * 25)
        + "Teilzeit, später Vollzeit möglich. Unsere Mitarbeiter arbeiten Vollzeit.",
        # a general or staff full-time remark never beats own part time + future full time
        "Teilzeit, später Voll- oder Teilzeit möglich. Unsere Mitarbeiter arbeiten Vollzeit.",
        "Wir suchen Mitarbeiter in Voll- und Teilzeit, Vollzeit aber erst später.",
    ],
)
def test_current_part_time_with_a_later_full_time_is_part_time(text):
    assert mode(text) == "PART_TIME"


@pytest.mark.parametrize(
    "text",
    [
        "Vollzeit ab 01.01.2027",
        "Vollzeit ab 1. Januar 2027",
        "Full-time, but only after onboarding.",
        "Vollzeit, jedoch erst nach der Probezeit",
        "Full-time, however, only after onboarding.",
        "Teilzeit zum Einstieg. Vollzeit nach sechs Monaten möglich.",
        "Teilzeit. Full-time after 6 months possible.",
        "20 h/Woche zum Einstieg. Vollzeit, nach der Probezeit möglich.",
        "20 h/Woche zum Einstieg, Vollzeit nach 6-monatiger Probezeit möglich.",
        "20 h/Woche mit Perspektive auf Vollzeit.",
        "20 h/Woche zum Einstieg. Nach der Probezeit, bei guter Leistung, ist Vollzeit möglich.",
        "20 h/Woche zum Einstieg; Vollzeit ab dem zweiten Monat.",
        "Vollzeit nach der Probezeit, bis dahin Teilzeit.",
        "Teilzeit zum Einstieg. Vollzeit ab Januar möglich.",
        "Full-time starting next month.",
        "Nach sechs Monaten in Teilzeit ist Vollzeit möglich.",
        "Vollzeit ab dem 2. Monat.",
        "Arbeitszeit: 20 Std. pro Woche mit Option auf Vollzeit.",
        "Wir suchen einen Mitarbeiter in Teilzeit mit Option auf Vollzeit.",
        "Perspektive auf eine Vollzeitstelle. Einstieg mit 20 Wochenstunden, Option auf Vollzeit.",
        "Join our team in a part-time role with the opportunity to transition to full-time.",
        "Vollzeit ab dem 1. Januar 2027.",
        "Full-time from January 1, 2027.",
        "Wir suchen eine Assistenz. Sie arbeiten in Teilzeit mit Option auf Vollzeit.",
        "Sie starten in Teilzeit und können später von Teilzeit auf Vollzeit aufstocken.",
        "Start at 20 hours per week; full-time from 01/01/2027.",
        "Zum Einstieg 20 Wochenstunden; Vollzeit ab dem 2. vollen Monat.",
        "Als Mitarbeiter arbeiten Sie in Teilzeit mit Option auf Vollzeit.",
        "Mit der Option auf Vollzeit starten Sie in Teilzeit.",
        "Zum Einstieg arbeiten Sie in Teilzeit. Später kann die Teilzeit auf Vollzeit aufstocken.",
        "Assistenz (m/w/d) in Teilzeit mit Option auf Vollzeit in unserem Team.",
        "Teilzeit zum Einstieg. Vollzeit ist i. d. R. erst nach der Probezeit möglich.",
        "Arbeitszeit: 19,50 Wochenstunden mit Option auf Vollzeit.",
        "Vollzeit ab dem 1. Feb. 2027.",
        "20 h/Woche zum Einstieg, Vollzeit nach 3–6 Monaten.",
        "Arbeitszeit: Teilzeit ohne Schichtdienst, mit Option auf Vollzeit.",
        "Teilzeit mit Vollzeitperspektive.",
        "Teilzeit mit Vollzeit-Perspektive.",
        "Vollzeit mit Gehaltserhöhung nach der Probezeit.",  # unclear scope: held, never FULL_TIME
    ],
)
def test_a_deferred_full_time_is_never_confirmed(text):
    # date dots do not end the clause; a contrastive continuation belongs to the same clause
    assert mode(text) != "FULL_TIME"


def test_stated_reduced_weekly_hours_beat_an_option_on_full_time():
    text = "Arbeitszeit: 20 Stunden pro Woche mit Option auf Vollzeit."
    assert mode(text) == "PART_TIME"
    assert mode("Arbeitszeit: 20 h/Woche mit Option auf Vollzeit.") == "PART_TIME"
    assert mode("Arbeitszeit: 20 Wochenstunden mit Option auf Vollzeit.") == "PART_TIME"
    assert mode("Vollzeit, 38,5 Stunden pro Woche. Später Homeoffice.") == "FULL_TIME"
    assert mode("Vollzeit ab 01.03.2026") == "FULL_TIME"


@pytest.mark.parametrize("text", ["Vollzeit perspektivisch möglich", "Full-time later"])
def test_a_future_full_time_alone_is_unknown_not_full_time(text):
    assert mode(text) == "UNKNOWN"


@pytest.mark.parametrize(
    "text",
    [
        "Vollzeit",
        "Vollzeit oder Teilzeit",
        "Vollzeit (40h) oder Teilzeit (30h)",
        "Vollzeit, Teilzeit auf Wunsch möglich",
        "Vollzeit, später Teilzeit möglich",
        "Aktuell Vollzeit, Teilzeit nach Absprache möglich",
        "Zunächst Vollzeit",
        "Wir suchen Dich in Voll- oder Teilzeit.",
        "We offer full- or part-time work.",
        "Einstellung in Vollzeit. Nach der Einarbeitung ist Homeoffice möglich.",
        "Vollzeit im Spätdienst",
        "Wir bieten eine langfristige Perspektive in Vollzeit (40 Stunden pro Woche). "
        "Unsere Kollegen arbeiten in Teilzeit.",
        "Flexible Arbeitszeiten mit der Option auf 4 Tage Vollzeit (32 Stunden pro Woche).",
        "Langfristige berufliche Perspektive in Vollzeitbeschäftigung (8 Stunden täglich).",
        "Vollzeit ab sofort mit Option auf spätere Teilzeit.",
        # real vacancies #49 and #2463: an option between full-time models / a permanent offer
        "Flexible Arbeitszeiten mit der Option auf 4 oder 5 Tage Vollzeit (32 – 40 Stunden)",
        "Langfristige berufliche Perspektive in Vollzeitbeschäftigung",
    ],
)
def test_a_current_full_time_is_not_lost(text):
    assert mode(text) == "FULL_TIME"


@pytest.mark.parametrize(
    "text", ["Teilzeitmöglichkeiten", "Part-time options available", "Gleitzeit, Teilzeitmodell"]
)
def test_a_listed_part_time_option_stays_unknown(text):
    assert mode(text) == "UNKNOWN"


@pytest.mark.parametrize(
    "text",
    ["Unsere Mitarbeiter arbeiten Voll- und Teilzeit", "Our employees work full- and part-time."],
)
def test_other_employees_hours_never_confirm_full_time(text):
    # kept on the human side (held, never FULL_TIME): the staff sentence is not this job's terms
    assert mode(text) in {"UNKNOWN", "PART_TIME"}


def test_a_staff_sentence_does_not_hide_the_vacancys_own_part_time():
    text = "Wir arbeiten in Voll- und Teilzeit. Die ausgeschriebene Stelle ist in Teilzeit."
    assert mode(text) == "PART_TIME"


def test_ba_flag_true_against_part_time_with_future_full_time_is_a_conflict():
    text = "Teilzeit, später Vollzeit möglich"
    assert work_time_criterion(text, _row(True)).status == "CONFLICT"
    assert work_time_criterion(text, _row(None)).status == "PART_TIME"
    assert work_time_criterion(text, _row(False)).status == "PART_TIME"


def test_ba_flag_decides_only_where_the_text_names_no_current_hours():
    text = "Vollzeit perspektivisch möglich"
    assert work_time_criterion(text, _row(True)).status == "FULL_TIME"  # structured flag
    assert work_time_criterion(text, _row(False)).status == "PART_TIME"
    assert work_time_criterion(text, _row(None)).status == "UNKNOWN"


@pytest.mark.parametrize(
    "hours,tier_blocked",
    [
        ("Teilzeit, später Vollzeit möglich", True),
        ("Vollzeit perspektivisch möglich", True),
        ("Vollzeit", False),
    ],
)
def test_a_future_full_time_never_opens_the_automatic_gate(hours, tier_blocked):
    a = _assessment("100% Remote. " + hours)
    reason = automatic_block_reason(a)
    if tier_blocked:
        assert _status(a, "work_time") in {"PART_TIME", "UNKNOWN"}
        assert reason is not None
    else:
        assert _status(a, "work_time") == "FULL_TIME"


def test_a_conflict_blocks_automatic_preparation():
    text = "100% Remote. Teilzeit, später Vollzeit möglich"
    assert work_time_criterion(text, _row(True)).status == "CONFLICT"
    a = _assessment(text)
    a.criteria = [c for c in a.criteria if c.name != "work_time"]
    a.criteria.append(work_time_criterion(text, _row(True)))
    assert _status(a, "work_time") == "CONFLICT"
    assert automatic_block_reason(a) is not None


def test_hostile_repetition_stays_bounded():
    import time

    start = time.perf_counter()
    result = classify_work_time("Teilzeit, später Vollzeit. " * 5000)
    assert time.perf_counter() - start < 2.0
    assert result.mode != "FULL_TIME"
