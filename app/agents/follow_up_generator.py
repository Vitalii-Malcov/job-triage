"""Deterministic Stage 7E follow-up content generation: given
already-trusted stored candidate/job facts, produce a proposed
candidate-initiated follow-up subject/body — never send/propose approval
itself (see app/services/follow_up.py's module docstring for the full
hard boundary this stage enforces).

**Why no LLM (mirrors app.agents.response_draft_generator's own
rationale).** Follow-up text must be auditable, reproducible, and
evidence-first. This module accepts NO email subject/body/from_address
text as input at all — only already-validated, already-trusted facts (a
candidate name only if provenance-confirmed, a job title/company only
from an already-persisted, tracked `JobRecord` whose `source` is
trusted) ever reach the generated text. There is exactly one template per
language — unlike Stage 7C, a follow-up is never conditioned on an
inbound classification, so no classification-keyed template table exists
here.

**Never invents.** A fact this module was not given (no confirmed
candidate name, no matched/trusted job) is represented as an explicit
`missing_fields` entry and a bracketed placeholder in the body text —
never guessed.
"""

from dataclasses import dataclass
from typing import Literal

Language = Literal["de", "en"]

FOLLOW_UP_GENERATOR_VERSION = "v1"
FOLLOW_UP_PROVIDER = "deterministic_template"

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
_SUBJECT_TEMPLATE: dict[Language, str] = {
    "de": "Nachfrage zu meiner Bewerbung - {job}",
    "en": "Following up on my application - {job}",
}
_BODY_LINES: dict[Language, tuple[str, ...]] = {
    "de": (
        "vor einiger Zeit habe ich mich auf die Position {job} beworben und wollte "
        "freundlich nachfragen, ob es bereits Neuigkeiten zum Stand meiner Bewerbung gibt.",
        "Über eine Rückmeldung würde ich mich sehr freuen.",
    ),
    "en": (
        "I recently applied for the {job} position and wanted to kindly follow up to "
        "ask whether there is any update on the status of my application.",
        "I would appreciate any feedback you can share.",
    ),
}


@dataclass(frozen=True)
class FollowUpContent:
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


def generate_follow_up_content(
    *,
    language: Language,
    candidate_name: str | None,
    job_title: str | None,
    job_company: str | None,
) -> FollowUpContent:
    """Pure, deterministic content generation — always succeeds (there is
    no classification-dependent "no response recommended" branch here;
    eligibility itself, computed upstream, is what gates whether a
    follow-up is proposed at all).
    """
    missing: list[str] = []
    job_label, job_missing = _job_label(job_title, job_company, language)
    if job_missing:
        missing.append(
            "matched job/company (no trusted tracked job identity is available for "
            "this follow-up — either no job was matched, or the matched job's source "
            "is not trusted for use in generated text)"
        )
    signature = candidate_name or _NO_NAME_PLACEHOLDER[language]
    if candidate_name is None:
        missing.append("candidate name (not confirmed in candidate profile)")

    subject = _SUBJECT_TEMPLATE[language].format(job=job_label)
    body_lines = [line.format(job=job_label) for line in _BODY_LINES[language]]
    body = "\n\n".join([_SALUTATION[language], *body_lines, _SIGN_OFF[language], signature])

    template_id = f"FOLLOW_UP_{language.upper()}_{FOLLOW_UP_GENERATOR_VERSION}"
    return FollowUpContent(
        subject=subject,
        body=body,
        language=language,
        missing_fields=tuple(missing),
        template_id=template_id,
    )
