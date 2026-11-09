"""Remote work eligibility: where the employer sits does not matter for a confirmed fully
remote, full-time job that may be done from Germany -- and nothing else is switched off.

FULL_REMOTE skips only the commute question. Role, seniority, skills, experience and German
language checks, the part-time check and the country restriction all still apply; unclear
remote terms (REMOTE_UNVERIFIED) go to a manual review. Nothing here sends or writes anything.
"""

import re

import pytest

from app.agents.job_fit_signals import classify_remote, classify_work_time
from app.db.models import JobRecord
from app.models.candidate_search import CandidateSearchSettings, GeoPoint
from app.services.job_fit_assessment import (
    Criterion,
    FitAssessment,
    location_criterion,
    remote_criterion,
    work_time_criterion,
)
from app.services.shortlist_gate import automatic_block_reason
from app.services.triage_category import classify

SETTINGS = CandidateSearchSettings(
    home_city="Heusenstamm",
    home_region="Hessen",
    commute_origin_label="Heusenstamm Bahnhof",
    commute_origin=GeoPoint(lat=50.0601, lon=8.8022),
    max_commute_km=100,
    commute_measure="CAR_ROUTE",
    remote_work_scope="GERMANY",
    remote_work_from_region="Hessen",
)
BERLIN = {"location": "Berlin", "workplace_lat": 52.52, "workplace_lon": 13.405}
MUENCHEN = {"location": "München", "workplace_lat": 48.137, "workplace_lon": 11.575}


def _assessment(
    description: str,
    *,
    title="Junior Python Developer (m/w/d)",
    place=BERLIN,
    skills="STRONG",
    entry="ENTRY_LEVEL",
    language="NO_REQUIREMENT",
    role="PRIMARY",
    hours: str | None = None,
) -> FitAssessment:
    if (
        hours is None
    ):  # a posting that states no working time is UNKNOWN; most tests are not about it
        stated = re.search(
            r"vollzeit|teilzeit|full[\s-]?time|part[\s-]?time|voll-", description, re.I
        )
        hours = "" if stated else "Vollzeit. "
    description = hours + description
    job = JobRecord(title=title, company="Example GmbH", description=description, **place)
    text = f"{title}\n{place['location']}\n{description}"
    remote = remote_criterion(text, SETTINGS, job)
    a = FitAssessment(job_id=1, company="Example GmbH", title=title)
    a.criteria = [
        Criterion("skills", skills, "s"),
        Criterion("role", role, "r"),
        Criterion("entry_level", entry, "e"),
        Criterion("language", language, "g"),
        location_criterion(job, SETTINGS, remote, None),
        remote,
        work_time_criterion(text),
    ]
    return a


def _status(a: FitAssessment, name: str) -> str:
    return a.criterion(name).status


class TestSignals:
    @pytest.mark.parametrize(
        "text,mode",
        [
            ("Vollzeit, 100% Remote", "FULLY_REMOTE"),
            ("Full-time, Fully Remote", "FULLY_REMOTE"),
            ("Remote Germany", "FULLY_REMOTE"),
            ("Remote EU", "FULLY_REMOTE"),
            ("Homeoffice 100%", "FULLY_REMOTE"),
            ("Homeoffice möglich", "PARTIAL_REMOTE"),
            ("Remote möglich", "PARTIAL_REMOTE"),
            ("bis zu 100% Remote", "PARTIAL_REMOTE"),
            ("Hybrid: 2 Tage pro Woche im Büro", "HYBRID"),
            ("Hybrid, Remote Germany", "HYBRID"),
            ("kein Homeoffice, remote Germany", "ONSITE"),
            ("Python Entwickler in Berlin", "UNKNOWN"),
        ],
    )
    def test_remote_mode(self, text, mode):
        assert classify_remote(text).mode == mode

    @pytest.mark.parametrize(
        "text",
        ["Remote US only", "Fully remote, UK-based candidates only", "Remote in the United States"],
    )
    def test_foreign_country_restriction_is_detected(self, text):
        assert classify_remote(text).country_restriction

    @pytest.mark.parametrize(
        "text",
        [
            "Remote UK or Germany",
            "Remote within the EU, incl. Germany",
            "join us, remote Germany",
            "Remote EU",
        ],
    )
    def test_a_clause_naming_germany_or_europe_is_not_a_restriction(self, text):
        assert classify_remote(text).country_restriction is None

    @pytest.mark.parametrize(
        "text,mode",
        [
            ("Vollzeit", "FULL_TIME"),
            ("Full-time", "FULL_TIME"),
            ("Vollzeit oder Teilzeit", "FULL_TIME"),
            ("Teilzeit", "PART_TIME"),
            ("part-time", "PART_TIME"),
            ("Python Entwickler", "UNKNOWN"),
        ],
    )
    def test_work_time(self, text, mode):
        assert classify_work_time(text).mode == mode


class TestFullRemote:
    @pytest.mark.parametrize(
        "description,place",
        [
            ("Vollzeit, 100% Remote.", BERLIN),
            ("Full-time. Remote Germany.", MUENCHEN),
            ("Vollzeit. Remote EU, Arbeit aus Deutschland möglich.", MUENCHEN),
        ],
    )
    def test_employer_location_does_not_matter(self, description, place):
        a = _assessment(description, place=place)
        assert _status(a, "remote") == "COMPATIBLE"
        assert _status(a, "location") == "REMOTE_OK"  # no car route, no distance estimate
        assert a.fit_tier in {"GOOD_MATCH", "PARTIAL_MATCH"}
        result = classify(a)
        assert (result.category, result.work_format) == ("PRIMARY", "FULL_REMOTE")
        assert automatic_block_reason(a) is None  # still not an approval to apply

    def test_missing_office_location_does_not_block_a_confirmed_full_remote_job(self):
        a = _assessment("Vollzeit, 100% Remote.", place={"location": None})
        assert _status(a, "location") == "REMOTE_OK"
        assert automatic_block_reason(a) is None

    def test_foreign_country_only_is_not_suitable(self):
        a = _assessment("Vollzeit. Remote US only.")
        assert _status(a, "remote") == "INCOMPATIBLE"
        assert a.fit_tier == "NOT_SUITABLE"
        assert classify(a).category == "EXCLUDED"
        assert automatic_block_reason(a) == "FIT_TIER_NOT_SUITABLE"

    def test_german_state_restriction_for_another_state_is_incompatible(self):
        a = _assessment("Vollzeit, 100% Remote aus Berlin.")
        assert _status(a, "remote") == "INCOMPATIBLE"


class TestRemoteDoesNotSwitchOffOtherChecks:
    def test_senior_title_is_still_excluded(self):
        a = _assessment("Vollzeit, 100% Remote.", entry="SENIOR_TITLE")
        assert a.fit_tier == "NOT_SUITABLE"
        assert classify(a).category == "EXCLUDED"

    def test_stated_experience_and_language_gaps_still_apply(self):
        a = _assessment("Vollzeit, 100% Remote.", language="GAP")
        assert a.fit_tier == "STRETCH"
        assert automatic_block_reason(a) == "FIT_TIER_STRETCH"

    def test_off_target_role_is_still_excluded(self):
        a = _assessment("Vollzeit, 100% Remote.", role="OFF_ROLE")
        assert classify(a).category == "EXCLUDED"

    def test_part_time_remote_is_held_for_the_operator(self):
        a = _assessment("Teilzeit, 100% Remote.")
        assert _status(a, "location") == "REMOTE_OK"
        assert _status(a, "work_time") == "PART_TIME"
        assert a.fit_tier == "STRETCH"
        assert automatic_block_reason(a) == "FIT_TIER_STRETCH"

    def test_unknown_working_time_does_not_downgrade_the_fit_but_holds_automatic_preparation(self):
        # Stage 54: UNKNOWN is not "full time". The tier is untouched (the vacancy stays visible and
        # PRIMARY for the operator) but the automatic gate needs CONFIRMED full time.
        a = _assessment("100% Remote.", hours="")
        assert _status(a, "work_time") == "UNKNOWN"
        assert a.fit_tier == "GOOD_MATCH"
        assert automatic_block_reason(a) == "WORK_TIME_NOT_CONFIRMED"

    def test_confirmed_full_time_remote_is_the_one_that_may_be_prepared(self):
        a = _assessment("Vollzeit, 100% Remote.")
        assert _status(a, "work_time") == "FULL_TIME"
        assert automatic_block_reason(a) is None


class TestHybridOnsiteAndUnverified:
    def test_hybrid_far_away_keeps_the_commute_limit(self):
        a = _assessment("Vollzeit. Hybrid: 3 Tage pro Woche im Büro.", place=MUENCHEN)
        assert _status(a, "remote") == "HYBRID"
        assert _status(a, "location") != "REMOTE_OK"
        assert a.fit_tier == "NOT_SUITABLE"  # ~300 km line, beyond the limit
        assert classify(a).work_format == "HYBRID"

    def test_onsite_far_away_keeps_the_commute_limit(self):
        a = _assessment("Vollzeit. Kein Homeoffice.", place=BERLIN)
        assert _status(a, "location") != "REMOTE_OK"
        assert a.fit_tier == "NOT_SUITABLE"
        assert classify(a).work_format == "ONSITE"

    @pytest.mark.parametrize(
        "wording", ["Homeoffice möglich.", "Remote möglich.", "Remote nach Absprache."]
    )
    def test_unclear_remote_terms_are_not_treated_as_full_remote(self, wording):
        a = _assessment(f"Vollzeit. {wording}", place={"location": "Karlsruhe"})
        assert _status(a, "remote") == "PARTIAL_REMOTE"
        assert _status(a, "location") != "REMOTE_OK"
        result = classify(a)
        assert (result.category, result.code) == ("MANUAL_REVIEW", "REMOTE_UNVERIFIED")
        assert result.work_format == "REMOTE_UNVERIFIED"
        assert automatic_block_reason(a) == "FIT_TIER_NEEDS_OPERATOR"


class TestPriority:
    def test_full_remote_ranks_before_hybrid_before_onsite_within_primary(self):
        near = {"location": "Offenbach", "workplace_lat": 50.1, "workplace_lon": 8.77}
        remote = classify(_assessment("Vollzeit, 100% Remote.", place=BERLIN))
        hybrid = classify(_assessment("Vollzeit. Hybrid, 2 Tage im Büro.", place=near))
        onsite = classify(_assessment("Vollzeit. Kein Homeoffice.", place=near))
        assert [c.category for c in (remote, hybrid, onsite)] == ["PRIMARY"] * 3
        assert remote.rank < hybrid.rank < onsite.rank

    def test_role_family_still_outranks_the_work_format(self):
        adjacent_remote = classify(
            _assessment("Vollzeit, 100% Remote.", title="DevOps Engineer (m/w/d)", role="OTHER")
        )
        primary_onsite = classify(
            _assessment(
                "Vollzeit. Kein Homeoffice.",
                place={"location": "Offenbach", "workplace_lat": 50.1, "workplace_lon": 8.77},
            )
        )
        assert adjacent_remote.category == "ADJACENT"
        assert primary_onsite.rank < adjacent_remote.rank


class TestAdversarialPostings:
    """Inputs from the independent review: the commute gate must stay on unless remote work is
    affirmed without any conflicting presence rule or negation."""

    @pytest.mark.parametrize(
        "description",
        [
            "Vollzeit. Homeoffice 100%. Hybrid: 2 Tage pro Woche im Büro.",
            "Vollzeit. Remote Germany. Onsite every Tuesday in Berlin.",
            "Vollzeit. Remote Germany ist nicht möglich; Arbeitsort Berlin.",
            "Vollzeit. Homeoffice 100% ist nicht möglich. Arbeitsort Berlin.",
            "Vollzeit. 100% Remote, aber jeden Dienstag im Büro.",
            "Full-time. Fully remote. Office presence in Berlin required 2 days per week.",
        ],
    )
    def test_presence_or_negation_keeps_the_commute_gate(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") in {"HYBRID", "ONSITE"}
        assert _status(a, "location") != "REMOTE_OK"
        assert a.fit_tier == "NOT_SUITABLE"  # Berlin is far beyond the 100 km limit

    @pytest.mark.parametrize(
        "description",
        [
            "Vollzeit. 100% Remote in Poland.",
            "Vollzeit. 100% Remote Austria only.",
            "Vollzeit. 100% Remote Schweiz only.",
            "Vollzeit. 100% Remote nur aus Österreich.",
            "Vollzeit. 100% Remote, nur aus den USA.",
            "Vollzeit. 100% Remote US only, serving customers in Germany.",
        ],
    )
    def test_foreign_country_restriction_is_incompatible(self, description):
        a = _assessment(description)
        assert _status(a, "remote") == "INCOMPATIBLE"
        assert automatic_block_reason(a) == "FIT_TIER_NOT_SUITABLE"

    @pytest.mark.parametrize(
        "description",
        [
            "Vollzeit. 100% Remote in Germany. We are a UK-based company.",
            "Vollzeit. 100% Remote in Germany. We have UK-based employees.",
            "Vollzeit. 100% Remote in the UK or in Germany.",
            "Vollzeit. Fully remote within the EU, incl. Germany.",
            "Vollzeit. 100% Remote. Keine Präsenzpflicht.",
            "Vollzeit. 100% Remote. Büropräsenz ist nicht erforderlich.",
            "Full-time. Fully remote. No hybrid work.",
            "Vollzeit. 100% Remote. Arbeitszeit: 5 Tage pro Woche.",
            "100% Remote. Du kannst optional im Büro arbeiten.",
        ],
    )
    def test_company_location_negated_or_optional_presence_is_still_full_remote(self, description):
        a = _assessment(description)
        assert _status(a, "remote") == "COMPATIBLE"
        assert automatic_block_reason(a) is None

    @pytest.mark.parametrize(
        "description",
        [
            "Vollzeit. 100% Remote UK or Germany.",
            "Vollzeit. 100% Remote Poland / Deutschland.",
            "Full-time. Fully remote in Poland or Germany.",
        ],
    )
    def test_germany_named_as_a_place_of_work_is_compatible(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") == "COMPATIBLE"
        assert automatic_block_reason(a) is None

    @pytest.mark.parametrize(
        "description",
        [
            "Full-time. Fully remote. Offices in Poland and Spain.",
            "Full-time. Fully remote. Office attendance in Berlin is mandatory and Python "
            "experience is preferred.",
            "Full-time. Fully remote. You must attend our Berlin office twice a week.",
        ],
    )
    def test_office_vocabulary_keeps_the_commute_gate(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") in {"HYBRID", "PARTIAL_REMOTE", "UNVERIFIED_ELIGIBILITY"}
        assert _status(a, "location") != "REMOTE_OK"
        assert automatic_block_reason(a) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Full-time. We cannot offer 100% remote.",
            "Vollzeit. 100% Remote. Homeoffice ist nicht möglich.",
        ],
    )
    def test_a_denial_of_remote_work_wins_over_an_affirmative_phrase(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") == "ONSITE"
        assert _status(a, "location") != "REMOTE_OK"

    @pytest.mark.parametrize(
        "description",
        [
            "Full-time. Fully remote in Belgium only.",
            "Full-time. Fully remote. Only residents of Poland may apply.",
        ],
    )
    def test_unlisted_or_residency_restrictions_never_confirm_german_eligibility(self, description):
        near = {"location": "Offenbach", "workplace_lat": 50.1, "workplace_lon": 8.77}
        a = _assessment(description, place=near)  # commute would pass: the posting still bars
        assert _status(a, "remote") in {"INCOMPATIBLE", "UNVERIFIED_ELIGIBILITY"}
        assert automatic_block_reason(a) is not None

    def test_unclear_eligibility_is_a_manual_review_even_for_a_near_job(self):
        near = {"location": "Offenbach", "workplace_lat": 50.1, "workplace_lon": 8.77}
        a = _assessment("Full-time. Fully remote. Applicants must be eligible to work.", place=near)
        assert _status(a, "remote") == "UNVERIFIED_ELIGIBILITY"
        result = classify(a)
        assert (result.category, result.code) == ("MANUAL_REVIEW", "REMOTE_UNVERIFIED")
        assert automatic_block_reason(a) == "FIT_TIER_NEEDS_OPERATOR"

    def test_a_negated_country_requirement_is_no_restriction(self):
        a = _assessment(
            "Full-time. 100% Remote in Germany. Candidates need not be based in the UK."
        )
        assert _status(a, "remote") == "COMPATIBLE"

    @pytest.mark.parametrize(
        "description",
        [
            "Full-time. Fully remote. Only candidates based in Austria are eligible.",
            "Vollzeit. 100% Remote US only, Germany is our largest customer market.",
            "Vollzeit. 100% Remote US only, global customer base.",
        ],
    )
    def test_customer_markets_do_not_cancel_a_country_restriction(self, description):
        a = _assessment(description)
        assert _status(a, "remote") == "INCOMPATIBLE"
        assert automatic_block_reason(a) == "FIT_TIER_NOT_SUITABLE"

    @pytest.mark.parametrize(
        "description,mode",
        [
            (
                "Teilzeit (20 Stunden/Woche), 100% Remote. Unser Team: 50 Vollzeitmitarbeiter.",
                "PART_TIME",
            ),
            ("100% Remote. Teilzeit, keine Vollzeit möglich.", "PART_TIME"),
            ("100% Remote. Arbeitszeit: 40 Stunden/Woche. Teilzeit ist nicht möglich.", "UNKNOWN"),
            ("Vollzeit, Teilzeit ist möglich.", "FULL_TIME"),
            (
                "100% Remote. Teilzeit (20 Stunden/Woche). Vollzeit bieten wir nicht an.",
                "PART_TIME",
            ),
        ],
    )
    def test_working_time_ignores_headcounts_and_negations(self, description, mode):
        assert classify_work_time(description).mode == mode

    def test_part_time_only_remote_is_held(self):
        a = _assessment("Teilzeit, 100% Remote. Wir sind 50 Vollzeitmitarbeiter.")
        assert _status(a, "work_time") == "PART_TIME"
        assert automatic_block_reason(a) == "FIT_TIER_STRETCH"


@pytest.mark.parametrize(
    "text,mode",
    [
        ("Vollzeit, Homeoffice nicht möglich", "FULL_TIME"),
        ("Teilzeit, keine Vollzeit möglich", "PART_TIME"),
    ],
)
def test_a_negation_belongs_only_to_the_word_it_follows(text, mode):
    assert classify_work_time(text).mode == mode


class TestRound4Postings:
    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote. We are unable to support working remotely.",
            "Full-time. No remote experience required, but 100% on-site. We cannot offer remote.",
        ],
    )
    def test_long_range_denial_keeps_the_commute_gate(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") in {"ONSITE", "HYBRID"}
        assert _status(a, "location") != "REMOTE_OK"

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote EU. Work is permitted only from Poland.",
            "Fully remote. Applicants must reside in Estonia.",
            "Fully remote. Only residents of Poland may apply. Office visits are required.",
        ],
    )
    def test_restriction_is_not_cancelled_by_a_region_or_a_hybrid_rule(self, description):
        near = {"location": "Offenbach", "workplace_lat": 50.1, "workplace_lon": 8.77}
        a = _assessment(description, place=near)
        assert _status(a, "remote") in {"INCOMPATIBLE", "UNVERIFIED_ELIGIBILITY"}
        assert automatic_block_reason(a) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany. No remote experience required.",
            "Fully remote in Germany. Remote work from Poland is also available.",
        ],
    )
    def test_skill_requirements_and_additional_locations_do_not_exclude(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") == "COMPATIBLE"
        assert automatic_block_reason(a) is None


class TestRound5Postings:
    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany. Office attendance in Berlin is mandatory with no exceptions.",
            "Fully remote in Germany. You must attend our Berlin office every Tuesday so you can "
            "meet the team.",
        ],
    )
    def test_unrelated_negation_or_optional_wording_does_not_cancel_presence(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") in {"HYBRID", "PARTIAL_REMOTE", "UNVERIFIED_ELIGIBILITY"}
        assert _status(a, "location") != "REMOTE_OK"
        assert automatic_block_reason(a) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote EU. Work is restricted to Poland.",
            "Fully remote EU. This role requires residency in Poland.",
        ],
    )
    def test_a_stated_regional_claim_does_not_mask_a_later_restriction(self, description):
        a = _assessment(description)
        assert _status(a, "remote") in {"INCOMPATIBLE", "UNVERIFIED_ELIGIBILITY"}
        assert automatic_block_reason(a) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany with no office attendance required.",
            "Fully remote in Germany. Keine Büropräsenz nötig.",
            "100% Remote in Germany. Du kannst optional ins Büro kommen.",
        ],
    )
    def test_negated_office_attendance_keeps_the_remote_claim(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") == "COMPATIBLE"
        assert _status(a, "location") == "REMOTE_OK"


class TestRound6Postings:
    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany. No exceptions to mandatory office attendance.",
            "Fully remote in Germany. Office attendance is not optional.",
            "Fully remote in Germany. Remote working is not permitted.",
        ],
    )
    def test_strong_obligation_and_denial_wording(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") in {"HYBRID", "ONSITE", "PARTIAL_REMOTE"}
        assert _status(a, "location") != "REMOTE_OK"
        assert automatic_block_reason(a) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany. Work is restricted to Poland with no exceptions.",
            "Fully remote in Germany. We also require residency in Poland.",
        ],
    )
    def test_restriction_survives_unrelated_negation_and_also(self, description):
        a = _assessment(description)
        assert _status(a, "remote") in {"INCOMPATIBLE", "UNVERIFIED_ELIGIBILITY"}
        assert automatic_block_reason(a) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany. Our platform serves UK-based candidates.",
            "Fully remote in Germany. No prior experience is necessary for remote work.",
        ],
    )
    def test_customer_description_and_experience_wording_do_not_exclude(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") == "COMPATIBLE"
        assert automatic_block_reason(a) is None


class TestRound7Postings:
    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany. No exceptions to required office attendance.",
            "Fully remote in Germany. Remote work is prohibited.",
            "Fully remote in Germany. Remote work is forbidden.",
            "Fully remote in Germany. Remote Arbeit ist ausgeschlossen.",
            "Fully remote in Germany. No equipment will be provided for remote work because it "
            "is not permitted.",
        ],
    )
    def test_prohibition_and_required_attendance_keep_the_commute_gate(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") in {"HYBRID", "ONSITE", "PARTIAL_REMOTE"}
        assert _status(a, "location") != "REMOTE_OK"
        assert automatic_block_reason(a) is not None

    def test_no_exceptions_does_not_cancel_a_country_restriction(self):
        a = _assessment("Fully remote in Germany. No exceptions: only from Poland.")
        assert _status(a, "remote") in {"INCOMPATIBLE", "UNVERIFIED_ELIGIBILITY"}
        assert automatic_block_reason(a) is not None


class TestRound8Postings:
    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote work is unavailable for this position.",
            "100% Remote kommt für diese Stelle nicht infrage.",
            "100% Remote nach der Einarbeitung. Die Einarbeitung findet an unserem Standort in "
            "Berlin statt.",
            "100% Remote. Monatliche Team-Meetings in Berlin.",
        ],
    )
    def test_realistic_denial_and_onboarding_wording_keeps_the_commute_gate(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") != "COMPATIBLE"
        assert _status(a, "location") != "REMOTE_OK"
        assert automatic_block_reason(a) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote. Polish residency is required.",
            "100% Remote. Polnische Staatsbürgerschaft.",
        ],
    )
    def test_nationality_adjectives_restrict_eligibility(self, description):
        a = _assessment(description)
        assert _status(a, "remote") in {"INCOMPATIBLE", "UNVERIFIED_ELIGIBILITY"}
        assert automatic_block_reason(a) is not None


def test_customer_residents_wording_goes_to_review_but_is_never_a_hard_exclusion():
    a = _assessment("Vollzeit. 100% Remote in Germany. Our software serves UK residents.")
    assert _status(a, "remote") == "UNVERIFIED_ELIGIBILITY"  # the operator confirms in a second
    assert a.fit_tier == "NEEDS_OPERATOR"  # reviewed, not excluded, whatever the employer location


class TestUnclearRemoteIsReviewedNotExcluded:
    """REMOTE_UNVERIFIED: a full-remote claim with unclear terms goes to the operator even when the
    employer is far away; a plain on-site job that merely allows home office stays excluded."""

    def test_far_job_with_a_vetoed_full_remote_claim_is_a_manual_review(self):
        a = _assessment("Vollzeit. 100% Remote. Präsenz nach Absprache.", place=BERLIN)
        assert _status(a, "remote") == "UNVERIFIED_ELIGIBILITY"
        assert a.fit_tier == "NEEDS_OPERATOR"
        result = classify(a)
        assert (result.category, result.code) == ("MANUAL_REVIEW", "REMOTE_UNVERIFIED")
        assert automatic_block_reason(a) == "FIT_TIER_NEEDS_OPERATOR"

    def test_far_job_with_unclear_eligibility_is_a_manual_review(self):
        a = _assessment("Fully remote. Applicants must reside in Estonia.", place=BERLIN)
        assert _status(a, "remote") == "UNVERIFIED_ELIGIBILITY"
        assert a.fit_tier == "NEEDS_OPERATOR"

    def test_far_onsite_job_that_only_allows_home_office_stays_excluded(self):
        a = _assessment("Vollzeit. Homeoffice möglich.", place=BERLIN)
        assert _status(a, "remote") == "PARTIAL_REMOTE"
        assert a.fit_tier == "NOT_SUITABLE"

    def test_an_explicit_presence_obligation_keeps_the_commute_gate(self):
        a = _assessment(
            "Vollzeit. 100% Remote, aber Büropräsenz in Berlin ist zwingend.", place=BERLIN
        )
        assert _status(a, "remote") == "HYBRID"
        assert a.fit_tier == "NOT_SUITABLE"


@pytest.mark.parametrize(
    "description",
    [
        "100% Remote. Wir bieten Gesundheitskurse an, sowohl online als auch im Büro.",
        "Vollzeit. 100% Remote. Fitnessangebote vor Ort, ganz flexibel.",
        "Vollzeit. 100% Remote. Reisebereitschaft für Kundenprojekte innerhalb Deutschlands.",
        "Vollzeit. 100% Remote, gelegentlich Teamevents. Strukturierte Einarbeitung.",
    ],
)
def test_perks_and_customer_travel_are_not_a_presence_obligation(description):
    a = _assessment(description, place=BERLIN)
    assert _status(a, "remote") == "COMPATIBLE"
    assert automatic_block_reason(a) is None


@pytest.mark.parametrize(
    "description",
    [
        "Vollzeit. 100% Remote. Die Einarbeitung findet an unserem Standort in Berlin statt.",
        "Vollzeit. 100% Remote. Monatliche Team-Meetings in Berlin.",
        "Vollzeit. 100% Remote, quartalsweise Treffen im Büro in Berlin.",
    ],
)
def test_obligations_tied_to_a_site_veto_the_remote_claim(description):
    a = _assessment(description, place=BERLIN)
    assert _status(a, "remote") in {"HYBRID", "PARTIAL_REMOTE", "UNVERIFIED_ELIGIBILITY"}
    assert _status(a, "location") != "REMOTE_OK"


class TestRound9Postings:
    @pytest.mark.parametrize(
        "description",
        [
            "100% Remote. Die Einarbeitung erfolgt vor Ort in Berlin.",
            "Fully remote in Germany. You are expected to work from our office in Berlin.",
            "Fully remote in Germany. Office attendance can be required at short notice.",
            "Fully remote in Germany. Remote working is no longer an option for this position.",
            "100% Remote. Homeoffice ist für diese Position keine Option.",
            "Fully remote. This position is restricted to Estonia.",
            "Fully remote in Germany. No exceptions to mandatory office attendance.",
        ],
    )
    def test_realistic_presence_denial_and_restriction_wording(self, description):
        near = {"location": "Offenbach", "workplace_lat": 50.1, "workplace_lon": 8.77}
        for place in (BERLIN, near):
            a = _assessment(description, place=place)
            # never treated as remote, wherever the employer is
            assert _status(a, "remote") != "COMPATIBLE", description
            assert _status(a, "location") != "REMOTE_OK", description
        # far away the commute gate (or the review) holds it; a hybrid job near home is legitimate
        assert automatic_block_reason(_assessment(description, place=BERLIN)) is not None

    @pytest.mark.parametrize(
        "description",
        [
            "Fully remote in Germany. Weekly yoga classes in our office.",
            "100% Remote in Deutschland. Jeden Mittwoch bieten wir Yogakurse im Büro an.",
            "Fully remote in Germany. Free on-site parking is available.",
            "Fully remote in Germany. No mandatory office attendance.",
            "Fully remote in Germany. You can visit the office if you like.",
        ],
    )
    def test_benefits_and_negated_obligations_do_not_veto_remote(self, description):
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") == "COMPATIBLE", description
        assert automatic_block_reason(a) is None
