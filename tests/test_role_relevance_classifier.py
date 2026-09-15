from app.agents.role_relevance_classifier import (
    classify_title_relevance,
    derive_candidate_target_domain,
)

# --- Must remain eligible (RELEVANT) ----------------------------------------


def test_junior_python_developer_is_relevant():
    result = classify_title_relevance("Junior Python Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "python-developer"


def test_python_entwickler_is_relevant():
    result = classify_title_relevance("Python Entwickler")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "entwickler"


def test_backend_developer_is_relevant():
    result = classify_title_relevance("Backend Developer (m/w/div.)")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "backend-developer"


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
    assert result.matched_signal == "qgis-expert"


def test_presales_consultant_is_irrelevant():
    result = classify_title_relevance("Presales Consultant (m/w/d) - Datacenter")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "presales"


def test_berater_projektmanagement_is_irrelevant():
    title = "Berater im Projektmanagement im öffentlichen Sektor - Business Transformation (w/m/d)"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "berater-projektmanagement"


def test_ingenieur_elektrotechnik_is_irrelevant():
    title = "Ingenieur Elektrotechnik (m/w/d) Automatisierung / Inbetriebnahme"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "ingenieur-elektrotechnik"


def test_wissenschaftliche_mitarbeiterin_is_irrelevant():
    # Known false-positive class from the pilot: scientific/academic
    # research-assistant titles where Python is incidental.
    title = "Wissenschaftliche/r Mitarbeiterin / Mitarbeiter im Projekt BMDNow"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "wissenschaftliche-mitarbeiter"


# --- S11B-001: scientific-role signal requires the explicit "Mitarbeiter"
# role noun, not the "wissenschaftlich..." adjective alone -------------------


def test_wissenschaftlicher_programmierer_is_not_irrelevant():
    result = classify_title_relevance("Wissenschaftlicher Programmierer")
    assert result.level != "IRRELEVANT"


def test_wissenschaftlicher_softwarearchitekt_is_not_irrelevant():
    result = classify_title_relevance("Wissenschaftlicher Softwarearchitekt")
    assert result.level != "IRRELEVANT"


def test_wissenschaftlicher_data_engineer_is_not_irrelevant():
    result = classify_title_relevance("Wissenschaftlicher Data Engineer")
    assert result.level != "IRRELEVANT"


def test_wissenschaftliche_hilfskraft_softwareentwicklung_is_not_irrelevant():
    result = classify_title_relevance("Wissenschaftliche Hilfskraft Softwareentwicklung")
    assert result.level != "IRRELEVANT"


def test_wissenschaftlicher_mitarbeiter_standard_form_is_irrelevant():
    result = classify_title_relevance("Wissenschaftlicher Mitarbeiter (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "wissenschaftliche-mitarbeiter"


def test_wissenschaftliche_mitarbeiterin_standard_form_is_irrelevant():
    result = classify_title_relevance("Wissenschaftliche Mitarbeiterin (m/w/d)")
    assert result.level == "IRRELEVANT"


def test_wissenschaftliche_slash_r_mitarbeiter_slash_in_form_is_irrelevant():
    title = "Wissenschaftliche/-r Mitarbeiter/-in im Bereich Arbeitsmarkt (w/m/d)"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "wissenschaftliche-mitarbeiter"


def test_wissenschaftliche_asterisk_r_mitarbeiter_asterisk_in_form_is_irrelevant():
    title = "Wissenschaftliche*r Mitarbeiter*in fuer Forschungsdatenmanagement"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "wissenschaftliche-mitarbeiter"


def test_real_pilot_scientific_mitarbeiter_vacancy_2_remains_irrelevant():
    title = (
        "Wissenschaftliche/-r Mitarbeiter/-in im Bereich Arbeitsmarkt (w/m/d) "
        "im Referat Arbeitsmarktbeteiligung"
    )
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"


def test_real_pilot_scientific_mitarbeiter_vacancy_3_remains_irrelevant():
    title = "Wissenschaftliche*r Mitarbeiter*in für Forschungsdatenmanagement"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"


# --- S11B-002: bare "Developer" is not a sufficient positive signal --------


def test_business_developer_is_unknown_not_relevant():
    result = classify_title_relevance("Business Developer")
    assert result.level == "UNKNOWN"


def test_senior_business_developer_is_unknown_not_relevant():
    result = classify_title_relevance("Senior Business Developer")
    assert result.level == "UNKNOWN"


def test_property_developer_is_unknown_not_relevant():
    result = classify_title_relevance("Property Developer")
    assert result.level == "UNKNOWN"


def test_real_estate_developer_is_unknown_not_relevant():
    result = classify_title_relevance("Real Estate Developer")
    assert result.level == "UNKNOWN"


def test_python_developer_bare_phrase_is_relevant():
    result = classify_title_relevance("Python Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "python-developer"


def test_software_developer_is_relevant():
    result = classify_title_relevance("Software Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "software-developer"


def test_fullstack_developer_is_relevant():
    result = classify_title_relevance("Fullstack Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "fullstack-developer"


def test_full_stack_developer_with_space_is_relevant():
    result = classify_title_relevance("Full Stack Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "fullstack-developer"


def test_frontend_developer_is_relevant():
    result = classify_title_relevance("Frontend Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "frontend-developer"


def test_web_developer_is_relevant():
    result = classify_title_relevance("Web Developer")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "web-developer"


def test_derive_business_developer_target_is_unknown():
    assert derive_candidate_target_domain(["Business Developer"]) == "UNKNOWN"


def test_derive_mixed_python_and_business_developer_target_is_unknown():
    target_roles = ["Junior Python Developer", "Business Developer"]
    assert derive_candidate_target_domain(target_roles) == "UNKNOWN"


def test_derive_junior_python_and_backend_developer_target_is_software_development():
    target_roles = ["Junior Python Developer", "Junior Backend Developer"]
    assert derive_candidate_target_domain(target_roles) == "SOFTWARE_DEVELOPMENT"


# --- Conflict policy: positive AND irrelevant both present -> UNKNOWN ------


def test_presales_software_engineer_is_unknown_genuinely_mixed():
    # "software engineer" (positive) + "presales" (irrelevant) both
    # match -- genuinely mixed, must fail open to UNKNOWN, not be
    # resolved by which pattern list happens to be checked first.
    result = classify_title_relevance("Presales Software Engineer")
    assert result.level == "UNKNOWN"
    assert result.matched_signal is None


def test_qgis_developer_is_unknown_after_s11b_003():
    # S11B-003 (fail-open hardening, supersedes the brief S11B-002-era
    # IRRELEVANT outcome): bare "qgis" is now a WEAK ambiguity signal,
    # not a STRONG one -- "QGIS Developer" doesn't match the STRONG
    # "qgis-expert"/"qgis-spezialist" role phrase, and "developer" alone
    # isn't a positive signal either (S11B-002), so this resolves to
    # UNKNOWN (neither positive nor strong-irrelevant present), never
    # IRRELEVANT.
    result = classify_title_relevance("QGIS Developer")
    assert result.level == "UNKNOWN"
    assert result.matched_signal is None


def test_wissenschaftlicher_softwareentwickler_is_relevant_after_s11b_001():
    # S11B-001 restricted the scientific-role signal to require the
    # explicit "Mitarbeiter" role noun -- "Wissenschaftlicher
    # Softwareentwickler" ("scientific/academic software developer") has
    # no "Mitarbeiter", so the irrelevant signal no longer fires, leaving
    # only the positive "entwickler" (Softwareentwickler) signal ->
    # RELEVANT. This is the intended fix, not a regression: a real
    # software-development role must not be excluded merely because it's
    # in a scientific/academic context.
    result = classify_title_relevance("Wissenschaftlicher Softwareentwickler")
    assert result.level == "RELEVANT"
    assert result.matched_signal == "entwickler"


def test_conflict_outcome_is_order_independent():
    # The SAME two signals, positive phrase first vs. irrelevant word
    # first -- must classify identically either way (proves this isn't a
    # first-match-wins ordered scan).
    a = classify_title_relevance("Software Developer Personalcontroller Hybrid Role")
    b = classify_title_relevance("Personalcontroller Software Developer Hybrid Role")
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


# --- S11B-003: QGIS restricted to explicit role phrases, not the bare
# domain word alone ----------------------------------------------------------


def test_qgis_expert_english_form_is_irrelevant():
    result = classify_title_relevance("QGIS Expert (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_expertin_is_irrelevant():
    result = classify_title_relevance("QGIS Expertin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_spezialist_is_irrelevant():
    result = classify_title_relevance("QGIS Spezialist (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_spezialistin_is_irrelevant():
    result = classify_title_relevance("QGIS Spezialistin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_python_engineer_is_not_irrelevant():
    result = classify_title_relevance("QGIS Python Engineer")
    assert result.level != "IRRELEVANT"


def test_qgis_software_engineer_is_not_irrelevant():
    # "Software Engineer" is a genuine positive phrase, and bare "qgis"
    # is only a weak ambiguity signal -- positive + weak -> UNKNOWN, not
    # IRRELEVANT (and not forced RELEVANT either -- genuinely ambiguous).
    result = classify_title_relevance("QGIS Software Engineer")
    assert result.level != "IRRELEVANT"
    assert result.level == "UNKNOWN"


def test_real_pilot_qgis_vacancy_remains_irrelevant():
    result = classify_title_relevance("QGIS Expertin / Experte")
    assert result.level == "IRRELEVANT"


# --- S11B-003: Elektrotechnik restricted to explicit role phrases ----------


def test_ingenieur_elektrotechnik_bare_is_irrelevant():
    result = classify_title_relevance("Ingenieur Elektrotechnik (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "ingenieur-elektrotechnik"


def test_elektroingenieur_is_irrelevant():
    result = classify_title_relevance("Elektroingenieur (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "elektroingenieur"


def test_elektroingenieurin_is_irrelevant():
    result = classify_title_relevance("Elektroingenieurin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "elektroingenieur"


def test_elektrotechniker_is_irrelevant():
    result = classify_title_relevance("Elektrotechniker (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "elektrotechniker"


def test_elektrotechnikerin_is_irrelevant():
    result = classify_title_relevance("Elektrotechnikerin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "elektrotechniker"


def test_software_engineer_elektrotechnik_is_unknown_not_irrelevant():
    # "Elektrotechnik" bare (the DOMAIN) must not reject an otherwise
    # ambiguous technical title -- genuinely mixed (positive "software
    # engineer" + weak "elektrotechnik") -> UNKNOWN, not IRRELEVANT, not
    # forced RELEVANT either.
    result = classify_title_relevance("Software Engineer Elektrotechnik")
    assert result.level == "UNKNOWN"


def test_python_engineer_elektrotechnik_is_unknown_not_irrelevant():
    # No positive signal ("Python Engineer" isn't a listed phrase) and
    # only the WEAK "elektrotechnik" domain word -- must fail open to
    # UNKNOWN, not IRRELEVANT.
    result = classify_title_relevance("Python Engineer Elektrotechnik")
    assert result.level == "UNKNOWN"


def test_real_pilot_elektrotechnik_vacancy_remains_irrelevant():
    title = "Ingenieur Elektrotechnik (m/w/d) Automatisierung / Inbetriebnahme (1687)"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"


# --- S11B-003: Projektmanagement restricted to explicit role phrases -------


def test_projektmanager_is_irrelevant():
    result = classify_title_relevance("Projektmanager (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "projektmanager"


def test_projektmanagerin_is_irrelevant():
    result = classify_title_relevance("Projektmanagerin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "projektmanager"


def test_berater_projektmanagement_bare_is_irrelevant():
    result = classify_title_relevance("Berater Projektmanagement (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "berater-projektmanagement"


def test_software_developer_projektmanagement_tools_is_not_irrelevant():
    # Bare "Projektmanagement" (the DOMAIN/tool context) must not force
    # IRRELEVANT on an otherwise positive title -- genuinely mixed
    # (positive "software developer" + weak "projektmanagement") ->
    # UNKNOWN or RELEVANT are both acceptable per spec, but never
    # IRRELEVANT.
    result = classify_title_relevance("Software Developer Projektmanagement Tools")
    assert result.level != "IRRELEVANT"


def test_python_engineer_projektmanagement_is_unknown_not_irrelevant():
    result = classify_title_relevance("Python Engineer Projektmanagement")
    assert result.level == "UNKNOWN"


def test_real_pilot_projektmanagement_vacancy_remains_irrelevant():
    title = "Berater im Projektmanagement im öffentlichen Sektor - Business Transformation (w/m/d)"
    result = classify_title_relevance(title)
    assert result.level == "IRRELEVANT"


# --- S11B-003: unchanged behaviors (regression guard) -----------------------


def test_personalcontroller_still_irrelevant_unchanged():
    result = classify_title_relevance("Personalcontroller (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "personalcontroller"


def test_administrator_still_irrelevant_unchanged():
    result = classify_title_relevance("MongoDB Administrator (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "administrator"


def test_systemadministrator_still_irrelevant_unchanged():
    result = classify_title_relevance("Systemadministrator (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "systemadministrator"


def test_presales_still_irrelevant_unchanged():
    result = classify_title_relevance("Presales Consultant (m/w/d) - Datacenter")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "presales"


# --- S11B-004: German feminine/hyphenated/connector variants ---------------


def test_qgis_expert_english_bare_is_irrelevant():
    assert classify_title_relevance("QGIS Expert").level == "IRRELEVANT"


def test_qgis_experte_spaced_is_irrelevant():
    result = classify_title_relevance("QGIS Experte")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_expertin_spaced_is_irrelevant():
    result = classify_title_relevance("QGIS Expertin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_hyphen_experte_is_irrelevant():
    result = classify_title_relevance("QGIS-Experte")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_hyphen_expertin_is_irrelevant():
    result = classify_title_relevance("QGIS-Expertin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_spazialist_spaced_is_irrelevant():
    result = classify_title_relevance("QGIS Spezialist")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_spezialistin_spaced_is_irrelevant():
    result = classify_title_relevance("QGIS Spezialistin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_hyphen_spezialist_is_irrelevant():
    result = classify_title_relevance("QGIS-Spezialist")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_qgis_hyphen_spezialistin_is_irrelevant():
    result = classify_title_relevance("QGIS-Spezialistin")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "qgis-expert"


def test_bare_qgis_alone_is_still_not_strong():
    # Regression guard: the hyphen-connector widening must not make bare
    # "QGIS" (no role word at all) a STRONG signal.
    result = classify_title_relevance("QGIS")
    assert result.level == "UNKNOWN"


def test_qgis_developer_remains_unknown_after_s11b_004():
    result = classify_title_relevance("QGIS Developer")
    assert result.level == "UNKNOWN"


def test_qgis_python_engineer_remains_unknown_after_s11b_004():
    result = classify_title_relevance("QGIS Python Engineer")
    assert result.level == "UNKNOWN"


def test_qgis_software_engineer_remains_unknown_after_s11b_004():
    result = classify_title_relevance("QGIS Software Engineer")
    assert result.level == "UNKNOWN"


def test_ingenieurin_elektrotechnik_is_irrelevant():
    result = classify_title_relevance("Ingenieurin Elektrotechnik (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "ingenieur-elektrotechnik"


def test_ingenieur_fuer_elektrotechnik_umlaut_is_irrelevant():
    result = classify_title_relevance("Ingenieur für Elektrotechnik (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "ingenieur-elektrotechnik"


def test_ingenieurin_fuer_elektrotechnik_umlaut_is_irrelevant():
    result = classify_title_relevance("Ingenieurin für Elektrotechnik (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "ingenieur-elektrotechnik"


def test_ingenieur_fuer_elektrotechnik_ascii_is_irrelevant():
    # "fuer" is the ASCII transliteration of "für" ("ü" -> "ue", a
    # two-character substitution, not "u" alone) -- common in real job
    # postings that avoid non-ASCII characters.
    result = classify_title_relevance("Ingenieur fuer Elektrotechnik (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "ingenieur-elektrotechnik"


def test_ingenieurin_fuer_elektrotechnik_ascii_is_irrelevant():
    result = classify_title_relevance("Ingenieurin fuer Elektrotechnik (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "ingenieur-elektrotechnik"


def test_software_engineer_elektrotechnik_remains_unknown_after_s11b_004():
    result = classify_title_relevance("Software Engineer Elektrotechnik")
    assert result.level == "UNKNOWN"


def test_python_engineer_elektrotechnik_remains_unknown_after_s11b_004():
    result = classify_title_relevance("Python Engineer Elektrotechnik")
    assert result.level == "UNKNOWN"


def test_beraterin_projektmanagement_is_irrelevant():
    result = classify_title_relevance("Beraterin Projektmanagement (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "berater-projektmanagement"


def test_beraterin_im_projektmanagement_is_irrelevant():
    result = classify_title_relevance("Beraterin im Projektmanagement (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "berater-projektmanagement"


def test_berater_im_projektmanagement_is_irrelevant():
    result = classify_title_relevance("Berater im Projektmanagement (m/w/d)")
    assert result.level == "IRRELEVANT"
    assert result.matched_signal == "berater-projektmanagement"


def test_software_developer_projektmanagement_tools_remains_not_irrelevant():
    result = classify_title_relevance("Software Developer Projektmanagement Tools")
    assert result.level != "IRRELEVANT"


def test_python_engineer_projektmanagement_remains_unknown_after_s11b_004():
    result = classify_title_relevance("Python Engineer Projektmanagement")
    assert result.level == "UNKNOWN"
