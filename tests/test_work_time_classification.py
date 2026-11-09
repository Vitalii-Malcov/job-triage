"""Working-time classification: a listed OPTION is not a part-time job.

The rule under test: a posting is PART_TIME only when it states its own hours as part time;
"Teilzeitmöglichkeiten", "part-time options", "Teilzeit nach Absprache möglich" describe what the
employer may allow. No statement is UNKNOWN, never an automatic PART_TIME and never a silent
FULL_TIME. The source's structured flag speaks only where the text is silent; a contradiction is a
CONFLICT for the operator.
"""

from types import SimpleNamespace

import pytest

from app.agents.job_fit_signals import classify_work_time
from app.db.models import JobRecord
from app.services import review_queue as rq
from app.services.job_fit_assessment import (
    _VERIFY,
    Criterion,
    FitAssessment,
    work_time_criterion,
)
from app.services.shortlist_gate import automatic_block_reason
from app.services.triage_category import classify
from tests.test_remote_work_eligibility import _assessment, _status


def mode(text: str) -> str:
    return classify_work_time(text).mode


# 1-9: the table of the specification ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Vollzeit", "FULL_TIME"),
        ("Full-time position", "FULL_TIME"),
        ("Vollzeit oder Teilzeit", "FULL_TIME"),
        ("Vollzeit/Teilzeit", "FULL_TIME"),
        ("In Voll- oder Teilzeit zum nächstmöglichen Zeitpunkt", "FULL_TIME"),
        ("Voll-/Teilzeit", "FULL_TIME"),
        ("full- or part-time", "FULL_TIME"),
        ("Nur Teilzeit", "PART_TIME"),
        ("Ausschließlich Teilzeit", "PART_TIME"),
        ("Part-time only", "PART_TIME"),
        ("Arbeitszeit: Teilzeit", "PART_TIME"),
        ("Teilzeit (derzeit 20 Wochenstunden)", "PART_TIME"),
        ("Eine Teilzeitstelle (50 % der regelmäßigen Arbeitszeit)", "PART_TIME"),
        ("Flexible Arbeitszeit, Gleitzeit, Teilzeitmöglichkeiten", "UNKNOWN"),
        ("Teilzeitmöglichkeiten", "UNKNOWN"),
        ("Part-time options available", "UNKNOWN"),
        ("We offer flextime, mobile working, part-time options, job sharing", "UNKNOWN"),
        ("Teilzeit nach Absprache möglich", "UNKNOWN"),
        ("Die Stelle kann auch in Teilzeit besetzt werden", "UNKNOWN"),
        ("Einer Teilzeitanstellung steht grundsätzlich nichts im Wege", "UNKNOWN"),
        ("Möglichkeit zur Teilzeitbeschäftigung", "UNKNOWN"),
        ("Possibility of part-time work", "UNKNOWN"),
        ("Die Stelle ist teilzeitgeeignet", "UNKNOWN"),
        ("Teilzeit nicht möglich", "UNKNOWN"),
        ("Keine Vollzeit möglich", "UNKNOWN"),
        ("Python Entwickler gesucht", "UNKNOWN"),
        ("", "UNKNOWN"),
    ],
)
def test_the_specified_wordings(text, expected):
    assert mode(text) == expected


def test_an_unconfirmed_full_time_is_never_invented():
    # no phrase here proves full time; the answer must stay UNKNOWN, not FULL_TIME
    for text in ("Teilzeit nicht möglich", "Keine Vollzeit möglich", "Wir bieten Teilzeitmodelle"):
        assert mode(text) != "FULL_TIME"


def test_a_headcount_of_other_staff_is_not_this_vacancies_hours():
    assert mode("Wir sind 50 Vollzeitmitarbeiter an drei Standorten.") == "UNKNOWN"
    assert mode("Teilzeit (20 Stunden/Woche). Unser Team: 50 Vollzeitmitarbeiter.") == "PART_TIME"


def test_explicit_part_time_only_wins_over_a_benefit_word_next_to_it():
    assert mode("Nur Teilzeit. Gleitzeit und Sabbatical möglich.") == "PART_TIME"
    assert mode("Arbeitszeit: Teilzeit. Wir bieten Gleitzeit und Sabbaticals.") == "PART_TIME"


# 11-12: text versus the structured BA flag ------------------------------------------------------


def _row(flag):
    return SimpleNamespace(full_time_offered=flag)


def test_the_structured_flag_speaks_only_where_the_text_is_silent():
    silent = "Wir suchen einen Entwickler."
    assert work_time_criterion(silent, _row(True)).status == "FULL_TIME"
    assert work_time_criterion(silent, _row(False)).status == "PART_TIME"
    assert work_time_criterion(silent, _row(None)).status == "UNKNOWN"
    # an option in the text is silence about the hours: the flag decides, nothing else
    option = "Gleitzeit, Teilzeitmöglichkeiten"
    assert work_time_criterion(option, _row(True)).status == "FULL_TIME"
    assert work_time_criterion(option, _row(None)).status == "UNKNOWN"


@pytest.mark.parametrize(
    "text,flag",
    [("Vollzeit.", False), ("Nur Teilzeit, 20 Stunden pro Woche.", True)],
)
def test_text_and_flag_that_contradict_are_a_conflict_for_the_operator(text, flag):
    criterion = work_time_criterion(text, _row(flag))
    assert criterion.status == "CONFLICT"
    assert "arbeitszeitVollzeit" in criterion.reason  # the explanation is kept
    assert ("work_time", "CONFLICT") in _VERIFY


def test_a_conflict_holds_the_vacancy_for_verification_and_blocks_automatic_preparation():
    a = _assessment("Vollzeit, 100% Remote.")
    a.criteria = [c for c in a.criteria if c.name != "work_time"]
    a.criteria.append(Criterion("work_time", "CONFLICT", "text vs flag", "Vollzeit"))
    assert a.decision == "NEEDS_VERIFICATION"
    assert a.fit_tier == "NEEDS_OPERATOR"
    assert automatic_block_reason(a) == "FIT_TIER_NEEDS_OPERATOR"


# 13: the same text gives the same answer after any refresh --------------------------------------


def test_classification_is_deterministic_for_the_same_text():
    text = "Gleitzeit, Teilzeitmöglichkeiten\nDein Profil"
    assert {work_time_criterion(text, _row(None)).status for _ in range(5)} == {"UNKNOWN"}


# 14-16: the real vacancies #705, #752 and #1337 --------------------------------------------------

BA_705_752 = (
    "* Flexible Arbeitszeitgestaltung durch Gleitzeitregelungen, Teilzeitmöglichkeiten\n"
    "**Dein Profil** * Du hast ein abgeschlossenes Studium"
)
AIRBUS_1337 = (
    "We support work-life balance through flexible working hours (flextime), mobile working, "
    "part-time options, job sharing, and sabbatical opportunities."
)


@pytest.mark.parametrize("job_id,text", [(705, BA_705_752), (752, BA_705_752), (1337, AIRBUS_1337)])
def test_real_vacancies_with_a_part_time_option_are_not_part_time(job_id, text):
    # job ids as in the production copy: 705/752 FERCHAU Karlsruhe, 1337 Airbus
    assert work_time_criterion(text, _row(None)).status == "UNKNOWN"
    a = _assessment(text, title="Junior Backend Engineer (m/w/d)")
    assert _status(a, "work_time") == "UNKNOWN"
    assert a.fit_tier != "STRETCH" or "work_time" not in " ".join(a.tier_reasons)


# 17-19: the neighbours that must not move ---------------------------------------------------------


def test_a_bfi_style_junior_python_vacancy_stays_in_the_review_queue():
    a = _assessment(
        "Python, SQL, 100% Remote. Gleitzeit, Teilzeitmöglichkeiten.",
        skills="WEAK",
        language="GAP",
        role="OTHER",
    )
    a.criteria.append(Criterion("duplicate", "CLEAR", "no application recorded"))
    job = SimpleNamespace(
        id=3294,
        title="Junior Engineer (m/w/d) Schwerpunkt Python",
        company="BFI",
        source="bundesagentur",
        first_seen_at=None,
        score=70,
        recommendation="MAYBE",
        location="Berlin",
    )
    item = rq.review_item(job, a, None)
    assert item is not None
    assert item.work_time == "UNKNOWN"  # shown honestly, not as PART_TIME


def test_an_application_already_sent_still_removes_the_vacancy_from_the_queue():
    a = _assessment("Vollzeit, Python.", skills="PARTIAL")
    a.criteria = [c for c in a.criteria if c.name != "duplicate"] + [
        Criterion("duplicate", "ALREADY_APPLIED", "application 25 [SUBMITTED]")
    ]
    job = SimpleNamespace(
        id=1,
        title="Junior Python Entwickler",
        company="Leani",
        source="x",
        first_seen_at=None,
        score=70,
        recommendation="MAYBE",
        location="",
    )
    assert rq.review_item(job, a, None) is None


def test_a_senior_title_stays_not_suitable_whatever_the_working_time_says():
    a = _assessment(
        "Vollzeit, 100% Remote, Python.",
        title="Sr. Softwareentwickler (w/m/d) Linux",
        entry="SENIOR_TITLE",
    )
    assert a.fit_tier == "NOT_SUITABLE"
    assert automatic_block_reason(a) == "FIT_TIER_NOT_SUITABLE"


# 20-24: safety invariants -------------------------------------------------------------------------


def test_a_fixed_working_time_never_overrides_other_barriers():
    for text in ("Vollzeit, 100% Remote.", "Gleitzeit, Teilzeitmöglichkeiten, 100% Remote."):
        a = _assessment(text, role="OFF_ROLE")
        assert automatic_block_reason(a) == "FIT_TIER_NOT_SUITABLE"


def test_administration_and_support_titles_stay_out_of_automatic_preparation():
    a = _assessment("Vollzeit, 100% Remote.", title="Junior Python Entwickler / IT-Administrator")
    assert automatic_block_reason(a) is not None


def test_unverified_remote_does_not_pass_because_the_working_time_is_clear():
    a = _assessment("Vollzeit. Homeoffice möglich.", place={"location": "München"})
    assert _status(a, "remote") != "COMPATIBLE"
    assert automatic_block_reason(a) is not None


def test_manual_review_membership_does_not_clear_a_vacancy_for_application():
    a = _assessment("Vollzeit.", skills="WEAK", role="OTHER")
    assert classify(a).category in {"MANUAL_REVIEW", "EXCLUDED", "PRIMARY", "ADJACENT"}
    assert automatic_block_reason(a) is not None or a.fit_tier in {"GOOD_MATCH", "PARTIAL_MATCH"}


def test_the_classifier_module_cannot_send_or_write_anything():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/agents/job_fit_signals.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for module in imported:
        assert not any(
            w in module for w in ("telegram", "gmail", "smtp", "imap", "httpx", "sqlalchemy")
        )


def test_job_record_and_fit_assessment_types_are_unchanged():
    assert "full_time_offered" in JobRecord.__table__.c
    assert FitAssessment.__dataclass_fields__.keys() >= {"job_id", "company", "title", "criteria"}


# Codex review findings: genuine part time next to benefit words, staff blurbs, hostile text ------


@pytest.mark.parametrize(
    "text",
    [
        "100% Remote. We offer a part-time position (20 hours per week).",
        "Teilzeit, 20 Stunden pro Woche. Wir bieten Gleitzeit.",
        "Wir bieten eine Teilzeitstelle mit flexibler Gleitzeit und Sabbatical.",
        "Our benefits: flextime. This is a part-time role.",
    ],
)
def test_explicit_vacancy_hours_survive_benefit_words(text):
    assert mode(text) == "PART_TIME"


def test_genuine_part_time_next_to_benefits_keeps_the_automatic_gate_closed():
    a = _assessment("100% Remote. We offer a part-time position (20 hours per week).")
    assert _status(a, "work_time") == "PART_TIME"
    assert a.fit_tier == "STRETCH"
    assert automatic_block_reason(a) == "FIT_TIER_STRETCH"


@pytest.mark.parametrize(
    "text",
    [
        "Our full- and part-time employees support clients. Part-time position (20 hours).",
        "Unsere Voll- und Teilzeitmitarbeiter betreuen Kunden. Teilzeit, 20 Stunden.",
    ],
)
def test_a_staff_blurb_is_not_this_vacancies_full_time(text):
    assert mode(text) == "PART_TIME"


@pytest.mark.parametrize(
    "text",
    [
        "Remote Germany. We offer a part-time software developer position.",
        "Remote Germany. Working hours: part-time. Benefits: flextime.",
        "Unsere Voll- und Teilzeit-Mitarbeiter betreuen Kunden. Arbeitszeit: Teilzeit (20 h).",
        "Teilzeitmodelle. " * 40 + "Arbeitszeit: Teilzeit (20 Stunden).",
        "Teilzeitmodelle. " * 40 + "Wir suchen Dich in Teilzeit (20 Stunden).",
    ],
)
def test_real_part_time_is_never_turned_into_a_gate_opening_answer(text):
    # the safe direction: a doubtful mention may hold a vacancy for the operator, but a vacancy
    # that really is part time must never become UNKNOWN or FULL_TIME because of its surroundings
    assert mode(text) == "PART_TIME"


@pytest.mark.parametrize(
    "text",
    ["Teilzeitmodelle. " * 4000, "Nur Teilzeit nicht möglich. " * 2000, "Keine Vollzeit. " * 4000],
)
def test_a_page_repeating_a_word_thousands_of_times_costs_bounded_time(text):
    import time

    start = time.perf_counter()
    classify_work_time(text)
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "text",
    [
        "Flexible part-time position (20 hours per week)",
        "Part-time position with pension options available (20 hours per week)",
        "Teilzeitstelle mit Gleitzeit und Sabbatical (20 Stunden)",
    ],
)
def test_a_part_time_position_with_options_around_it_is_still_part_time(text):
    assert mode(text) == "PART_TIME"
    a = _assessment("100% Remote. " + text)
    assert automatic_block_reason(a) == "FIT_TIER_STRETCH"


def test_repeating_a_word_without_punctuation_costs_bounded_time():
    import time

    for text in ("Teilzeitmodelle " * 4000, "Teilzeit nicht möglich " * 2000):
        start = time.perf_counter()
        classify_work_time(text)
        assert time.perf_counter() - start < 1.0


def test_a_long_clause_keeps_its_office_obligation_for_the_remote_classifier():
    from app.agents.job_fit_signals import classify_remote

    text = (
        "100% remote. You must spend two days per week "
        + "working together with the team on many exciting things " * 10
        + "in our office"
    )
    assert len(text) > 400
    assert classify_remote(text).mode != "FULLY_REMOTE"


@pytest.mark.parametrize(
    "text",
    [
        "This position is part-time with pension options available (20 hours per week)",
        "Teilzeitstelle mit Homeoffice nach Absprache (20 Stunden/Woche)",
        "Our full- and part-time engineers support clients. This is a part-time position (20 h).",
        "Unsere Voll- und Teilzeit-Mitarbeiter betreuen Kunden. Dies ist eine Teilzeitstelle.",
    ],
)
def test_explicit_part_time_stays_part_time_whatever_surrounds_it(text):
    assert mode(text) == "PART_TIME"
    assert automatic_block_reason(_assessment("100% Remote. " + text)) == "FIT_TIER_STRETCH"


@pytest.mark.parametrize(
    "text",
    [
        "Nur Teilzeit nicht vorgesehen " * 4000,
        "Teilzeit nicht möglich " * 4000,
        "Keine Vollzeit " * 4000,
    ],
)
def test_hostile_repetition_is_bounded_and_never_answers_full_time(text):
    import time

    start = time.perf_counter()
    result = classify_work_time(text)
    assert time.perf_counter() - start < 1.0
    assert result.mode != "FULL_TIME"


@pytest.mark.parametrize(
    "text",
    [
        "This position is part-time with pension options available.",
        "Our employees work full- and part-time. This position is part-time.",
        "Keine Vollzeit. " * 26,
    ],
)
def test_round_five_gate_opening_cases_stay_on_the_human_side(text):
    assert mode(text) == "PART_TIME"


def test_a_staff_sentence_does_not_hide_a_real_offer_of_full_time():
    assert mode("Wir suchen Dich in Voll- oder Teilzeit.") == "FULL_TIME"
    assert mode("We offer full- or part-time work.") == "FULL_TIME"


def test_a_huge_token_after_the_word_costs_bounded_time():
    import time

    start = time.perf_counter()
    classify_work_time("Teilzeit" + "1" * 200_000)
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "text",
    [
        "Wir arbeiten in Voll- und Teilzeit. Die ausgeschriebene Stelle ist in Teilzeit.",
        "Sie arbeiten in Teilzeit grundsätzlich am Vormittag.",
    ],
)
def test_round_six_ordinary_sentences_stay_part_time(text):
    assert mode(text) == "PART_TIME"
    assert automatic_block_reason(_assessment("100% Remote. " + text)) == "FIT_TIER_STRETCH"


def test_a_grammatical_option_with_a_dependent_word_is_still_an_option():
    assert mode("Einer Teilzeitanstellung steht auch grundsätzlich nichts im Wege.") == "UNKNOWN"
    assert mode("Teilzeit ist grundsätzlich möglich.") == "UNKNOWN"


@pytest.mark.parametrize(
    "text",
    [
        "Sie arbeiten in Teilzeit bei Bedarf auch am Wochenende.",
        "Die Stelle ist in Teilzeit nach Vereinbarung an drei Tagen pro Woche zu besetzen.",
    ],
)
def test_schedule_wording_is_not_an_offer_of_part_time(text):
    assert mode(text) == "PART_TIME"
    assert automatic_block_reason(_assessment("100% Remote. " + text)) == "FIT_TIER_STRETCH"


@pytest.mark.parametrize(
    "text",
    [
        "Sie arbeiten in Teilzeit möglichst vormittags.",
        "Teilzeit nach Möglichkeit am Vormittag.",
    ],
)
def test_a_possibility_word_must_match_as_a_whole_word(text):
    assert mode(text) == "PART_TIME"
    assert automatic_block_reason(_assessment("100% Remote. " + text)) == "FIT_TIER_STRETCH"


def test_whole_word_possibility_is_still_an_offer():
    for text in (
        "Teilzeit ist möglich.",
        "Teilzeit möglich",
        "part-time possible",
        "Teilzeit denkbar",
    ):
        assert mode(text) == "UNKNOWN", text
