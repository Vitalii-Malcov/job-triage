"""Tests for app.agents.email_classifier (Stage 7B) — German-primary +
English classification, clause-scoped negation, automated-message
detection, and classification-conflict handling.
"""

from app.agents.email_classifier import classify_email


def test_german_application_received():
    result = classify_email("Ihre Bewerbung", "Wir haben Ihre Bewerbung erhalten.", "hr@company.de")
    assert result.category == "APPLICATION_RECEIVED"
    assert result.confidence == "HIGH"
    assert result.evidence


def test_german_interview_invitation():
    result = classify_email(
        "Einladung",
        "Wir möchten Sie gerne zu einem Vorstellungsgespräch einladen.",
        "hr@company.de",
    )
    assert result.category == "INTERVIEW_INVITATION"
    assert result.confidence == "HIGH"


def test_german_rejection():
    result = classify_email(
        "Ihre Bewerbung",
        "Leider können wir Ihre Bewerbung nicht berücksichtigen.",
        "hr@company.de",
    )
    assert result.category == "REJECTION"
    assert result.confidence == "HIGH"


def test_german_request_for_information():
    result = classify_email(
        "Unterlagen",
        "Bitte senden Sie uns noch Ihre Zeugnisse zu.",
        "hr@company.de",
    )
    assert result.category == "REQUEST_FOR_INFORMATION"
    assert result.confidence == "HIGH"


def test_german_position_closed():
    result = classify_email(
        "Stellenausschreibung", "Die Stelle wurde bereits besetzt.", "hr@company.de"
    )
    assert result.category == "WITHDRAWAL_OR_POSITION_CLOSED"
    assert result.confidence == "HIGH"


def test_english_application_received():
    result = classify_email(
        "Your application",
        "We have received your application. Thank you for applying.",
        "hr@company.com",
    )
    assert result.category == "APPLICATION_RECEIVED"


def test_english_interview_invitation():
    result = classify_email(
        "Interview",
        "We would like to invite you to an interview next week.",
        "hr@company.com",
    )
    assert result.category == "INTERVIEW_INVITATION"


def test_english_rejection():
    result = classify_email(
        "Application update",
        "We regret to inform you that you have not been selected.",
        "hr@company.com",
    )
    assert result.category == "REJECTION"


def test_english_offer():
    result = classify_email(
        "Job Offer", "We are pleased to offer you the position.", "hr@company.com"
    )
    assert result.category == "OFFER"


def test_negated_rejection_phrase_is_not_classified_as_rejection():
    """'Dies ist keine Absage' must not classify as REJECTION merely
    because 'Absage' occurs — GMAIL-6B-style negation, clause-scoped so
    the genuine invitation in the same sentence still survives.
    """
    result = classify_email(
        "Update",
        "Dies ist keine Absage, wir laden Sie herzlich zu einem Vorstellungsgespräch ein.",
        "hr@company.de",
    )
    assert result.category == "INTERVIEW_INVITATION"
    assert all("REJECTION" not in item.kind for item in result.evidence)


def test_negated_interview_phrase_is_not_classified_as_invitation():
    result = classify_email(
        "Internal note",
        "Please note the interview is not required for this internal role update.",
        "hr@company.com",
    )
    assert result.category != "INTERVIEW_INVITATION"


def test_negation_does_not_suppress_unrelated_sentence():
    """Negation is clause-scoped, not email-wide — a genuine rejection in
    a later, unrelated sentence must still be detected.
    """
    result = classify_email(
        "Update",
        "Dies ist keine allgemeine Ankündigung. "
        "Leider können wir Ihre Bewerbung nicht berücksichtigen.",
        "hr@company.de",
    )
    assert result.category == "REJECTION"


def test_unknown_message():
    result = classify_email("Hey", "Want to grab lunch tomorrow?", "friend@gmail.com")
    assert result.category == "UNKNOWN"
    assert result.confidence == "LOW"
    assert result.evidence == ()


def test_automated_acknowledgement_via_sender_and_phrase():
    result = classify_email(
        "Auto-Reply",
        "This is an automated message. Your application status has changed.",
        "no-reply@ats.example.com",
    )
    assert result.category == "AUTOMATED_NOTIFICATION"
    assert result.confidence == "MEDIUM"
    assert result.is_automated is True


def test_no_reply_ats_email_can_still_be_application_received():
    """Spec: 'no-reply alone must not automatically override strong
    semantic evidence' — an automated ATS acknowledgement with an
    explicit application-received phrase stays APPLICATION_RECEIVED, not
    downgraded to AUTOMATED_NOTIFICATION; `is_automated` is a separate
    signal.
    """
    result = classify_email(
        "Bewerbungseingang",
        "Wir haben Ihre Bewerbung erhalten. Dies ist eine automatisch generierte Nachricht.",
        "no-reply@ats.example.com",
    )
    assert result.category == "APPLICATION_RECEIVED"
    assert result.is_automated is True


def test_no_reply_ats_email_can_still_be_rejection():
    result = classify_email(
        "Ihre Bewerbung",
        "Leider können wir Ihre Bewerbung nicht berücksichtigen.",
        "no-reply@ats.example.com",
    )
    assert result.category == "REJECTION"
    assert result.is_automated is True


def test_classification_conflict_resolves_to_other():
    """A genuine REJECTION alongside a positive-outcome category in the
    same email is a contradiction, not a guessable precedence — resolves
    to OTHER, LOW confidence, both fragments cited as evidence.
    """
    result = classify_email(
        "Update",
        "Leider können wir Ihre Bewerbung nicht berücksichtigen. "
        "Trotzdem laden wir Sie zu einem Vorstellungsgespräch ein.",
        "hr@company.de",
    )
    assert result.category == "OTHER"
    assert result.confidence == "LOW"
    assert len(result.evidence) >= 2


def test_reschedule_and_invitation_is_not_a_conflict():
    """A reschedule email that also still mentions the original
    invitation is a normal pipeline progression, not ambiguity —
    resolves via precedence to the more specific INTERVIEW_RESCHEDULE.
    """
    result = classify_email(
        "Terminänderung",
        "Wir möchten Sie gerne zu einem Vorstellungsgespräch einladen. "
        "Können wir den Termin für unser Gespräch verschieben?",
        "hr@company.de",
    )
    assert result.category == "INTERVIEW_RESCHEDULE"


def test_evidence_fragment_is_bounded():
    long_body = "Wir haben Ihre Bewerbung erhalten. " + ("Zusätzlicher Text. " * 50)
    result = classify_email("Subject", long_body, "hr@company.de")
    assert result.category == "APPLICATION_RECEIVED"
    for item in result.evidence:
        assert len(item.value) <= 163  # EVIDENCE_FRAGMENT_MAX_LENGTH + "..." allowance


def test_automated_sender_pattern_variants():
    for sender in (
        "no-reply@x.com",
        "donotreply@x.com",
        "mailer-daemon@x.com",
        "do-not-reply@x.com",
    ):
        result = classify_email("Subject", "just some text", sender)
        assert result.is_automated is True, sender


def test_general_recruiter_message_fallback():
    result = classify_email(
        "Karriere bei uns",
        "Wir freuen uns über Ihr Interesse an einer Position in unserem Unternehmen.",
        "karriere@company.de",
    )
    assert result.category == "GENERAL_RECRUITER_MESSAGE"
    assert result.confidence == "LOW"
