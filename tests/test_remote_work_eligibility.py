"""Remote work eligibility: where the employer sits does not matter for a confirmed fully
remote, full-time job that may be done from Germany -- and nothing else is switched off.

FULL_REMOTE skips only the commute question. Role, seniority, skills, experience and German
language checks, the part-time check and the country restriction all still apply; unclear
remote terms (REMOTE_UNVERIFIED) go to a manual review. Nothing here sends or writes anything.
"""

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
) -> FitAssessment:
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

    def test_unknown_working_time_never_holds_a_remote_job(self):
        a = _assessment("100% Remote.")
        assert _status(a, "work_time") == "UNKNOWN"
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
            "Vollzeit. 100% Remote in Germany. Our software serves UK residents.",
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
            "Full-time. Fully remote. Offices in Poland and Spain.",
        ],
    )
    def test_an_unresolved_foreign_country_sends_remote_to_manual_review(self, description):
        """Not a hard exclusion, but never a skipped commute gate either."""
        a = _assessment(description, place=BERLIN)
        assert _status(a, "remote") == "PARTIAL_REMOTE"
        assert _status(a, "location") != "REMOTE_OK"
        assert automatic_block_reason(a) is not None

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
