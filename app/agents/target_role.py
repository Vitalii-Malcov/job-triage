"""Stage 24: the candidate's TARGET profession is software development
(Python / backend first). Technical IT support and system administration are
previous-work-experience material for the CV, not a target role.

`support_admin_family` answers one title-only question: does this title name
an IT-support / helpdesk / service-desk / administration role? The match is on
ROLE words in the title -- never on a stray "Support" in the description, and a
genuine software-development title ("Software Developer Support Tools") is
never flagged. Nothing here reads or writes the database.
"""

import re

from app.agents.role_relevance_classifier import classify_title_relevance

_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("helpdesk", r"\bhelp[\s-]?desk\b|\bservice[\s-]?desk\b|\bhotline\b"),
        (
            "support-level",
            r"\b(?:1st|2nd|3rd|first|second|third|1|2|3)[\s.-]*level\b|\blevel[\s-]?[123]\b"
            r"|\b[123]\.?\s*-?level\b",
        ),
        (
            "it-support",
            r"\b(?:it|edv|user|anwender|applikations?|application|software|technical|technischer?|"
            r"desktop|field)[\s-]*support(?:er|erin)?\b|\bsupport[\s-]*(?:techniker|mitarbeiter|specialist|"
            r"spezialist|analyst|engineer|agent)\w*",
        ),
        (
            "anwenderbetreuung",
            r"\banwenderbetreu\w*|\banwendungsbetreu\w*|\banwendersupport\b|\bit[\s-]?betreu\w*",
        ),
        (
            "it-service-techniker",
            r"\b(?:it[\s-]*)?(?:service|system|server|netzwerk|network|vor[\s-]?ort|außendienst)"
            r"[\s-]*(?:techniker|technician|mitarbeiter)\w*",
        ),
        ("it-techniker", r"\bit[\s-]?techniker\w*|\bit[\s-]?servicetechniker\w*"),
        (
            "administrator",
            r"\b\w*administrat(?:or|orin)\b|\b(?:it|system|netzwerk|network|server|windows|linux)"
            r"[\s-]*administration\b|\bit[\s-]?admin\b|\bsysadmin\b|\bsystemverwalt\w*",
        ),
        ("systemintegration", r"\bsystemintegrat(?:or|orin|ion)\b|\bit[\s-]?systemintegrat\w*"),
        # A technology name alone is no role: it needs an admin/support/consulting
        # role word next to it ("Microsoft 365 Administrator", "AD Spezialist").
        (
            "m365-ad",
            r"\b(?:microsoft[\s-]*365|m365|o365|active[\s-]+directory)[\s-]*"
            r"(?:administrator\w*|admin|consultant\w*|berater\w*|spezialist\w*|specialist\w*|"
            r"experte\w*|expert\w*|techniker\w*|support\w*|betreu\w*)\b"
            r"|\b(?:administrator\w*|consultant\w*|berater\w*|spezialist\w*|specialist\w*|"
            r"support\w*)[\s-]+(?:für\s+|fuer\s+|for\s+)?"
            r"(?:microsoft[\s-]*365|m365|o365|active[\s-]+directory)\b",
        ),
    )
)

# A real software-development role phrase in the title keeps it out of the
# family ("Software Developer IT-Support Tools", "Python Entwickler Service Desk
# Tools"). A bare technology word ("Python Systemadministrator") does not.
_DEV_ROLE = re.compile(
    r"\b(?:software|python|backend|back-end|web|fullstack|full-stack|frontend|anwendungs|"
    r"applikations|ki|ai)[\s-]*(?:development[\s-]+engineer|entwickler\w*|developer|engineer|"
    r"ingenieur\w*|programmier\w*|architekt\w*|architect)\b"
    r"|\bfull[\s-]?stack[\s-]+engineer\b|\bsoftwareingenieur\w*"
    r"|\b(?:software|anwendungs|applikations)entwicklungs?[\s-]*ingenieur\w*"
    r"|\bqa[\s-]+(?:automation|engineer|tester)\b|\btest[\s-]*automati[a-z]*[\s-]*(?:engineer|developer|ingenieur\w*|entwickler\w*)\b"
    r"|\btestautomatisierer(?:in)?\b|\bsoftware[\s-]?(?:tester|testing[\s-]?engineer|testingenieur\w*|test[\s-]?engineer)\b"
    r"|\bentwicklungsingenieur\w*|\bfachinformatiker\w*(?:\s*/\s*in\b|\s*\([^)]{1,12}\))?"
    r"\s+(?:f(?:ü|ue)r\s+)?anwendungsentwicklung"
    r"|\bentwickler(?:in)?\b|\bdeveloper\b|\bprogrammierer(?:in)?\b",
    re.IGNORECASE,
)


def support_admin_family(title: str) -> str | None:
    """The matched support/administration family, or None."""
    text = title or ""
    if classify_title_relevance(text).level == "RELEVANT":
        return None
    if _DEV_ROLE.search(text):
        # An explicit developer role word ("IT-Administrator / Softwareentwickler")
        # is a hybrid the operator reads; "... mit Entwicklungsaufgaben" is not.
        return None
    return next((name for name, pattern in _FAMILIES if pattern.search(text)), None)


def is_support_admin_term(term: str) -> bool:
    """For configured search keywords: a search term that only finds support
    or administration vacancies ("Application Support", "Systemintegrator")."""
    return support_admin_family(term) is not None
