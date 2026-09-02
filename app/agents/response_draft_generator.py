"""Deterministic Stage 7C response-draft content generation: given a
Stage 7B classification plus already-trusted stored candidate/job facts,
produce a proposed reply subject/body — never send/draft/reply itself
(see app/services/response_draft.py's module docstring for the full hard
boundary this stage enforces).

**Why no LLM (mirrors app.agents.email_classifier's own rationale).**
Response text must be auditable, reproducible, and evidence-first, and
must never originate from or be steered by untrusted inbound email
content. This module deliberately accepts NO email subject/body/
from_address text as input at all — only already-validated, already-
trusted facts (a candidate name only if provenance-confirmed via
`is_top_level_fact_usable_for_generation`; a job title/company only from
an already-persisted, tracked `JobRecord`) ever reach the generated
text. This closes prompt-injection / content-injection risk by
construction: there is no LLM to steer, and no untrusted string is ever
concatenated into a template.

**Never invents.** A fact this module was not given (no confirmed
candidate name, no matched job) is represented as an explicit
`missing_fields` entry and a bracketed placeholder in the body text —
never guessed. Doubly true for anything the original email might have
asked for (specific documents, availability, salary expectations): this
module has no access to email content at all, so it can never
manufacture an answer to a question it never saw — the recruiter's
actual request is represented only generically ("the requested
information") and always flagged in `missing_fields` for a human to fill
in before the draft is ever sent.
"""

import re
from dataclasses import dataclass
from typing import Literal

Language = Literal["de", "en"]

# Classifications a deterministic, templated reply proposal makes sense
# for (spec: "Generate drafts only for classifications where a response
# makes sense"). Default-deny: every OTHER classification
# (APPLICATION_RECEIVED, REJECTION, WITHDRAWAL_OR_POSITION_CLOSED,
# AUTOMATED_NOTIFICATION, OTHER, UNKNOWN) is routed to an explicit
# NO_RESPONSE_RECOMMENDED result by app/services/response_draft.py rather
# than ever reaching this module's templates.
SUPPORTED_RESPONSE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "REQUEST_FOR_INFORMATION",
        "INTERVIEW_INVITATION",
        "INTERVIEW_RESCHEDULE",
        "OFFER",
        "GENERAL_RECRUITER_MESSAGE",
    }
)

# Bumped whenever template wording or the generation algorithm changes in
# a way that should produce a new draft revision for previously-generated
# messages, rather than being masked by the (gmail_message_id, analysis_id,
# candidate_profile_version, generator_version) idempotency identity — see
# app.db.models.ResponseDraftRecord's docstring.
#
# v1 -> v2 (Codex remediation, final blocker): the job-trust-laundering
# fix in app.services.response_draft (TRUSTED_JOB_SOURCES) changed what
# SAFE output looks like for a message matched to an untrusted-source
# (e.g. XING email-derived) job, but did not change any template text
# this module owns directly. generator_version is still bumped because
# it is part of ResponseDraftRecord's idempotency identity (see that
# model's docstring) — without the bump, a pre-fix v1 row already
# persisted for a given (gmail_message_id, analysis_id,
# candidate_profile_version) identity would keep being returned as-is by
# get_or_create_response_draft's idempotent lookup, silently masking the
# fix for every message analyzed before it shipped. Bumping forces a
# fresh, sanitized v2 revision instead, while the old v1 row remains
# queryable, unmodified, immutable history (never deleted/overwritten).
RESPONSE_DRAFT_GENERATOR_VERSION = "v2"
RESPONSE_DRAFT_PROVIDER = "deterministic_template"

_NO_JOB_PLACEHOLDER: dict[Language, str] = {
    "de": "[Position/Unternehmen unbekannt - bitte ergänzen]",
    "en": "[position/company unknown - please fill in]",
}
_NO_NAME_PLACEHOLDER: dict[Language, str] = {
    "de": "[Ihr Name]",
    "en": "[Your Name]",
}
_SALUTATION: dict[Language, str] = {
    "de": "Sehr geehrte Damen und Herren,",
    "en": "Dear Hiring Team,",
}
_SIGN_OFF: dict[Language, str] = {
    "de": "Mit freundlichen Grüßen",
    "en": "Best regards",
}

# Bounded, deterministic DE/EN heuristic for template-set selection only
# (never for interpreting email content as instructions) — counts known
# marker words; ties default to English. The counted text itself is
# never persisted or echoed into the generated draft.
_GERMAN_MARKER_PATTERN = re.compile(
    r"\b(?:und|der|die|das|f(?:ü|ue)r|mit|ich|sie|wir|bitte|vielen|dank|gerne|"
    r"stelle|bewerbung|gespr(?:ä|ae)ch|unternehmen|freuen|geehrte|herzlich|"
    r"termin|einladen|angebot)\b",
    re.IGNORECASE,
)
_ENGLISH_MARKER_PATTERN = re.compile(
    r"\b(?:the|and|for|with|please|thank|you|interview|position|application|"
    r"regards|team|company|role|offer|schedule)\b",
    re.IGNORECASE,
)


def detect_language(subject: str, body_plain: str) -> Language:
    """Which fixed DE/EN template set to render into — see module
    docstring for why this is the ONLY thing email content is ever used
    to decide, and never to supply text/facts directly.
    """
    text = f"{subject}\n{body_plain}"
    de_count = len(_GERMAN_MARKER_PATTERN.findall(text))
    en_count = len(_ENGLISH_MARKER_PATTERN.findall(text))
    return "de" if de_count > en_count else "en"


@dataclass(frozen=True)
class ResponseDraftContent:
    subject: str
    body: str
    language: Language
    missing_fields: tuple[str, ...]
    template_id: str


def _job_label(
    job_title: str | None, job_company: str | None, language: Language
) -> tuple[str, bool]:
    if job_title and job_company:
        return f"{job_title} ({job_company})", False
    if job_title:
        return job_title, False
    return _NO_JOB_PLACEHOLDER[language], True


# Each entry: classification -> language -> (subject_template, body_lines,
# extra_missing_fields). `{job}` is substituted with the resolved job
# label (a real "Title (Company)" string, or a bracketed placeholder —
# see `_job_label`). No other substitution ever happens: nothing here
# reads or echoes email content.
_TEMPLATES: dict[str, dict[Language, tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "REQUEST_FOR_INFORMATION": {
        "de": (
            "Re: {job} - Angeforderte Informationen",
            (
                "vielen Dank für Ihre Nachricht bezüglich {job}.",
                "Die von Ihnen angeforderten Informationen "
                "[PLATZHALTER: bitte die angeforderten Angaben ergänzen] "
                "sende ich Ihnen in Kürze zu.",
            ),
            ("the specific information the recruiter requested (not automatically extracted)",),
        ),
        "en": (
            "Re: {job} - Requested Information",
            (
                "thank you for your message regarding {job}.",
                "I will provide the requested information "
                "[PLACEHOLDER: please fill in the specific details requested] shortly.",
            ),
            ("the specific information the recruiter requested (not automatically extracted)",),
        ),
    },
    "INTERVIEW_INVITATION": {
        "de": (
            "Re: {job} - Einladung zum Vorstellungsgespräch",
            (
                "vielen Dank für die Einladung zum Vorstellungsgespräch für die Position {job}.",
                "Gerne nehme ich den Termin wahr. "
                "[PLATZHALTER: bitte einen passenden Termin/Uhrzeit bestätigen oder vorschlagen]",
            ),
            ("candidate availability / preferred interview time (not stored in this system)",),
        ),
        "en": (
            "Re: {job} - Interview Invitation",
            (
                "thank you for inviting me to interview for the {job} position.",
                "I would be happy to attend. "
                "[PLACEHOLDER: please confirm or propose a suitable date/time]",
            ),
            ("candidate availability / preferred interview time (not stored in this system)",),
        ),
    },
    "INTERVIEW_RESCHEDULE": {
        "de": (
            "Re: {job} - Terminverschiebung",
            (
                "vielen Dank für die Information zur Verschiebung des Gesprächstermins für {job}.",
                "[PLATZHALTER: bitte einen neuen passenden Termin bestätigen oder vorschlagen]",
            ),
            ("candidate availability for a new interview time (not stored in this system)",),
        ),
        "en": (
            "Re: {job} - Interview Reschedule",
            (
                "thank you for letting me know about rescheduling the interview for {job}.",
                "[PLACEHOLDER: please confirm or propose a new suitable date/time]",
            ),
            ("candidate availability for a new interview time (not stored in this system)",),
        ),
    },
    "OFFER": {
        "de": (
            "Re: {job} - Vielen Dank für das Angebot",
            (
                "vielen Dank für Ihr Angebot für die Position {job}.",
                "Ich werde die Details prüfen und mich in Kürze mit einer Rückmeldung bei "
                "Ihnen melden. "
                "[PLATZHALTER: Entscheidung zu Gehalt/Startdatum/Bedingungen noch ausstehend]",
            ),
            ("candidate's decision on salary/start date/offer terms (must not be auto-decided)",),
        ),
        "en": (
            "Re: {job} - Thank You for the Offer",
            (
                "thank you for offering me the {job} position.",
                "I will review the details and get back to you shortly with my decision. "
                "[PLACEHOLDER: decision on salary/start date/offer terms still pending]",
            ),
            ("candidate's decision on salary/start date/offer terms (must not be auto-decided)",),
        ),
    },
    "GENERAL_RECRUITER_MESSAGE": {
        "de": (
            "Re: {job}",
            (
                "vielen Dank für Ihre Nachricht. Ich melde mich in Kürze mit einer "
                "ausführlichen Rückmeldung bei Ihnen. "
                "[PLATZHALTER: bitte auf die spezifische Anfrage eingehen]",
            ),
            (
                "specific recruiter request (message content not automatically "
                "interpreted beyond its classification)",
            ),
        ),
        "en": (
            "Re: {job}",
            (
                "thank you for reaching out. I will get back to you shortly with a "
                "detailed response. "
                "[PLACEHOLDER: please address the specific request]",
            ),
            (
                "specific recruiter request (message content not automatically "
                "interpreted beyond its classification)",
            ),
        ),
    },
}


def generate_response_draft(
    *,
    classification: str,
    language: Language,
    candidate_name: str | None,
    job_title: str | None,
    job_company: str | None,
) -> ResponseDraftContent | None:
    """Pure, deterministic content generation. Returns `None` when
    `classification` is not in `SUPPORTED_RESPONSE_CLASSIFICATIONS` — the
    caller (app/services/response_draft.py) is responsible for turning
    that into a persisted `NO_RESPONSE_RECOMMENDED` result; this function
    itself never decides persistence.
    """
    templates_by_language = _TEMPLATES.get(classification)
    if templates_by_language is None:
        return None

    missing: list[str] = []
    job_label, job_missing = _job_label(job_title, job_company, language)
    if job_missing:
        missing.append(
            "matched job/company (no trusted tracked job identity is available for "
            "this message — either no job was matched, or the matched job's source "
            "is not trusted for use in generated text)"
        )
    signature = candidate_name or _NO_NAME_PLACEHOLDER[language]
    if candidate_name is None:
        missing.append("candidate name (not confirmed in candidate profile)")

    subject_template, body_line_templates, extra_missing = templates_by_language[language]
    missing.extend(extra_missing)

    subject = subject_template.format(job=job_label)
    body_lines = [line.format(job=job_label) for line in body_line_templates]
    body = "\n\n".join([_SALUTATION[language], *body_lines, _SIGN_OFF[language], signature])

    template_id = f"{classification}_{language.upper()}_{RESPONSE_DRAFT_GENERATOR_VERSION}"
    return ResponseDraftContent(
        subject=subject,
        body=body,
        language=language,
        missing_fields=tuple(missing),
        template_id=template_id,
    )
