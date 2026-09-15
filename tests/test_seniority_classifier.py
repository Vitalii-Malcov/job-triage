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


def test_teamleiter_compound_title_is_classified_senior():
    result = classify_title_seniority("Teamleiter Softwareentwicklung (m/w/d)")
    assert result.level == "SENIOR"
    assert result.matched_signal == "teamleiter"


def test_leitung_title_is_classified_senior():
    assert classify_title_seniority("Leitung IT-Entwicklung (m/w/d)").level == "SENIOR"


def test_leiter_standalone_title_is_classified_senior():
    assert classify_title_seniority("Leiter Softwareentwicklung (m/w/d)").level == "SENIOR"


def test_abteilungsleiter_compound_title_is_classified_senior():
    assert classify_title_seniority("Abteilungsleiter (m/w/d)").level == "SENIOR"


def test_abteilungsleiterin_compound_title_is_classified_senior():
    assert classify_title_seniority("Abteilungsleiterin (m/w/d)").level == "SENIOR"


def test_projektleiter_compound_title_is_classified_senior():
    assert classify_title_seniority("Projektleiter Softwareentwicklung (m/w/d)").level == "SENIOR"


def test_entwicklungsleiter_compound_title_is_classified_senior():
    assert classify_title_seniority("Entwicklungsleiter (m/w/d)").level == "SENIOR"


def test_bereichsleiter_compound_title_is_classified_senior():
    assert classify_title_seniority("Bereichsleiter IT (m/w/d)").level == "SENIOR"


def test_bauleiter_compound_title_is_classified_senior():
    assert classify_title_seniority("Bauleiter (m/w/d)").level == "SENIOR"


def test_halbleiter_semiconductor_word_is_not_a_substring_false_positive():
    # "Halbleiter" (semiconductor) is a genuine, common German engineering
    # term ending in "-leiter" but has nothing to do with job seniority --
    # must not be misclassified merely because it contains the "leiter"
    # substring.
    assert classify_title_seniority("Halbleiter-Ingenieur (m/w/d)").level == "UNKNOWN"


def test_blitzableiter_word_is_not_a_substring_false_positive():
    # "Blitzableiter" (lightning rod) similarly ends in "-leiter" but is
    # not a leadership title.
    assert classify_title_seniority("Blitzableiter-Techniker (m/w/d)").level == "UNKNOWN"


def test_wellenleiter_waveguide_word_is_not_a_substring_false_positive():
    # "Wellenleiter" (waveguide) -- same physics-conductor homonym class
    # as Halbleiter/Supraleiter, just a different first-part noun.
    assert classify_title_seniority("Wellenleiter-Ingenieur (m/w/d)").level == "UNKNOWN"


def test_lichtleiter_optical_fiber_word_is_not_a_substring_false_positive():
    # "Lichtleiter" (light guide / optical fiber) -- same physics-
    # conductor homonym class.
    assert classify_title_seniority("Lichtleiter-Techniker (m/w/d)").level == "UNKNOWN"


def test_flugbegleiter_flight_attendant_is_not_a_leadership_compound():
    # "Flugbegleiter" (flight attendant) ends in "-leiter" purely as a
    # string coincidence of the UNRELATED word "Begleiter" (companion/
    # escort, from "begleiten" = to accompany) -- must not be
    # misclassified as a leadership compound.
    assert classify_title_seniority("Flugbegleiter (m/w/d)").level == "UNKNOWN"


def test_flugbegleiterin_feminine_form_is_not_a_leadership_compound():
    assert classify_title_seniority("Flugbegleiterin (m/w/d)").level == "UNKNOWN"


def test_alltagsbegleiter_everyday_companion_is_not_a_leadership_compound():
    assert classify_title_seniority("Alltagsbegleiter (m/w/d)").level == "UNKNOWN"


def test_schulbegleiter_school_aide_is_not_a_leadership_compound():
    assert classify_title_seniority("Schulbegleiter (m/w/d)").level == "UNKNOWN"


def test_integrationsbegleiter_is_not_a_leadership_compound():
    assert classify_title_seniority("Integrationsbegleiter (m/w/d)").level == "UNKNOWN"


def test_unlisted_begleiter_compound_is_generically_excluded_by_family():
    # The "-begleiter" exclusion is a whole SUFFIX FAMILY, not an
    # enumerated word list -- a compound never explicitly named anywhere
    # in this module must still be excluded as long as it ends in
    # "begleiter"/"begleiterin".
    assert classify_title_seniority("Reisebegleiter (m/w/d)").level == "UNKNOWN"


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


def test_derive_junior_only_target_is_junior():
    target_roles = [
        "Junior Python Developer",
        "Junior Backend Developer",
        "Python Backend Developer",
    ]
    assert derive_candidate_target_seniority(target_roles) == "JUNIOR"


def test_derive_empty_target_roles_is_unknown():
    assert derive_candidate_target_seniority([]) == "UNKNOWN"


def test_derive_no_junior_signal_at_all_is_unknown():
    assert derive_candidate_target_seniority(["Python Backend Developer"]) == "UNKNOWN"


def test_derive_mixed_junior_and_senior_targets_is_unknown():
    # Candidate has not unambiguously stated a junior-only target --
    # "cannot be determined" must not be guessed at.
    target_roles = ["Junior Python Developer", "Lead Python Developer"]
    assert derive_candidate_target_seniority(target_roles) == "UNKNOWN"


def test_derive_senior_only_target_is_unknown_not_senior():
    # This module never derives a SENIOR candidate target -- only JUNIOR
    # or UNKNOWN, per the Stage 11A scope (junior-vs-senior-title gating).
    assert derive_candidate_target_seniority(["Senior Python Developer"]) == "UNKNOWN"
