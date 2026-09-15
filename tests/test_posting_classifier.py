from app.agents.posting_classifier import classify_posting


def test_selbstaendigkeit_course_listing_excluded_by_default_no_freelance_preference():
    # alfatraining's real Stage 10 pilot false positive. SELBSTAENDIGKEIT
    # is NOT a course-specific type (see genuine-freelance test below) --
    # excluded here because no FREELANCE preference is granted, same as
    # AUSBILDUNG/PRAKTIKUM_TRAINEE without their matching preference.
    result = classify_posting(
        title="Programmierung mit Python",
        posting_type="SELBSTAENDIGKEIT",
        allowed_employment_types=frozenset({"APPRENTICESHIP", "INTERNSHIP"}),
    )
    assert result.is_target_employment is False
    assert result.excluded_reason == "posting_type_requires_preference:SELBSTAENDIGKEIT"


def test_python_advanced_training_listing_is_excluded_by_default():
    result = classify_posting(title="Python Advanced", posting_type="SELBSTAENDIGKEIT")
    assert result.is_target_employment is False


def test_genuine_freelance_role_allowed_with_freelance_preference():
    # Live-verified Stage 10 follow-up finding: SELBSTAENDIGKEIT is
    # Bundesagentur's real self-employed/freelance category (e.g.
    # commission-based "Handelsvertreter"/"Vertriebspartner" roles), not
    # a training-course-only marker. Must be reachable when the candidate
    # has explicitly opted into freelance work.
    result = classify_posting(
        title="Handelsvertreter (m/w/d)",
        posting_type="SELBSTAENDIGKEIT",
        allowed_employment_types=frozenset({"FREELANCE"}),
    )
    assert result.is_target_employment is True
    assert result.excluded_reason is None


def test_obvious_course_title_still_excluded_even_with_freelance_preference():
    # Defense in depth: the title-pattern check runs UNCONDITIONALLY,
    # after the posting_type/preference check, regardless of outcome --
    # even a candidate who has explicitly opted into freelance work must
    # not see an obvious course/training offering just because it happens
    # to carry SELBSTAENDIGKEIT and their preference permits that type.
    result = classify_posting(
        title="Python Weiterbildung für Fortgeschrittene",
        posting_type="SELBSTAENDIGKEIT",
        allowed_employment_types=frozenset({"FREELANCE"}),
    )
    assert result.is_target_employment is False
    assert result.excluded_reason == "course_title_pattern"


def test_selbstaendigkeit_does_not_globally_equate_to_non_employment():
    # A plain, non-course-titled SELBSTAENDIGKEIT posting is excluded only
    # for lack of preference, never tagged as a "non_employment" type.
    result = classify_posting(title="Vertriebspartner (m/w/d)", posting_type="SELBSTAENDIGKEIT")
    assert result.is_target_employment is False
    assert result.excluded_reason == "posting_type_requires_preference:SELBSTAENDIGKEIT"


def test_duales_studium_excluded_without_matching_preference():
    result = classify_posting(
        title="Duales Studium Informatik mit Ausrichtung Künstliche Intelligenz",
        posting_type="AUSBILDUNG",
    )
    assert result.is_target_employment is False
    assert result.excluded_reason == "posting_type_requires_preference:AUSBILDUNG"


def test_ausbildung_allowed_when_candidate_preference_permits_it():
    result = classify_posting(
        title="Ausbildung Fachinformatiker (m/w/d)",
        posting_type="AUSBILDUNG",
        allowed_employment_types=frozenset({"APPRENTICESHIP"}),
    )
    assert result.is_target_employment is True
    assert result.excluded_reason is None


def test_ausbildung_excluded_when_preferences_present_but_do_not_include_it():
    result = classify_posting(
        title="Ausbildung Fachinformatiker (m/w/d)",
        posting_type="AUSBILDUNG",
        allowed_employment_types=frozenset({"FULL_TIME"}),
    )
    assert result.is_target_employment is False


def test_praktikum_excluded_without_internship_preference():
    result = classify_posting(
        title="Praktikant Data Science (w/m/d)", posting_type="PRAKTIKUM_TRAINEE"
    )
    assert result.is_target_employment is False


def test_praktikum_allowed_with_internship_preference():
    result = classify_posting(
        title="Praktikant Data Science (w/m/d)",
        posting_type="PRAKTIKUM_TRAINEE",
        allowed_employment_types=frozenset({"INTERNSHIP"}),
    )
    assert result.is_target_employment is True


def test_arbeit_role_with_weiterbildung_in_title_is_not_excluded():
    # S10-002 (Codex Stage 10 review, BLOCKING): a real L&D-department
    # staff role -- "Weiterbildung" is the role's SUBJECT MATTER (the
    # employee works ON training), not an offer to sell a course. A
    # structurally-confirmed ARBEIT posting must never be excluded by the
    # weaker title-keyword fallback.
    result = classify_posting(title="Mitarbeiter Weiterbildung (m/w/d)", posting_type="ARBEIT")
    assert result.is_target_employment is True
    assert result.excluded_reason is None


def test_arbeit_role_with_schulung_in_title_is_not_excluded():
    result = classify_posting(title="Referent Schulung und Qualifizierung", posting_type="ARBEIT")
    assert result.is_target_employment is True


def test_arbeit_role_with_kurs_in_title_is_not_excluded():
    result = classify_posting(title="Leitung Kursplanung (m/w/d)", posting_type="ARBEIT")
    assert result.is_target_employment is True


def test_non_arbeit_course_title_is_still_excluded():
    # The ARBEIT bypass must not weaken exclusion for postings that
    # actually lack structural confirmation -- an untyped source with an
    # obvious course title is still caught.
    result = classify_posting(title="Python Weiterbildung für Fortgeschrittene", posting_type=None)
    assert result.is_target_employment is False
    assert result.excluded_reason == "course_title_pattern"


def test_legitimate_junior_python_backend_job_unaffected():
    result = classify_posting(title="Junior Python Backend Developer", posting_type="ARBEIT")
    assert result.is_target_employment is True
    assert result.excluded_reason is None


def test_company_name_with_educational_wording_does_not_cause_false_positive():
    # A REAL job (posting_type=ARBEIT) at a company whose name happens to
    # contain training/education vocabulary must not be excluded -- the
    # classifier only ever inspects title/posting_type, never company name.
    result = classify_posting(
        title="Python Backend Developer (m/w/d)",
        posting_type="ARBEIT",
    )
    assert result.is_target_employment is True


def test_title_only_course_keyword_fallback_for_missing_structured_type():
    # No structured posting_type available (e.g. a non-Bundesagentur
    # source) -- conservative title fallback still catches an obvious
    # course/training offering.
    result = classify_posting(title="Python Schulung für Einsteiger", posting_type=None)
    assert result.is_target_employment is False
    assert result.excluded_reason == "course_title_pattern"


def test_real_trainer_job_title_with_german_compound_is_not_false_positive():
    # "Schülerkurse" is one German compound word ("Schüler" + "kurse") --
    # the whole-word regex must not match "kurs" as a mid-word substring,
    # so a genuine paid trainer position isn't excluded just because its
    # title contains a compound built from a course-related root.
    result = classify_posting(
        title="Trainer für Schülerkurse in Teilzeit (m/w/d)",
        posting_type="ARBEIT",
    )
    assert result.is_target_employment is True


def test_unknown_posting_type_falls_back_to_title_check_only():
    result = classify_posting(title="Senior Backend Engineer", posting_type="SOMETHING_NEW")
    assert result.is_target_employment is True
