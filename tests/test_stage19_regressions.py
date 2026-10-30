"""Stage 19 regressions found by the end-to-end rehearsal (pure, no DB)."""

import pytest

from app.agents.job_fit_signals import extract_salary
from app.services.job_fit_assessment import Criterion, FitAssessment


@pytest.mark.parametrize(
    ("text", "low", "high", "provenance"),
    [
        ("Gehalt zwischen 42.000 und 48.000 € brutto im Jahr", 42000, 48000, "EMPLOYER_STATED"),
        ("zwischen 42.000 € und 48.000 € jährlich", 42000, 48000, "EMPLOYER_STATED"),
        ("zwischen 3.200 und 3.800 € monatlich", 38400, 45600, "EMPLOYER_STATED"),
        # A salary calculator's figure is an estimate, not the employer's.
        (
            "Laut Gehaltsrechner verdienen Sie zwischen 40.000 und 52.000 €",
            40000,
            52000,
            "JOB_BOARD_ESTIMATE",
        ),
        # Unchanged rules next to the new range.
        ("45.000–55.000 € jährlich", 45000, 55000, "EMPLOYER_STATED"),
        ("bis zu 60.000 €", None, 60000, "EMPLOYER_STATED"),
    ],
)
def test_salary_ranges(text, low, high, provenance):
    info = extract_salary(text)
    assert (info.yearly_min, info.yearly_max, info.provenance) == (low, high, provenance)


def test_allowance_never_becomes_the_salary_next_to_a_range():
    info = extract_salary("IT-Zulage 1.500 € monatlich. Gehalt zwischen 42.000 und 48.000 €.")
    assert (info.yearly_min, info.yearly_max) == (42000, 48000)
    assert [c.kind for c in info.components] == ["ALLOWANCE"]


BASE = {
    "skills": "STRONG",
    "role": "PRIMARY",
    "experience": "UNKNOWN",
    "language": "OK",
    "location": "PASS",
    "remote": "HYBRID",
    "salary": "MATCH",
    "entry_level": "ENTRY_LEVEL",
    "start_date": "OK",
    "duplicate": "CLEAR",
}


def _assessment(
    *,
    basis="EXPLICIT_REQUIREMENT",
    confirmed=True,
    experience_missing=True,
    **statuses,
):
    values = {**BASE, **statuses}
    return FitAssessment(
        job_id=1,
        company="Example GmbH",
        title="Junior Python Developer",
        criteria=[Criterion(name, status, f"{name} reason") for name, status in values.items()],
        german_requirement_basis=basis,
        german_level_confirmed=confirmed,
        experience_missing_in_profile=experience_missing,
    )


def test_good_match():
    assessment = _assessment()
    assert assessment.fit_tier == "GOOD_MATCH" and assessment.decision == "CANDIDATE"


@pytest.mark.parametrize(
    ("statuses", "options", "tier"),
    [
        # Explicit mandatory German two levels above a confirmed level.
        ({"language": "MAJOR_GAP"}, {}, "NOT_SUITABLE"),
        # ... but not when the candidate's level is unconfirmed (Codex plan
        # review): not a barrier. Stage 22 P1: a stated language gap is a
        # STRETCH (worth trying), no longer "needs the operator".
        ({"language": "MAJOR_GAP"}, {"confirmed": False}, "STRETCH"),
        ({"language": "GAP_TO_VERIFY"}, {"basis": "INFERRED_REQUIREMENT"}, "STRETCH"),
        # A senior title with an unmet experience requirement.
        ({"experience": "GAP", "entry_level": "EXPERIENCED"}, {}, "NOT_SUITABLE"),
        # Codex rounds 1-4: without a current analysis listing the
        # experience as MISSING (confirmed, UNKNOWN, stale, none) -- verify.
        # Stage 22 P1: a stated experience gap is a STRETCH, not "verify".
        (
            {"experience": "GAP", "entry_level": "EXPERIENCED"},
            {"experience_missing": False},
            "STRETCH",
        ),
        # Years asked without a senior title may be "wünschenswert": stretch.
        ({"experience": "GAP"}, {}, "STRETCH"),
        ({"location": "FAIL"}, {}, "NOT_SUITABLE"),
        ({"location": "DISTANCE_UNVERIFIED"}, {}, "NEEDS_OPERATOR"),
        # GOOD_MATCH needs the role and the money to fit too.
        ({"role": "OTHER"}, {}, "PARTIAL_MATCH"),
        ({"salary": "BELOW"}, {}, "PARTIAL_MATCH"),
        ({"skills": "PARTIAL"}, {}, "PARTIAL_MATCH"),
    ],
)
def test_fit_tier_rules(statuses, options, tier):
    assessment = _assessment(**options, **statuses)
    assert assessment.fit_tier == tier, assessment.tier_reasons
    assert assessment.tier_reasons  # always explained


def test_tier_never_changes_the_stage17_decision():
    assert _assessment(language="MAJOR_GAP").decision == "CANDIDATE"
    assert _assessment(location="FAIL").decision == "BLOCKED"


def test_process_gmail_summary_names_the_next_step():
    from types import SimpleNamespace

    from scripts.stage15_application_tracker import decision_summary

    def result(**kw):
        base = dict(
            operator_review="NEEDS_OPERATOR_REVIEW",
            classification="REJECTION",
            application_id=None,
            proposed_status=None,
            review_candidates=(),
            message_ref="abc123",
            correlation_status="UNMATCHED",
            reason="NO_SIGNAL",
        )
        return SimpleNamespace(**{**base, **kw})

    lines = decision_summary(
        [
            result(application_id=7, proposed_status="REJECTED"),
            result(classification="INTERVIEW_INVITATION", review_candidates=(3,)),
            result(operator_review=None),
        ],
        lambda app_id: f"Firma {app_id} | Rolle",
    )
    assert lines[0] == "NEEDS YOUR DECISION (2):"
    assert any("transition 7 REJECTED" in line for line in lines)
    assert any("same sender as application(s) 3 (Firma 3 | Rolle)" in line for line in lines)
    assert decision_summary([result(operator_review=None)], str) == ["NEEDS YOUR DECISION: nothing"]
