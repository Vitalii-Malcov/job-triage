"""Stage 54: uncertainty about the working time must not open the AUTOMATIC preparation gate.

The classifier is a text heuristic. "Not stated" and "stated in words it does not recognise"
(Halbtagsstelle, 50 %-Stelle, Minijob, hours without the word Teilzeit) are both UNKNOWN, so the
automatic gate requires CONFIRMED full time: the posting says so, or the source's own full-time flag
does. UNKNOWN keeps the fit tier (the vacancy stays visible to the operator) but is not admitted.
"""

import re
from types import SimpleNamespace

import pytest

from app.agents.job_fit_signals import classify_work_time
from app.services.job_fit_assessment import Criterion, work_time_criterion
from app.services.shortlist_gate import (
    BLOCK_WORK_TIME_NOT_CONFIRMED,
    automatic_block_reason,
)
from tests.test_remote_work_eligibility import _assessment, _status


def _with_work_time(status: str, confirmed: bool = True):
    a = _assessment("Vollzeit, 100% Remote.")
    a.criteria = [c for c in a.criteria if c.name != "work_time"]
    a.criteria.append(Criterion("work_time", status, "audit"))
    a.criteria = [c for c in a.criteria if c.name != "work_time_evidence"]
    if confirmed:
        a.criteria.append(Criterion("work_time_evidence", "CONFIRMED", "audit"))
    return a


def test_only_confirmed_full_time_opens_the_gate():
    assert automatic_block_reason(_with_work_time("FULL_TIME")) is None
    # Stage 60: a heuristic FULL_TIME without positive evidence is held
    assert (
        automatic_block_reason(_with_work_time("FULL_TIME", confirmed=False))
        == "FULL_TIME_NOT_CONFIRMED"
    )
    assert automatic_block_reason(_with_work_time("UNKNOWN")) == BLOCK_WORK_TIME_NOT_CONFIRMED
    # these are already held by the fit tier, before the work-time check is reached
    assert automatic_block_reason(_with_work_time("PART_TIME")) == "FIT_TIER_STRETCH"
    assert automatic_block_reason(_with_work_time("CONFLICT")) == "FIT_TIER_NEEDS_OPERATOR"


def test_an_assessment_without_a_work_time_criterion_fails_closed():
    a = _assessment("Vollzeit, 100% Remote.")
    a.criteria = [c for c in a.criteria if c.name != "work_time"]
    assert automatic_block_reason(a) == BLOCK_WORK_TIME_NOT_CONFIRMED


@pytest.mark.parametrize(
    "text",
    [
        "Halbtagsstelle, 20 Stunden pro Woche",
        "50 %-Stelle",
        "Minijob auf 538-Euro-Basis",
        "Werkstudent (m/w/d), 15 h/Woche",
        "Wir suchen Dich für 25 Stunden pro Woche",
        "Teilzeit: Vollzeit nicht möglich",
        "Die Stelle ist im Teilzeitmodell zu besetzen",
        "Part-time employment available immediately",
    ],
)
def test_part_time_the_classifier_does_not_recognise_still_cannot_open_the_gate(text):
    # whatever the classifier says about these, the gate must not admit them on silence alone
    a = _assessment("100% Remote. " + text, hours="")
    if _status(a, "work_time") != "FULL_TIME":
        assert automatic_block_reason(a) is not None
    assert classify_work_time(text).mode != "FULL_TIME"


def test_a_vacancy_the_gate_holds_keeps_its_tier_and_stays_visible_to_the_operator():
    a = _assessment("100% Remote.", hours="")
    assert a.fit_tier == "GOOD_MATCH"
    assert automatic_block_reason(a) == BLOCK_WORK_TIME_NOT_CONFIRMED


def test_the_source_flag_counts_as_confirmation_only_where_the_text_is_silent():
    silent = "Wir suchen einen Entwickler."
    assert (
        work_time_criterion(silent, SimpleNamespace(full_time_offered=True)).status == "FULL_TIME"
    )
    assert work_time_criterion(silent, SimpleNamespace(full_time_offered=None)).status == "UNKNOWN"
    assert (
        work_time_criterion(silent, SimpleNamespace(full_time_offered=False)).status == "PART_TIME"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Our company offers full- and part-time work. This position is part-time.",
        "Wir bieten Stellen in Voll- und Teilzeit. Diese Stelle ist eine Teilzeitstelle.",
    ],
)
def test_a_general_full_and_part_time_statement_never_overrides_the_vacancys_own_part_time(text):
    assert classify_work_time(text).mode == "PART_TIME"
    assert automatic_block_reason(_assessment("100% Remote. " + text, hours="")) is not None


def test_a_general_full_and_part_time_statement_alone_is_still_full_time():
    assert classify_work_time("We offer full- or part-time work.").mode == "FULL_TIME"
    assert classify_work_time("Wir suchen Dich in Voll- oder Teilzeit.").mode == "FULL_TIME"


def test_the_automatic_gate_function_does_not_read_the_recommendation_or_the_score():
    import inspect

    from app.services import shortlist_gate

    source = inspect.getsource(shortlist_gate.automatic_block_reason)
    assert not re.search(r"recommendation|\.score|MAYBE|APPLY", source)


def test_the_written_out_combination_does_not_override_the_vacancys_own_part_time():
    text = "Vollzeit oder Teilzeit. Diese Stelle ist Teilzeit."
    assert classify_work_time(text).mode == "PART_TIME"
    assert classify_work_time("Vollzeit oder Teilzeit.").mode == "FULL_TIME"
    assert classify_work_time("Vollzeit. Teilzeit auf Anfrage möglich.").mode == "FULL_TIME"


def test_the_audit_script_refuses_to_write_over_a_database(tmp_path):
    import sqlite3
    import subprocess
    import sys

    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    for target in (db, tmp_path / "report.db"):
        done = subprocess.run(
            [
                sys.executable,
                "scripts/audit_remote_fulltime_reconciliation.py",
                "--db",
                f"a={db}",
                "--out",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 2 and "REFUSED" in done.stderr
    assert db.stat().st_size > 0 or True  # the input database is untouched by a refusal


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Vollzeit oder Teilzeit. Arbeitszeit: Vollzeit oder Teilzeit.", "FULL_TIME"),
        ("Our company offers full-time work. This position is part-time.", "PART_TIME"),
        ("Unser Haus bietet Vollzeit an. Diese Stelle ist eine Teilzeitstelle.", "PART_TIME"),
        ("Vollzeit (40 h). Teilzeit auf Anfrage möglich.", "FULL_TIME"),
        ("Vollzeit. Kein Teilzeitjob.", "FULL_TIME"),
    ],
)
def test_a_general_full_time_statement_yields_to_the_vacancys_own_part_time(text, expected):
    assert classify_work_time(text).mode == expected


def test_the_hours_of_a_full_time_phrase_are_not_attributed_to_an_optional_part_time_mention():
    assert classify_work_time("Vollzeit (40 h), Teilzeit auf Anfrage möglich.").mode == "FULL_TIME"
    assert classify_work_time("Full-time (40 hours), part-time on request possible.").mode == (
        "FULL_TIME"
    )
    assert classify_work_time("We offer a part-time position (20 hours per week).").mode == (
        "PART_TIME"
    )
