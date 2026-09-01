"""Tests for app.services.email_matching (Stage 7B) — deterministic
job/application matching, precedence, ambiguity, and bounded evidence.
"""

from app.services.email_matching import (
    JobCandidate,
    ThreadPriorMatch,
    extract_company_domain,
    extract_sender_domain,
    match_email_to_job,
    normalize_company_name,
)


def _job(job_id, title, company, location="Berlin", url="https://example.com/jobs/1", status="NEW"):
    return JobCandidate(
        job_id=job_id, title=title, company=company, location=location, url=url, status=status
    )


def test_exact_application_reference_gives_high_confidence():
    jobs = [
        _job(
            1,
            "Python Developer",
            "Acme GmbH",
            url="https://acme.example.com/jobs/482173",
            status="APPLIED",
        ),
        _job(
            2,
            "Python Developer",
            "Other GmbH",
            url="https://other.example.com/careers/999",
            status="APPLIED",
        ),
    ]
    result = match_email_to_job(
        subject="Ihre Bewerbung Ref: 482173",
        body_plain="Vielen Dank für Ihre Bewerbung",
        from_address="hr@acme.example.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "APPLICATION"
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"
    assert any(item.kind == "JOB_REFERENCE" for item in result.evidence)


def test_exact_unique_company_and_job_combination():
    jobs = [_job(1, "Backend Engineer", "UniqueCo GmbH", status="NEW")]
    result = match_email_to_job(
        subject="Re: Backend Engineer bei UniqueCo",
        body_plain="UniqueCo GmbH bedankt sich für Ihre Bewerbung als Backend Engineer.",
        from_address="hr@somewhere-unrelated.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "JOB_ONLY"
    assert result.matched_job_id == 1


def test_same_title_across_multiple_companies_is_ambiguous():
    jobs = [
        _job(1, "Data Analyst Reporting", "FirstCo", url="https://firstco.example.com"),
        _job(2, "Data Analyst Reporting", "SecondCo", url="https://secondco.example.com"),
    ]
    result = match_email_to_job(
        subject="Re: Data Analyst Reporting",
        body_plain="Thanks for applying as Data Analyst Reporting.",
        from_address="hr@unrelated-domain.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert {c.job_id for c in result.candidates} == {1, 2}
    assert result.matched_job_id is None


def test_same_company_multiple_jobs_is_ambiguous():
    jobs = [
        _job(1, "Data Analyst", "BigCo GmbH", url="https://bigco.example.com/a"),
        _job(2, "Marketing Lead", "BigCo GmbH", url="https://bigco.example.com/b"),
    ]
    result = match_email_to_job(
        subject="Bewerbung bei BigCo",
        body_plain="BigCo GmbH bedankt sich für Ihre Bewerbung.",
        from_address="hr@unrelated-domain.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert {c.job_id for c in result.candidates} == {1, 2}


def test_generic_title_with_several_applications_is_ambiguous_not_guessed():
    """Spec's own worked example: five 'Python Developer' applications,
    a generic acknowledgement — no arbitrary winner.
    """
    jobs = [
        _job(
            i, "Python Developer", f"Company{i}", url=f"https://c{i}.example.com", status="APPLIED"
        )
        for i in range(1, 6)
    ]
    result = match_email_to_job(
        subject="Bewerbung",
        body_plain="Vielen Dank für Ihre Bewerbung als Python Developer.",
        from_address="hr@ats.example.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert len(result.candidates) == 5
    assert result.confidence == "LOW"  # weak/generic evidence only


def test_unrelated_email_is_unmatched():
    jobs = [_job(1, "Python Developer", "Acme GmbH")]
    result = match_email_to_job(
        subject="Lunch tomorrow?",
        body_plain="Want to grab lunch tomorrow?",
        from_address="friend@gmail.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "UNMATCHED"
    assert result.matched_job_id is None
    assert result.score == 0


def test_existing_trusted_thread_association_wins_over_composite_scoring():
    """A corroborated thread association (sender domain matches the
    associated job's own domain) wins outright at HIGH — see
    test_uncorroborated_thread_association_is_low_confidence below for
    the 7B-006 fix covering the NON-corroborated case.
    """
    jobs = [
        _job(1, "Python Developer", "Acme GmbH", status="APPLIED"),
        _job(
            3,
            "Totally Different Role",
            "SomeOtherCo",
            url="https://someotherco.example.com",
            status="APPLIED",
        ),
    ]
    result = match_email_to_job(
        subject="Re: Re: your application",
        body_plain="some unrelated follow-up text about scheduling",
        from_address="hr@someotherco.example.com",
        job_candidates=jobs,
        thread_prior_matches=[ThreadPriorMatch(job_id=3, match_type="APPLICATION")],
    )
    assert result.matched_job_id == 3
    assert result.match_type == "APPLICATION"
    assert result.confidence == "HIGH"
    assert result.evidence[0].kind == "THREAD_ASSOCIATION"


def test_uncorroborated_thread_association_is_low_confidence():
    """7B-006: plain thread membership alone (no company/domain/title/
    reference continuity signal in the CURRENT message) must not inherit
    HIGH trust — demoted to LOW, which forces human review.
    """
    jobs = [
        _job(
            3,
            "Totally Different Role",
            "SomeOtherCo",
            url="https://someotherco.example.com",
            status="APPLIED",
        )
    ]
    result = match_email_to_job(
        subject="Re: Re: your application",
        body_plain="some unrelated follow-up text about scheduling",
        from_address="hr@completely-unrelated-domain.com",
        job_candidates=jobs,
        thread_prior_matches=[ThreadPriorMatch(job_id=3, match_type="APPLICATION")],
    )
    assert result.matched_job_id == 3  # still informative
    assert result.confidence == "LOW"  # but not trusted
    assert result.evidence[0].kind == "THREAD_ASSOCIATION_UNCORROBORATED"


def test_ambiguous_thread_prior_matches_do_not_produce_false_association():
    """More than one distinct job_id among thread prior matches means the
    thread's own history is itself inconsistent — must not be trusted as
    a decisive association; falls through to composite scoring.
    """
    jobs = [_job(1, "Python Developer", "Acme GmbH", url="https://acme.example.com")]
    result = match_email_to_job(
        subject="Re: your application",
        body_plain="Acme GmbH follow-up",
        from_address="hr@acme.example.com",
        job_candidates=jobs,
        thread_prior_matches=[
            ThreadPriorMatch(job_id=1, match_type="APPLICATION"),
            ThreadPriorMatch(job_id=2, match_type="APPLICATION"),
        ],
    )
    assert result.evidence == () or result.evidence[0].kind != "THREAD_ASSOCIATION"


def test_cross_account_isolation_is_caller_responsibility_via_bounded_candidates():
    """The matcher itself has no notion of account_key — isolation is
    enforced by the repository layer only ever supplying job_candidates/
    thread_prior_matches already scoped to the requesting account (see
    test_gmail_analysis_repository.py for the DB-level isolation test).
    This test only documents/locks in that the matcher trusts its inputs
    as already-scoped and does not need its own account parameter.
    """
    import inspect

    from app.services.email_matching import match_email_to_job as fn

    assert "account_key" not in inspect.signature(fn).parameters


def test_free_mail_domain_does_not_produce_false_high_confidence():
    jobs = [_job(1, "Data Engineer", "SomeCo", url="https://someco.example.com")]
    result = match_email_to_job(
        subject="hi",
        body_plain="just checking in about the role",
        from_address="someone@gmail.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.confidence != "HIGH"
    assert not any(item.kind == "DOMAIN_COMPANY_MATCH" for item in result.evidence)


# ---------------------------------------------------------------------------
# 7B-001: explicit reference is a true precedence tier, not a weight that
# composite evidence could ever mathematically outscore.
# ---------------------------------------------------------------------------


def _max_composite_competitor(job_id, url="https://strongco.example.com"):
    """A candidate engineered to maximize every composite evidence kind
    simultaneously (company + domain + 4 distinctive title tokens +
    location) — the strongest possible composite score with NO explicit
    reference of its own.
    """
    return _job(
        job_id,
        "Alpha Beta Gamma Delta Specialist",
        "StrongCo GmbH",
        location="Berlin",
        url=url,
        status="APPLIED",
    )


def test_unique_job_id_beats_maximum_possible_composite_competitor():
    reference_job = _job(
        1,
        "Unrelated Title",
        "Unrelated Co",
        url="https://ref.example.com/jobs/482173",
        status="APPLIED",
    )
    competitor = _max_composite_competitor(2)
    result = match_email_to_job(
        subject="Job-ID: 482173",
        body_plain="StrongCo GmbH Alpha Beta Gamma Delta Specialist Berlin",
        from_address="hr@strongco.example.com",
        job_candidates=[reference_job, competitor],
        thread_prior_matches=[],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"
    assert any(item.kind == "JOB_REFERENCE" for item in result.evidence)


def test_referenz_nr_beats_composite_competitor():
    competitor = _max_composite_competitor(2)
    result = match_email_to_job(
        subject="Ihre Bewerbung",
        body_plain="Referenz-Nr: ABC123. StrongCo GmbH Alpha Beta Gamma Delta Specialist Berlin",
        from_address="hr@strongco.example.com",
        job_candidates=[
            _job(
                1,
                "Unrelated Title",
                "Unrelated Co",
                url="https://ref.example.com/ABC123",
                status="APPLIED",
            ),
            competitor,
        ],
        thread_prior_matches=[],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"


def test_kennziffer_beats_composite_competitor():
    competitor = _max_composite_competitor(2)
    result = match_email_to_job(
        subject="Ihre Bewerbung",
        body_plain="Kennziffer XYZ999. StrongCo GmbH Alpha Beta Gamma Delta Specialist Berlin",
        from_address="hr@strongco.example.com",
        job_candidates=[
            _job(
                1,
                "Unrelated Title",
                "Unrelated Co",
                url="https://ref.example.com/XYZ999",
                status="APPLIED",
            ),
            competitor,
        ],
        thread_prior_matches=[],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"


def test_stellen_nr_beats_composite_competitor():
    competitor = _max_composite_competitor(2)
    result = match_email_to_job(
        subject="Ihre Bewerbung",
        body_plain="Stellen-Nr: QRS777. StrongCo GmbH Alpha Beta Gamma Delta Specialist Berlin",
        from_address="hr@strongco.example.com",
        job_candidates=[
            _job(
                1,
                "Unrelated Title",
                "Unrelated Co",
                url="https://ref.example.com/QRS777",
                status="APPLIED",
            ),
            competitor,
        ],
        thread_prior_matches=[],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"


def test_same_explicit_reference_on_two_candidates_is_ambiguous():
    result = match_email_to_job(
        subject="Ihre Bewerbung Ref: SHARED42",
        body_plain="Vielen Dank",
        from_address="hr@unrelated.com",
        job_candidates=[
            _job(1, "Role A", "Company A", url="https://a.example.com/SHARED42"),
            _job(2, "Role B", "Company B", url="https://b.example.com/SHARED42"),
        ],
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert {c.job_id for c in result.candidates} == {1, 2}


def test_unresolved_explicit_reference_does_not_fabricate_a_winner():
    """A reference IS present in the email but matches no candidate — must
    not silently pick a winner from that reference; falls through to
    composite scoring (here: no composite evidence either -> UNMATCHED),
    and records that the reference was unresolved.
    """
    result = match_email_to_job(
        subject="Ihre Bewerbung Ref: NOTFOUND99",
        body_plain="Vielen Dank fuer Ihre Nachricht.",
        from_address="hr@unrelated.com",
        job_candidates=[
            _job(1, "Completely Unrelated", "Nope Inc", url="https://nope.example.com/other")
        ],
        thread_prior_matches=[],
    )
    assert result.match_type == "UNMATCHED"
    assert any(item.kind == "JOB_REFERENCE_UNRESOLVED" for item in result.evidence)
    assert not any(item.kind == "JOB_REFERENCE" for item in result.evidence)


# ---------------------------------------------------------------------------
# 7B-002: URL-aware reference extraction — matched only via URL-shaped
# substrings, never bare numbers.
# ---------------------------------------------------------------------------


def test_numeric_job_url_pasted_verbatim_in_email_is_matched():
    job = _job(1, "Some Role", "Some Co", url="https://jobs.example/roles/482173")
    competitor = _max_composite_competitor(2)
    result = match_email_to_job(
        subject="Your application",
        body_plain="Thanks for applying via https://jobs.example/roles/482173",
        from_address="hr@strongco.example.com",
        job_candidates=[job, competitor],
        thread_prior_matches=[],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"


def test_numeric_job_url_without_scheme_is_matched():
    job = _job(1, "Some Role", "Some Co", url="https://jobs.example/roles/482173")
    result = match_email_to_job(
        subject="Your application",
        body_plain="See jobs.example/roles/482173 for details.",
        from_address="hr@unrelated.com",
        job_candidates=[job],
        thread_prior_matches=[],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"


def test_bare_year_is_not_treated_as_job_reference():
    job = _job(1, "Some Role", "Some Co", url="https://jobs.example/roles/2026")
    result = match_email_to_job(
        subject="Newsletter",
        body_plain="We wish you a great start into 2026 and beyond.",
        from_address="friend@gmail.com",
        job_candidates=[job],
        thread_prior_matches=[],
    )
    assert result.match_type == "UNMATCHED"


def test_postal_code_is_not_treated_as_job_reference():
    job = _job(1, "Some Role", "Some Co", url="https://jobs.example/roles/60311")
    result = match_email_to_job(
        subject="Address update",
        body_plain="Our new office address: Musterstrasse 1, 60311 Frankfurt.",
        from_address="friend@gmail.com",
        job_candidates=[job],
        thread_prior_matches=[],
    )
    assert result.match_type == "UNMATCHED"


def test_phone_number_is_not_treated_as_job_reference():
    job = _job(1, "Some Role", "Some Co", url="https://jobs.example/roles/030123456")
    result = match_email_to_job(
        subject="Call us",
        body_plain="Please call us at 030123456 for questions.",
        from_address="friend@gmail.com",
        job_candidates=[job],
        thread_prior_matches=[],
    )
    assert result.match_type == "UNMATCHED"


def test_date_like_number_is_not_treated_as_job_reference():
    job = _job(1, "Some Role", "Some Co", url="https://jobs.example/roles/20260901")
    result = match_email_to_job(
        subject="Save the date",
        body_plain="Please save the date: 20260901 for our open house.",
        from_address="friend@gmail.com",
        job_candidates=[job],
        thread_prior_matches=[],
    )
    assert result.match_type == "UNMATCHED"


# ---------------------------------------------------------------------------
# 7B-006: thread trust must be corroborated — an attacker/unrelated sender
# referencing a legitimate thread root must not inherit HIGH trust.
# ---------------------------------------------------------------------------


def test_thread_spoof_attacker_does_not_inherit_high_trust():
    legitimate_job = _job(
        1,
        "Backend Engineer",
        "TrustedCo GmbH",
        url="https://trustedco.example.com/jobs/1",
        status="APPLIED",
    )
    result = match_email_to_job(
        subject="Re: your application",
        body_plain="Please see the attached documents regarding scheduling.",
        from_address="attacker@evil.example",
        job_candidates=[legitimate_job],
        thread_prior_matches=[ThreadPriorMatch(job_id=1, match_type="APPLICATION")],
    )
    assert result.matched_job_id == 1  # still surfaced as information
    assert result.confidence == "LOW"  # never HIGH purely from thread membership
    assert result.evidence[0].kind == "THREAD_ASSOCIATION_UNCORROBORATED"


def test_thread_spoof_with_genuine_corroboration_still_gets_high():
    """A legitimate reply that genuinely continues the same conversation
    (same company/domain) must still get HIGH — the fix must not
    over-correct into distrusting every reply.
    """
    legitimate_job = _job(
        1,
        "Backend Engineer",
        "TrustedCo GmbH",
        url="https://trustedco.example.com/jobs/1",
        status="APPLIED",
    )
    result = match_email_to_job(
        subject="Re: your application",
        body_plain="Following up on your application to TrustedCo GmbH.",
        from_address="hr@trustedco.example.com",
        job_candidates=[legitimate_job],
        thread_prior_matches=[ThreadPriorMatch(job_id=1, match_type="APPLICATION")],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"
    assert result.evidence[0].kind == "THREAD_ASSOCIATION"


# ---------------------------------------------------------------------------
# 7B-008: evidence fragments are bounded even for pathological inputs.
# ---------------------------------------------------------------------------


def test_evidence_fragment_is_bounded_even_for_pathological_title():
    from app.services.email_matching import EVIDENCE_FRAGMENT_MAX_LENGTH

    pathological_title = "A" * 500  # one giant unbroken "token"
    job = _job(1, pathological_title, "SomeCo")
    result = match_email_to_job(
        subject=pathological_title,
        body_plain="",
        from_address="hr@unrelated.com",
        job_candidates=[job],
        thread_prior_matches=[],
    )
    for item in result.evidence:
        assert len(item.value) <= EVIDENCE_FRAGMENT_MAX_LENGTH + 3  # + "..." allowance


def test_evidence_fragment_bounded_at_exact_and_plus_one_boundary():
    from app.services.email_matching import EVIDENCE_FRAGMENT_MAX_LENGTH, _truncate

    exact = "x" * EVIDENCE_FRAGMENT_MAX_LENGTH
    over = "x" * (EVIDENCE_FRAGMENT_MAX_LENGTH + 1)
    assert _truncate(exact) == exact
    assert len(_truncate(over)) == EVIDENCE_FRAGMENT_MAX_LENGTH + 3
    assert _truncate(over).endswith("...")


def test_total_ambiguous_response_size_is_globally_bounded():
    from app.services.email_matching import (
        EVIDENCE_FRAGMENT_MAX_LENGTH,
        MATCH_CANDIDATE_LIST_MAX_ITEMS,
        MATCH_EVIDENCE_MAX_ITEMS,
    )

    jobs = [
        _job(
            i,
            "Python Developer " + "x" * 200,
            f"Company{i}" + "y" * 200,
            url=f"https://c{i}.example.com",
        )
        for i in range(1, 30)
    ]
    result = match_email_to_job(
        subject="Bewerbung",
        body_plain="Vielen Dank für Ihre Bewerbung als Python Developer.",
        from_address="hr@ats.example.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert len(result.candidates) <= MATCH_CANDIDATE_LIST_MAX_ITEMS
    total_chars = sum(
        len(item.value)
        for candidate in result.candidates
        for item in candidate.evidence[:MATCH_EVIDENCE_MAX_ITEMS]
    )
    assert total_chars <= MATCH_CANDIDATE_LIST_MAX_ITEMS * MATCH_EVIDENCE_MAX_ITEMS * (
        EVIDENCE_FRAGMENT_MAX_LENGTH + 3
    )


# ---------------------------------------------------------------------------
# Ambiguity margin — near-ties resolve to AMBIGUOUS, not a guessed winner.
# ---------------------------------------------------------------------------


def test_near_tie_by_generic_token_count_is_ambiguous():
    """Two candidates sharing COMPANY_EXACT(35) but differing only by
    generic-token count (35+6=41 vs 35+4=39, a 2-point gap) — well within
    AMBIGUITY_SCORE_MARGIN — must resolve to AMBIGUOUS, not pick the
    marginally-higher-scoring one.
    """
    jobs = [
        _job(
            1,
            "Python Developer Senior Consultant",
            "SameCo GmbH",
            url="https://samecoA.example.com",
        ),
        _job(2, "Python Developer Senior", "SameCo GmbH", url="https://samecoB.example.com"),
    ]
    result = match_email_to_job(
        subject="Bewerbung bei SameCo",
        body_plain="SameCo GmbH bedankt sich fuer Ihre Bewerbung als "
        "Python Developer Senior Consultant.",
        from_address="hr@unrelated.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert {c.job_id for c in result.candidates} == {1, 2}


def test_near_tie_within_margin_is_ambiguous_not_decisive():
    """Two candidates whose only differentiator is a single generic title
    token (a few points, well within AMBIGUITY_SCORE_MARGIN) must resolve
    to AMBIGUOUS, not silently pick the marginally-higher scorer.
    """
    jobs = [
        _job(
            1,
            "Python Developer Senior",
            "Acme GmbH",
            location="Berlin",
            url="https://acme.example.com",
        ),
        _job(
            2, "Python Developer", "Acme GmbH", location="Berlin", url="https://acme2.example.com"
        ),
    ]
    result = match_email_to_job(
        subject="Bewerbung",
        body_plain="Vielen Dank fuer Ihre Bewerbung bei Acme GmbH in Berlin "
        "als Python Developer Senior.",
        from_address="hr@unrelated.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"


def test_strong_explicit_reference_beats_80_point_composite():
    strong_composite_competitor = _job(
        2,
        "Alpha Beta Gamma Delta",
        "MegaCo GmbH",
        location="Munich",
        url="https://megaco.example.com",
        status="APPLIED",
    )
    reference_job = _job(
        1, "Unrelated", "Other Inc", url="https://ref.example.com/REF555", status="APPLIED"
    )
    result = match_email_to_job(
        subject="Ref: REF555",
        body_plain="MegaCo GmbH Alpha Beta Gamma Delta Munich",
        from_address="hr@megaco.example.com",
        job_candidates=[reference_job, strong_composite_competitor],
        thread_prior_matches=[],
    )
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"


def test_unique_strong_company_and_title_beats_weak_alternatives():
    strong = _job(
        1,
        "Senior Backend Platform Architect",
        "VeryUniqueCo GmbH",
        url="https://veryuniqueco.example.com",
    )
    weak_alternatives = [
        _job(i, "Python Developer", f"Weak{i}", url=f"https://weak{i}.example.com")
        for i in range(2, 6)
    ]
    result = match_email_to_job(
        subject="Bewerbung",
        body_plain="VeryUniqueCo GmbH bedankt sich fuer Ihre Bewerbung als "
        "Senior Backend Platform Architect.",
        from_address="hr@unrelated.com",
        job_candidates=[strong, *weak_alternatives],
        thread_prior_matches=[],
    )
    assert result.match_type == "JOB_ONLY"
    assert result.matched_job_id == 1


def test_normalize_company_name_strips_legal_suffix_conservatively():
    assert normalize_company_name("ABC GmbH") == normalize_company_name("ABC")
    assert normalize_company_name("Acme AG") != normalize_company_name("Beta AG")


def test_extract_sender_domain_handles_malformed_input():
    assert extract_sender_domain(None) is None
    assert extract_sender_domain("not-an-email") is None
    assert (
        extract_sender_domain("Person <person@Example.COM>".split("<")[-1].rstrip(">"))
        == "example.com"
    )


def test_extract_company_domain_strips_www():
    assert extract_company_domain("https://www.acme.example.com/jobs/1") == "acme.example.com"
    assert extract_company_domain("acme.example.com") == "acme.example.com"


def test_evidence_and_candidate_lists_are_bounded():
    from app.services.email_matching import MATCH_CANDIDATE_LIST_MAX_ITEMS

    jobs = [
        _job(i, "Python Developer", f"Company{i}", url=f"https://c{i}.example.com")
        for i in range(1, 20)
    ]
    result = match_email_to_job(
        subject="Bewerbung",
        body_plain="Vielen Dank für Ihre Bewerbung als Python Developer.",
        from_address="hr@ats.example.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert len(result.candidates) <= MATCH_CANDIDATE_LIST_MAX_ITEMS
