"""Night audit 2026-10-27: salary extraction regressions.

Live defect: "IT-Zulage 1.500 € mtl." was taken as the base salary
(18,000 EUR/year). Same class: Christmas/holiday pay and bonuses, the
13th salary, hourly rates, job-board estimates, "nach Vereinbarung".
"""

import pytest

import app.services.job_fit_assessment as fit
from app.agents.job_fit_signals import extract_salary
from app.models.candidate_search import CandidateSearchSettings


def _yearly(text):
    info = extract_salary(text)
    return info.yearly_min, info.yearly_max


@pytest.mark.parametrize(
    "text",
    [
        "Wir bieten eine IT-Zulage von 1.500 € mtl. sowie Weiterbildung.",
        "1.500 € IT-Zulage monatlich",
        "Weihnachtsgeld 1.000 € und Urlaubsgeld 500 €",
        "Jahresbonus bis 5.000 €",
        "Fahrtkostenzuschuss 1.200 € im Jahr",
        "Weiterbildungsbudget von 2.000 € pro Jahr",
    ],
)
def test_pay_components_never_become_the_base_salary(text):
    info = extract_salary(text)
    assert (info.yearly_min, info.yearly_max, info.period) == (None, None, None)
    assert info.components


def test_live_case_allowance_next_to_base_salary():
    info = extract_salary("Monatsgehalt 3.200 € brutto, zusätzlich IT-Zulage 1.500 € mtl.")
    assert (info.yearly_min, info.yearly_max, info.period) == (38400, 38400, "MONTH")
    assert [c.kind for c in info.components] == ["ALLOWANCE"]


@pytest.mark.parametrize(
    ("text", "low", "high", "months"),
    [
        ("Vergütung 3.700 – 5.000 € x 13 Gehälter", 48100, 65000, 13),
        ("3.700 € bis 5.000 € brutto monatlich (13 Monatsgehälter)", 48100, 65000, 13),
        ("Grundgehalt 3.500 € zzgl. 13. Monatsgehalt", 45500, 45500, 13),
        ("3.500 € brutto monatlich", 42000, 42000, 12),
    ],
)
def test_monthly_salary_uses_stated_number_of_salaries(text, low, high, months):
    info = extract_salary(text)
    assert (info.yearly_min, info.yearly_max, info.salaries_per_year) == (low, high, months)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Gehalt: 45.000–50.000 € brutto/Jahr", (45000, 50000)),
        ("bis zu 55.000 € im Jahr", (None, 55000)),
        ("€ 52.000 p.a.", (52000, 52000)),
    ],
)
def test_yearly_salaries(text, expected):
    assert _yearly(text) == expected


@pytest.mark.parametrize(
    ("text", "rate"),
    [("Stundenlohn 25 €/Stunde", 25.0), ("25 € pro Stunde", 25.0), ("15,50 € / Std.", 15.5)],
)
def test_hourly_rate_is_not_annualized(text, rate):
    info = extract_salary(text)
    assert (info.yearly_min, info.yearly_max) == (None, None)
    assert info.hourly_max == rate


@pytest.mark.parametrize(
    "text", ["Vergütung nach TVöD", "Eingruppierung TV-L, Stufe je nach Erfahrung"]
)
def test_tariff_without_group_or_step_is_unknown_amount(text):
    info = extract_salary(text)
    assert info.tariff and info.yearly_max is None


def test_salary_by_agreement():
    info = extract_salary("Gehalt nach Vereinbarung")
    assert info.negotiable and info.yearly_max is None


def test_job_board_estimate_is_labelled():
    info = extract_salary("Salary Estimate: 48.000 € - 60.000 € (estimated by XING)")
    assert (info.yearly_min, info.yearly_max) == (48000, 60000)
    assert info.provenance == "JOB_BOARD_ESTIMATE"


def test_employer_figure_wins_over_job_board_estimate():
    info = extract_salary(
        "Geschätztes Gehalt laut StepStone: 50.000 € - 60.000 €. "
        "Wir zahlen 45.000 € brutto im Jahr."
    )
    assert (info.yearly_min, info.yearly_max) == (45000, 45000)
    assert info.provenance == "EMPLOYER_STATED"


SETTINGS = CandidateSearchSettings(
    minimum_salary=45000, maximum_salary=50000, salary_negotiable=True
)


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Wir bieten eine IT-Zulage von 1.500 € mtl.", "UNKNOWN"),
        ("Stundenlohn 25 €/Stunde", "HOURLY"),
        ("Gehalt nach Vereinbarung", "NEGOTIABLE"),
        ("Vergütung 3.700 – 5.000 € x 13 Gehälter", "MATCH"),
        ("bis zu 55.000 € im Jahr", "MATCH"),
    ],
)
def test_salary_criterion_statuses(text, status):
    assert fit.salary_criterion(text, SETTINGS).status == status


def test_salary_criterion_flags_estimate_and_components():
    reason = fit.salary_criterion(
        "Salary Estimate: 48.000 € - 60.000 € (estimated by XING). Weihnachtsgeld 1.000 €",
        SETTINGS,
    ).reason
    assert "job-board estimate" in reason and "christmas bonus" in reason


def test_bonus_after_plus_sign_keeps_the_salary():
    info = extract_salary("Germany: €150000 - €154000 (EUR) + 20% bonus target")
    assert (info.yearly_min, info.yearly_max) == (150000, 154000)


def test_count_of_monthly_salaries_is_not_a_monthly_period():
    info = extract_salary("Einstieg mit Bachelorabschluss: 59.724 Euro (14 Monatsgehälter)")
    assert (info.yearly_min, info.yearly_max, info.period) == (59724, 59724, "YEAR")


@pytest.mark.parametrize(
    "text",
    [
        "Mitarbeiter-werben-Mitarbeiter-Prämie (bis zu 3.000 € brutto pro Einstellung)",
        "Weiterbildungszuschuss (bis 3.000 Euro)",
        "1.000€ Ausstattungsbudget",
        "Relocation Bonus in Höhe von 2.000€",
    ],
)
def test_live_benefit_amounts_are_components(text):
    assert extract_salary(text).yearly_max is None
