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
from collections.abc import Iterable
from dataclasses import dataclass

from app.agents.role_relevance_classifier import classify_title_relevance

# Review (Architecture 3, "built for one person"): the target profession is a POLICY of
# this deployment, written down here once instead of being implied by regexes. The
# candidate's stored configuration (Stage 17: roles, search keywords) is the user's data
# and is never rewritten by it; where the two disagree the policy wins at search/ranking
# time (a stale stored "Application Support" cannot revive such titles -- Stage 24) and
# the disagreement is REPORTED (`configured_role_conflicts`, shown by
# `scripts/stage17_candidate_config.py show|apply` and logged when keywords are dropped).
TARGET_PROFESSION = "software development"

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
            r"\b(?:it|edv|user|anwender|anwendungs|applikations?|application|software|technical|technischer?|"
            r"desktop|field|client)[\s-]*support(?:er|erin|ern|erinnen)?\b|\bsupport[\s-]*(?:techniker|mitarbeiter|specialist|"
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
            r"\b\w*administrat(?:or|orin|oren|orinnen)\b|\b(?:it|system|netzwerk|network|server|windows|linux)"
            r"[\s-]*administration\b|\b(?:it|system|linux|unix|windows|datenbank|database|db|cloud|server|netzwerk|network)[\s-]?admins?\b|\bsysadmins?\b|\bsystemverwalt\w*",
        ),
        ("it-systemelektroniker", r"\b(?:it[\s-]*)?systemelektroniker(?:in|innen)?\b"),
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


# Gender markers ("(m/w/d)", "m/w/x") are noise; parenthesised ROLE words are kept.
_GENDER_MARKER = re.compile(
    r"\b[mwdfx](?:\s*[/|*:_]\s*[mwdfx]){1,3}\b|\b[mwdfx]\s*\*\s*[mwdfx]\b", re.IGNORECASE
)
_PARENTHESIS = re.compile(r"\(([^)]*)\)")
_GENDER_ONLY = re.compile(r"^(?:[mwdfx]|gn|divers|all|genders|[\*/|:_,\s])+$", re.IGNORECASE)
# The support/admin word names the PRODUCT being built ("... IT-Support Tools"), not the job.
# Whole words only; a developer-role phrase ("Software-Entwickler") is never a product.
_PRODUCT_NOUN = re.compile(
    r"(?:tools?|software|plattform(?:en)?|platforms?|systems?|systeme|portale?|anwendung(?:en)?|"
    r"applikation(?:en)?|l(?:ö|oe)sung(?:en)?|produkte?|apps?)\b",
    re.IGNORECASE,
)


def _strip_noise(title: str) -> str:
    """Drop gender markers; keep any other parenthesised text, set off by ';' so it can
    never make two words look adjacent."""
    text = _PARENTHESIS.sub(
        lambda m: " " if _GENDER_ONLY.match(m.group(1).strip()) else f" ; {m.group(1)} ; ",
        title or "",
    )
    return _GENDER_MARKER.sub(" ", text)


def hybrid_support_admin_family(title: str) -> str | None:
    """The support/administration family of a HYBRID title, or None.

    Hybrid = the title names a developer role AND an administration/support role term
    ("IT-Administrator / Softwareentwickler", "IT-Support für Softwareentwickler"). It is not
    excluded (see `support_admin_family`) but the main profession is unclear: the operator
    reads it and it is never prepared automatically. Fails safe: a false positive only
    costs a manual look, a false negative would let an admin job through. The one
    exemption is support that names the software being built ("Python Entwickler Service
    Desk Tools"): the term is directly followed by a product noun."""
    text = _strip_noise(title)
    if not _DEV_ROLE.search(text):
        return None
    for name, pattern in _FAMILIES:
        for match in pattern.finditer(text):
            tail = text[match.end() :].lstrip(" -")
            # support names the product only when a developer role comes FIRST
            if (
                _DEV_ROLE.search(text[: match.start()])
                and _PRODUCT_NOUN.match(tail)
                and not _DEV_ROLE.match(tail)
            ):
                continue
            return name
    return None


def is_developer_title(title: str) -> bool:
    """Title-only: does the title name a software-development role? Independent of the
    candidate's configured role priorities and never true for a support/administration
    title without a separate developer alternative."""
    return _DEV_ROLE.search(title or "") is not None


def is_support_admin_term(term: str) -> bool:
    """For configured search keywords: a search term that only finds support
    or administration vacancies ("Application Support", "Systemintegrator")."""
    return support_admin_family(term) is not None


@dataclass(frozen=True)
class RoleConflict:
    """One stored role/keyword (or one "/"-alternative of a role) the policy ignores."""

    source: str  # "primary_roles" | "secondary_roles" | "search_keywords"
    value: str  # the conflicting text (for a role: the alternative)
    family: str
    role: str | None = None  # the whole stored role when `value` is one alternative of it

    def describe(self) -> str:
        if self.source == "search_keywords":
            effect = f"excluded from the search queries ({TARGET_PROFESSION} target policy)"
        else:
            effect = (
                f"titles of this family are ranked off-role whatever this configured priority "
                f"says ({TARGET_PROFESSION} target policy)"
            )
        shown = f"{self.value!r}" + (f" (in {self.role!r})" if self.role else "")
        return f"{self.source}: {shown} is a {self.family} role -- {effect}"


def configured_role_conflicts(
    primary_roles: Iterable[str] = (),
    secondary_roles: Iterable[str] = (),
    search_keywords: Iterable[str] = (),
) -> list[RoleConflict]:
    """Stored roles / keywords that the policy excludes, in configuration order.
    A role's "/"-separated alternatives are checked one by one (ranking does the same);
    a keyword is checked as a whole (it is searched as one term). Pure: reads nothing,
    writes nothing, never changes the stored settings."""
    conflicts: list[RoleConflict] = []
    for source, values in (("primary_roles", primary_roles), ("secondary_roles", secondary_roles)):
        for role in values:
            for alternative in (part.strip() for part in role.split("/")):
                family = support_admin_family(alternative) if alternative else None
                if family is not None:
                    conflicts.append(
                        RoleConflict(
                            source, alternative, family, role=None if alternative == role else role
                        )
                    )
    for keyword in search_keywords:
        family = support_admin_family(keyword)
        if family is not None:
            conflicts.append(RoleConflict("search_keywords", keyword, family))
    return conflicts
