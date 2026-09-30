from app.agents.seniority_classifier import (
    classify_title_seniority,
    derive_candidate_target_seniority,
)


def test_senior_title_is_classified_senior():
    assert classify_title_seniority("Senior Entwickler Python (m/w/d)").level == "SENIOR"


def test_lead_title_is_classified_senior():
    result = classify_title_seniority("Lead Python Developer")
    assert result.level == "SENIOR"
    assert result.matched_signal == "lead"


def test_principal_title_is_classified_senior():
    assert classify_title_seniority("Principal Backend Engineer").level == "SENIOR"


def test_staff_title_is_classified_senior():
    assert classify_title_seniority("Staff Software Engineer").level == "SENIOR"


def test_head_title_is_classified_senior():
    assert classify_title_seniority("Head of Engineering").level == "SENIOR"


def test_leitung_title_is_classified_senior():
    assert classify_title_seniority("Leitung IT-Entwicklung (m/w/d)").level == "SENIOR"


def test_leiter_standalone_title_is_classified_senior():
    assert classify_title_seniority("Leiter Softwareentwicklung (m/w/d)").level == "SENIOR"


# --- S11A-001: explicit organizational-leadership allowlist ----------------


def test_teamleiter_is_classified_senior():
    result = classify_title_seniority("Teamleiter Softwareentwicklung (m/w/d)")
    assert result.level == "SENIOR"
    assert result.matched_signal == "leiter"


def test_teamleiterin_is_classified_senior():
    assert classify_title_seniority("Teamleiterin Softwareentwicklung (m/w/d)").level == "SENIOR"


def test_abteilungsleiter_is_classified_senior():
    assert classify_title_seniority("Abteilungsleiter (m/w/d)").level == "SENIOR"


def test_abteilungsleiterin_is_classified_senior():
    assert classify_title_seniority("Abteilungsleiterin (m/w/d)").level == "SENIOR"


def test_bereichsleiter_is_classified_senior():
    assert classify_title_seniority("Bereichsleiter IT (m/w/d)").level == "SENIOR"


def test_bereichsleiterin_is_classified_senior():
    assert classify_title_seniority("Bereichsleiterin IT (m/w/d)").level == "SENIOR"


def test_projektleiter_is_classified_senior():
    assert classify_title_seniority("Projektleiter Softwareentwicklung (m/w/d)").level == "SENIOR"


def test_projektleiterin_is_classified_senior():
    assert classify_title_seniority("Projektleiterin Softwareentwicklung (m/w/d)").level == "SENIOR"


def test_entwicklungsleiter_is_classified_senior():
    assert classify_title_seniority("Entwicklungsleiter (m/w/d)").level == "SENIOR"


def test_entwicklungsleiterin_is_classified_senior():
    assert classify_title_seniority("Entwicklungsleiterin (m/w/d)").level == "SENIOR"


def test_bauleiter_is_classified_senior():
    assert classify_title_seniority("Bauleiter (m/w/d)").level == "SENIOR"


def test_bauleiterin_is_classified_senior():
    assert classify_title_seniority("Bauleiterin (m/w/d)").level == "SENIOR"


# --- Negative cases: electrical/physics "conductor" homonyms ---------------
# German "Leiter" is also the word for "electrical conductor" -- these
# compounds are real engineering terms, not organizational compounds, and
# none of them are on the explicit allowlist above.


def test_stromleiter_conductor_word_is_unknown():
    assert classify_title_seniority("Stromleiter-Techniker (m/w/d)").level == "UNKNOWN"


def test_schutzleiter_protective_conductor_word_is_unknown():
    assert classify_title_seniority("Schutzleiter-Pruefung (m/w/d)").level == "UNKNOWN"


def test_neutralleiter_neutral_conductor_word_is_unknown():
    assert classify_title_seniority("Neutralleiter-Montage (m/w/d)").level == "UNKNOWN"


def test_aussenleiter_line_conductor_word_is_unknown():
    assert classify_title_seniority("Aussenleiter-Pruefung (m/w/d)").level == "UNKNOWN"


def test_innenleiter_inner_conductor_word_is_unknown():
    assert classify_title_seniority("Innenleiter-Fertigung (m/w/d)").level == "UNKNOWN"


def test_phasenleiter_phase_conductor_word_is_unknown():
    assert classify_title_seniority("Phasenleiter-Technik (m/w/d)").level == "UNKNOWN"


def test_kupferleiter_copper_conductor_word_is_unknown():
    assert classify_title_seniority("Kupferleiter-Fertigung (m/w/d)").level == "UNKNOWN"


def test_halbleiter_semiconductor_word_is_unknown():
    assert classify_title_seniority("Halbleiter-Ingenieur (m/w/d)").level == "UNKNOWN"


def test_wellenleiter_waveguide_word_is_unknown():
    assert classify_title_seniority("Wellenleiter-Ingenieur (m/w/d)").level == "UNKNOWN"


def test_lichtleiter_optical_fiber_word_is_unknown():
    assert classify_title_seniority("Lichtleiter-Techniker (m/w/d)").level == "UNKNOWN"


def test_blitzableiter_lightning_rod_word_is_unknown():
    assert classify_title_seniority("Blitzableiter-Techniker (m/w/d)").level == "UNKNOWN"


# --- Negative cases: the "-begleiter" (companion/escort) word family -------


def test_flugbegleiter_flight_attendant_is_unknown():
    assert classify_title_seniority("Flugbegleiter (m/w/d)").level == "UNKNOWN"


def test_flugbegleiterin_feminine_form_is_unknown():
    assert classify_title_seniority("Flugbegleiterin (m/w/d)").level == "UNKNOWN"


def test_alltagsbegleiter_everyday_companion_is_unknown():
    assert classify_title_seniority("Alltagsbegleiter (m/w/d)").level == "UNKNOWN"


def test_schulbegleiter_school_aide_is_unknown():
    assert classify_title_seniority("Schulbegleiter (m/w/d)").level == "UNKNOWN"


def test_integrationsbegleiter_is_unknown():
    assert classify_title_seniority("Integrationsbegleiter (m/w/d)").level == "UNKNOWN"


# --- Unrecognized organizational compounds stay UNKNOWN by design ----------


def test_unlisted_organizational_leiter_compound_stays_unknown():
    # "Niederlassungsleiter" (branch manager) is a genuine leadership
    # title but is NOT on the explicit allowlist -- per S11A-001, an
    # unsupported organizational compound must stay UNKNOWN rather than
    # be guessed at via a generic suffix scan. Extending the allowlist is
    # a deliberate, reviewed decision, not automatic.
    assert classify_title_seniority("Niederlassungsleiter (m/w/d)").level == "UNKNOWN"


def test_junior_title_is_not_senior():
    assert classify_title_seniority("Junior Python Developer").level == "UNKNOWN"


def test_plain_developer_title_is_not_senior():
    assert classify_title_seniority("Python Entwickler").level == "UNKNOWN"
    assert classify_title_seniority("Backend Developer").level == "UNKNOWN"


def test_title_with_no_seniority_marker_at_all():
    result = classify_title_seniority("Data Consultant (m/w/d)")
    assert result.level == "UNKNOWN"
    assert result.matched_signal is None


def test_senioren_prefixed_word_is_not_a_substring_false_positive():
    # "Seniorenberater" (elder-care advisor) contains "senior" as a
    # substring but is not a job-seniority marker -- \bsenior\b must not
    # match because there is no word boundary right after "senior" here.
    assert classify_title_seniority("Seniorenberater (m/w/d)").level == "UNKNOWN"


def test_leadership_word_is_not_a_substring_false_positive():
    # "Leadership" contains "lead" as a substring but \blead\b requires a
    # word boundary immediately after "lead", which "Leadership" doesn't
    # have.
    assert classify_title_seniority("Leadership Program Trainee").level == "UNKNOWN"


def test_headhunter_word_is_not_a_substring_false_positive():
    assert classify_title_seniority("Headhunter Assistant").level == "UNKNOWN"


def test_description_mentioning_senior_is_never_inspected():
    # classify_title_seniority only ever receives/inspects the title
    # string -- this test documents that contract by construction (no
    # description parameter exists to pass).
    result = classify_title_seniority("Python Entwickler")
    assert result.level == "UNKNOWN"


# --- S11A-002: candidate target derivation -- JUNIOR only if ALL roles say
# junior; fails open (UNKNOWN) on any ambiguity ------------------------------


def test_derive_all_junior_targets_is_junior():
    target_roles = ["Junior Python Developer", "Junior Backend Developer"]
    assert derive_candidate_target_seniority(target_roles) == "JUNIOR"


def test_derive_junior_plus_unlabeled_target_is_unknown():
    target_roles = ["Junior Python Developer", "Data Engineer"]
    assert derive_candidate_target_seniority(target_roles) == "UNKNOWN"


def test_derive_junior_plus_senior_target_is_unknown():
    target_roles = ["Junior Python Developer", "Senior Backend Developer"]
    assert derive_candidate_target_seniority(target_roles) == "UNKNOWN"


def test_derive_single_unlabeled_target_is_unknown():
    assert derive_candidate_target_seniority(["Python Backend Developer"]) == "UNKNOWN"


def test_derive_empty_target_roles_is_unknown():
    assert derive_candidate_target_seniority([]) == "UNKNOWN"


def test_derive_mixed_junior_and_senior_targets_is_unknown():
    # Candidate has not unanimously stated a junior-only target --
    # "cannot be determined" must not be guessed at.
    target_roles = ["Junior Python Developer", "Lead Python Developer"]
    assert derive_candidate_target_seniority(target_roles) == "UNKNOWN"


def test_derive_senior_only_target_is_unknown_not_senior():
    # This module never derives a SENIOR candidate target -- only JUNIOR
    # or UNKNOWN, per the Stage 11A scope (junior-vs-senior-title gating).
    assert derive_candidate_target_seniority(["Senior Python Developer"]) == "UNKNOWN"


def test_derive_single_junior_target_is_junior():
    assert derive_candidate_target_seniority(["Junior Python Developer"]) == "JUNIOR"


# --- M3 (Astra Stage 12 audit): mixed senior/junior markers in the SAME ----
# --- title/target-role string is ambiguous, not confidently one or the -----
# --- other -------------------------------------------------------------------


def test_mixed_senior_junior_title_is_unknown_not_senior():
    # Astra repro: a title offering both a senior AND a junior track must
    # not be confidently excluded as SENIOR -- that would incorrectly
    # deny a junior candidate a posting explicitly open to junior
    # applicants too.
    result = classify_title_seniority("Senior/Junior Python Developer")
    assert result.level == "UNKNOWN"
    assert result.matched_signal is None


def test_junior_title_with_senior_company_suffix_in_title_is_unknown():
    # Astra repro: "title contamination" -- a junior title with a company
    # name embedded IN the title string itself (e.g. via a dash suffix)
    # that happens to contain "Senior" must not flip to SENIOR either.
    result = classify_title_seniority("Junior Python Developer - Senior GmbH")
    assert result.level == "UNKNOWN"


def test_derive_mixed_senior_junior_single_target_role_is_unknown():
    # The candidate-side mirror of the title-side fix: a single target
    # role string that itself names both levels must not derive JUNIOR.
    assert derive_candidate_target_seniority(["Senior/Junior Python Developer"]) == "UNKNOWN"


# --- Explicit-signal isolation through the FULL evaluator (Astra test-------
# --- quality finding: the existing unit test never actually supplied a ----
# --- description to the production evaluator) ------------------------------


def test_full_evaluator_description_mentioning_senior_does_not_exclude_junior_candidate():
    from app.agents.job_score_evaluator import evaluate_job_score
    from app.models.job import Job

    job = Job(
        source="test",
        title="Junior Python Developer",
        company="Example GmbH",
        url="https://example.com/job/1",
        description=(
            "You will report to a senior manager and work alongside senior "
            "engineers on our Python backend. " * 10
        ),
        posting_type="ARBEIT",
        must_have_skills=["Python", "SQL"],
        nice_to_have_skills=["Docker"],
    )
    result = evaluate_job_score(
        job,
        "ARBEIT",
        candidate_skills=frozenset({"python", "sql", "docker"}),
        allowed_employment_types=frozenset(),
        get_candidate_target_seniority=lambda: "JUNIOR",
        get_candidate_target_domain=lambda: "UNKNOWN",
    )
    assert result.recommendation != "SKIP"
