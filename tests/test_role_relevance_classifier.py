from app.agents.role_relevance_classifier import (
    classify_title_relevance,
    derive_candidate_target_domain,
)

# --- Must remain eligible (RELEVANT) ----------------------------------------


def test_junior_python_developer_is_relevant():
    result = classify_title_relevance("Junior Python Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "developer"


def test_python_entwickler_is_relevant():
    result = classify_title_relevance("Python Entwickler")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "entwickler"


def test_backend_developer_is_relevant():
    result = classify_title_relevance("Backend Developer (m/w/div.)")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "developer"


def test_software_engineer_backend_is_relevant():
    result = classify_title_relevance("Software Engineer Backend")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "software-engineer"


def test_anwendungsentwickler_is_relevant():
    result = classify_title_relevance("Anwendungsentwickler (m/w/d)")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "entwickler"


def test_ai_engineer_is_relevant():
    result = classify_title_relevance("AI Engineer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "ai-engineer"


def test_ki_entwickler_is_relevant():
    assert classify_title_relevance("KI-Entwickler").level == "RELEVANT"


def test_real_pilot_ai_engineer_title_is_relevant():
    # The real Stage 11 posting (Finanz Informatik) -- must not be
    # rejected merely because it isn't literally "Python Developer".
    title = "E362/B - AI Engineer / KI-Entwickler (m/w/d)"
    result = classify_title_relevance(title)
    assert result.level == "RELEVANT"


# --- Precision hardening: standalone technology words are NOT relevant
# role signals by themselves -----------------------------------------------


def test_python_systemadministrator_is_irrelevant_not_relevant():
    # Precision fix: standalone "python" is no longer a sufficient
    # RELEVANT signal -- "Systemadministrator" is the actual role, and it
    # is on the irrelevant allowlist.
    result = classify_title_relevance("Python Systemadministrator")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "systemadministrator"


def test_python_administrator_is_irrelevant_not_relevant():
    result = classify_title_relevance("Python Administrator")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "administrator"


def test_python_presales_consultant_is_irrelevant_not_relevant():
    result = classify_title_relevance("Python Presales Consultant")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "presales"


def test_python_personalcontroller_is_irrelevant_not_relevant():
    result = classify_title_relevance("Python Personalcontroller")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "personalcontroller"


def test_backend_administrator_is_irrelevant_not_relevant():
    # Precision fix: standalone "backend" is no longer a sufficient
    # RELEVANT signal either.
    result = classify_title_relevance("Backend Administrator")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "administrator"


def test_backend_presales_consultant_is_irrelevant_not_relevant():
    result = classify_title_relevance("Backend Presales Consultant")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "presales"


def test_mongodb_systemadministrator_is_irrelevant():
    result = classify_title_relevance("MongoDB Systemadministrator")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "systemadministrator"


def test_python_engineer_bare_engineer_is_unknown_not_relevant():
    # Bare "Engineer" (without "Software"/"AI" in front) is not a listed
    # role-family signal -- UNKNOWN is acceptable here, not a regression.
    result = classify_title_relevance("Python Engineer")
    assert result.level == "UNKNOWN"


def test_backend_engineer_bare_engineer_is_unknown_not_relevant():
    result = classify_title_relevance("Backend Engineer")
    assert result.level == "UNKNOWN"


# --- Must be confidently IRRELEVANT (unambiguous titles) -------------------


def test_personalcontroller_is_irrelevant():
    result = classify_title_relevance("Personalcontroller (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "personalcontroller"


def test_mongodb_administrator_is_irrelevant():
    result = classify_title_relevance("MongoDB Administrator (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "administrator"


def test_systemadministrator_is_irrelevant():
    result = classify_title_relevance("Systemadministrator (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "systemadministrator"


def test_qgis_expert_is_irrelevant():
    result = classify_title_relevance("QGIS Expertin / Experte")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis"


def test_presales_consultant_is_irrelevant():
    result = classify_title_relevance("Presales Consultant (m/w/d) - Datacenter")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "presales"


def test_berater_projektmanagement_is_irrelevant():
    title = "Berater im Projektmanagement im öffentlichen Sektor - Business Transformation (w/m/d)"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "projektmanagement"


def test_ingenieur_elektrotechnik_is_irrelevant():
    title = "Ingenieur Elektrotechnik (m/w/d) Automatisierung / Inbetriebnahme"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "elektrotechnik"


def test_wissenschaftliche_mitarbeiterin_is_irrelevant():
    # Known false-positive class from the pilot: scientific/academic
    # research-assistant titles where Python is incidental.
    title = "Wissenschaftliche/r Mitarbeiterin / Mitarbeiter im Projekt BMDNow"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "wissenschaftliche-mitarbeiter"


# --- Conflict policy: positive AND irrelevant both present -> UNKNOWN ------


def test_presales_software_engineer_is_unknown_genuinely_mixed():
    # "software engineer" (positive) + "presales" (irrelevant) both
    # match -- genuinely mixed, must fail open to UNKNOWN, not be
    # resolved by which pattern list happens to be checked first.
    result = classify_title_relevance("Presales Software Engineer")
    assert result.level == "UNKNOWN"
    assert result.matched_signal is None


def test_qgis_developer_is_unknown_genuinely_mixed():
    result = classify_title_relevance("QGIS Developer")
    assert result.level == "UNKNOWN"


def test_wissenschaftlicher_softwareentwickler_is_unknown_genuinely_mixed():
    result = classify_title_relevance("Wissenschaftlicher Softwareentwickler")
    assert result.level == "UNKNOWN"


def test_conflict_outcome_is_order_independent():
    # The SAME two signals, positive word first vs. irrelevant word first
    # -- must classify identically either way (proves this isn't a
    # first-match-wins ordered scan).
    a = classify_title_relevance("Developer Personalcontroller Hybrid Role")
    b = classify_title_relevance("Personalcontroller Developer Hybrid Role")
    assert a.level == "UNKNOWN"
    assert b.level == "UNKNOWN"
    assert a.level == b.level


# --- UNKNOWN / fail-open cases -----------------------------------------------


def test_data_consultant_with_no_signal_either_way_is_unknown():
    result = classify_title_relevance("Data Consultant (m/w/d)")
    assert result.level == "UNKNOWN"
    assert result.matched_signal is None


def test_blank_title_is_unknown():
    result = classify_title_relevance("")
    assert result.level == "UNKNOWN"


def test_ambiguous_generic_consultant_title_is_unknown_not_irrelevant():
    # Bare "Consultant"/"Berater" alone (no Projektmanagement/Presales
    # etc.) is deliberately NOT on the irrelevant allowlist -- too broad,
    # could legitimately be an IT/software consulting role.
    result = classify_title_relevance("Consultant (m/w/d)")
    assert result.level == "UNKNOWN"


def test_ambiguous_generic_ingenieur_title_is_unknown_not_irrelevant():
    # Bare "Ingenieur" alone (no Elektrotechnik) is deliberately NOT
    # excluded -- "Ingenieur Softwareentwicklung" would be a legitimate
    # software role using the German word for "Engineer".
    result = classify_title_relevance("Ingenieur (m/w/d)")
    assert result.level == "UNKNOWN"


# --- Avoid naive substring matching ------------------------------------------


def test_administratorin_feminine_form_still_matches_whole_word():
    result = classify_title_relevance("Administratorin (m/w/d)")
    assert result.level == "IRRELEVANT"


def test_unrelated_word_containing_lead_substring_is_not_falsely_relevant():
    # "Leadership" contains "lead" but not "developer"/"engineer"/etc --
    # documents that this classifier has no bare-substring shortcuts.
    result = classify_title_relevance("Leadership Program Trainee")
    assert result.level == "UNKNOWN"


# --- derive_candidate_target_domain -----------------------------------------


def test_derive_all_relevant_targets_is_software_development():
    target_roles = ["Junior Python Developer", "Junior Backend Developer"]
    assert derive_candidate_target_domain(target_roles) == "SOFTWARE_DEVELOPMENT"


def test_derive_empty_target_roles_is_unknown():
    assert derive_candidate_target_domain([]) == "UNKNOWN"


def test_derive_one_ambiguous_role_makes_whole_result_unknown():
    target_roles = ["Junior Python Developer", "Data Engineer Trainee"]
    # "Data Engineer Trainee" contains no relevant signal (no "developer"/
    # "software engineer"/"ai engineer" phrase match -- bare "engineer"
    # alone is not a listed relevant family) -- ambiguity must fail open.
    assert derive_candidate_target_domain(target_roles) == "UNKNOWN"


def test_derive_single_relevant_target_is_software_development():
    assert derive_candidate_target_domain(["Python Backend Developer"]) == "SOFTWARE_DEVELOPMENT"
