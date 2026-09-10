"""Shared candidate-facing outbound-letter boilerplate for the two
deterministic, template-based letter generators: Stage 7C
(`app.agents.response_draft_generator`) and Stage 7E
(`app.agents.follow_up_generator`). Both produce a real "Title (Company)"
job label (or an explicit bracketed placeholder when either fact is
missing/untrusted) and share the same DE/EN salutation, sign-off, and
placeholder text -- consolidated after both modules had accumulated
byte-identical copies of this content, each independently. Neither
module's own classification/template logic lives here -- only the parts
that were genuinely, provably identical.
"""

from typing import Literal

Language = Literal["de", "en"]

NO_JOB_PLACEHOLDER: dict[Language, str] = {
    "de": "[Position/Unternehmen unbekannt - bitte ergänzen]",
    "en": "[position/company unknown - please fill in]",
}
NO_NAME_PLACEHOLDER: dict[Language, str] = {
    "de": "[Ihr Name]",
    "en": "[Your Name]",
}
SALUTATION: dict[Language, str] = {
    "de": "Sehr geehrte Damen und Herren,",
    "en": "Dear Hiring Team,",
}
SIGN_OFF: dict[Language, str] = {
    "de": "Mit freundlichen Grüßen",
    "en": "Best regards",
}


def resolve_job_label(
    job_title: str | None, job_company: str | None, language: Language
) -> tuple[str, bool]:
    """Returns `(label, is_placeholder)`. Named `resolve_*` rather than
    `job_label` deliberately -- both call sites assign their own local
    variable named `job_label` from this call's result, and Python treats
    a name assigned anywhere in a function as local for the function's
    entire scope; importing a same-named `job_label` function would make
    that assignment shadow the import and raise `UnboundLocalError` on the
    very call meant to produce it.
    """
    if job_title and job_company:
        return f"{job_title} ({job_company})", False
    if job_title:
        return job_title, False
    return NO_JOB_PLACEHOLDER[language], True
