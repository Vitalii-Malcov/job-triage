"""Stage 23: which SPECIALIZATION a posting is about, and which requirements
it states in off-target technology domains.

The matcher scores literal skill overlap. It cannot tell a Python backend
role from "Embedded Linux", "SAP Consultant" or "Salesforce Admin": one shared
word (Linux, SQL, Testing) scores 100/100 or a neutral 50. This module is the
missing vocabulary, kept deliberately separate from the matcher so the cached
matcher analyses stay valid (no ALGORITHM_VERSION bump).

Rules (Stage 23 spec):
- REQUIRED / PREFERRED / UNKNOWN reuse the existing skill-extractor engine
  (`_contextual_segments`, `_classify_mention`): a technology is REQUIRED only
  when the title names it or the text marks it mandatory, PREFERRED when
  marked nice-to-have, UNKNOWN when merely mentioned.
- Absence of evidence is never evidence of a mismatch: this module only
  reports what the posting literally says.
- Pure functions, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.agents.skill_extractor import (
    _MUST_HAVE_MARKERS,
    _NICE_TO_HAVE_MARKERS,
    _PROFILE_REQUIREMENT_MARKERS,
    _classify_mention,
    _contextual_segments,
    _postfix_nice_scopes_all_mentions,
)

Importance = Literal["REQUIRED", "PREFERRED", "UNKNOWN"]

# Domains the candidate does not target (Junior Python backend / automation /
# QA / IT support). A domain here is a specialization, not a tool the
# candidate might simply lack.
OFF_TARGET_DOMAINS = ("EMBEDDED", "PLC", "SAP", "SALESFORCE", "PKI", "SALES")


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# domain -> (term, pattern). Literal mentions only.
_VOCABULARY: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "EMBEDDED": (
        ("embedded", _p(r"(?<!\w)embedded(?!\w)")),
        ("firmware", _p(r"(?<!\w)firmware(?!\w)")),
        ("microcontroller", _p(r"(?<!\w)(?:mikro|micro)-?controller\w*")),
        ("rtos", _p(r"(?<!\w)(?:rtos|freertos|zephyr|yocto)(?!\w)")),
        ("fpga", _p(r"(?<!\w)(?:fpga|vhdl|verilog)(?!\w)")),
        ("autosar", _p(r"(?<!\w)autosar(?!\w)")),
        ("can-bus", _p(r"(?<!\w)can[- ]?bus(?!\w)")),
    ),
    "PLC": (
        ("sps", _p(r"(?<![\w-])sps(?!\w)")),
        ("plc", _p(r"(?<!\w)plc(?!\w)")),
        ("tia portal", _p(r"(?<!\w)tia[- ]?portal(?!\w)")),
        ("step 7", _p(r"(?<!\w)(?:simatic|step\s?7|codesys|twincat)(?!\w)")),
        ("steuerungstechnik", _p(r"(?<!\w)steuerungs(?:technik|programmierung)\w*")),
        ("automatisierungstechnik", _p(r"(?<!\w)automatisierungstechni\w+")),
    ),
    "SAP": (
        ("sap", _p(r"(?<![\w-])sap(?![\w])")),
        ("abap", _p(r"(?<!\w)abap(?!\w)")),
        ("s/4hana", _p(r"(?<!\w)s/?4\s?hana(?!\w)")),
    ),
    "SALESFORCE": (("salesforce", _p(r"(?<!\w)sales\s?force(?!\w)")),),
    "PKI": (
        ("pki", _p(r"(?<!\w)pki(?!\w)")),
        ("certificate management", _p(r"(?<!\w)certificate\s+management(?!\w)")),
        ("hsm", _p(r"(?<!\w)hsm(?!\w)")),
    ),
    "SALES": (
        ("sales", _p(r"(?<!\w)sales(?!\w)")),
        ("vertrieb", _p(r"(?<!\w)(?:vertrieb\w*|aussendienst\w*|außendienst\w*)")),
        ("business development", _p(r"(?<!\w)business[- ]development(?!\w)")),
        ("account manager", _p(r"(?<!\w)account[- ]?manag\w+")),
    ),
    # Tools an IT-support / admin role may legitimately require. Reported but
    # never an off-target domain (the candidate targets IT support too).
    "WINDOWS_ADMIN": (
        ("windows", _p(r"(?<!\w)windows(?!\w)")),
        ("active directory", _p(r"(?<!\w)active\s+directory(?!\w)")),
        ("microsoft 365", _p(r"(?<!\w)(?:microsoft\s?365|m365|office\s?365|o365)(?!\w)")),
        ("intune", _p(r"(?<!\w)intune(?!\w)")),
        ("exchange", _p(r"(?<!\w)exchange\s+(?:server|online)(?!\w)")),
        ("vmware", _p(r"(?<!\w)(?:vmware|hyper-v)(?!\w)")),
        ("powershell", _p(r"(?<!\w)powershell(?!\w)")),
    ),
    "BI_ETL": (
        ("power bi", _p(r"(?<!\w)power\s?bi(?!\w)")),
        ("etl", _p(r"(?<!\w)etl(?!\w)")),
        ("tableau", _p(r"(?<!\w)tableau(?!\w)")),
        ("data warehouse", _p(r"(?<!\w)(?:data[- ]?warehouse|dwh)(?!\w)")),
    ),
    "CLOUD_DEVOPS": (
        ("aws", _p(r"(?<!\w)aws(?!\w)")),
        ("azure", _p(r"(?<!\w)azure(?!\w)")),
        ("gcp", _p(r"(?<!\w)(?:gcp|google\s+cloud)(?!\w)")),
        ("kubernetes", _p(r"(?<!\w)(?:kubernetes|k8s)(?!\w)")),
        ("terraform", _p(r"(?<!\w)terraform(?!\w)")),
        ("ansible", _p(r"(?<!\w)ansible(?!\w)")),
        ("ci/cd", _p(r"(?<!\w)ci\s*/\s*cd(?!\w)")),
    ),
    "CPP": (
        ("c++", _p(r"(?<!\w)c\+\+(?!\w)")),
        ("c#", _p(r"(?<!\w)c#(?!\w)")),
        (".net", _p(r"(?<![\w])\.net(?!\w)")),
        ("java", _p(r"(?<!\w)java(?![\w])")),
    ),
    # The candidate's own target stack.
    "TARGET_STACK": (
        ("python", _p(r"(?<!\w)python(?!\w)")),
        ("fastapi", _p(r"(?<!\w)fast[ -]?api(?!\w)")),
        ("django", _p(r"(?<!\w)django(?!\w)")),
        ("flask", _p(r"(?<!\w)flask(?!\w)")),
        ("pytest", _p(r"(?<!\w)pytest(?!\w)")),
        ("selenium", _p(r"(?<!\w)selenium(?!\w)")),
    ),
}

# A title naming any of these is a Python/backend/IT-support role whatever
# else it mentions ("Python Developer (SAP-Integration)").
_TARGET_TITLE = _p(
    r"python|back[- ]?end|fastapi|django|flask|webentwickl|web developer|selenium|"
    r"support|help[- ]?desk|service[- \s]*desk|(?:1st|2nd|first|second|[12])[- ]?level|"
    r"anwendungsbetreu|it-mitarbeiter|anwenderbetreu|test\s*autom|\bqa\b|"
    r"anwendungsentwickl|fachinformatiker|"
    r"quality[- ]assurance|software[- ]?test|tester|test[- ]engineer"
)
# Sales: only when the title is not also a technical role.
_SALES_TECHNICAL = _p(
    r"sales\s*engineer|vertriebs?ingenieur|presales|pre-sales|"
    r"entwickl|developer|software[- ]?(?:engineer|tester|test)|"
    r"programmier|administrator|\bit-|analy[st]|reporting|\bbi\b|"
    r"\bdat[ae]n?\b|data\s*(?:engineer|analyst)"
)


@dataclass(frozen=True)
class DomainMention:
    domain: str
    term: str
    importance: Importance
    in_title: bool = False


@dataclass(frozen=True)
class DomainProfile:
    title_domain: str | None  # off-target specialization named by the title
    title_term: str | None
    mentions: tuple[DomainMention, ...]

    def required(self, domain: str) -> list[DomainMention]:
        return [m for m in self.mentions if m.domain == domain and m.importance == "REQUIRED"]

    def has_required_target_stack(self) -> bool:
        """Python/backend stack that is not explicitly optional. A mention under an
        unrecognised heading ("Requirements:") counts: the safe direction is the
        operator, never an automatic SKIP."""
        return any(
            m.domain == "TARGET_STACK" and m.importance != "PREFERRED" for m in self.mentions
        )


def title_domain(title: str) -> tuple[str, str] | None:
    """The off-target specialization a TITLE names, or None.

    A title that also names the candidate's target stack or a generic software
    / support role is not off-target ("Python Developer, SAP-Integration").
    """
    if _TARGET_TITLE.search(title or ""):
        return None
    for domain in OFF_TARGET_DOMAINS:
        for term, pattern in _VOCABULARY[domain]:
            if pattern.search(title or ""):
                if domain == "SALES" and _SALES_TECHNICAL.search(title):
                    continue
                return domain, term
    return None


def extract_domain_profile(title: str, description: str) -> DomainProfile:
    """Every vocabulary mention with its importance (REQUIRED / PREFERRED /
    UNKNOWN). Title mentions are REQUIRED (they describe the role sought)."""
    found: dict[tuple[str, str], DomainMention] = {}
    rank = {"REQUIRED": 2, "UNKNOWN": 1, "PREFERRED": 0}  # evidence is never lost to "optional"

    def add(mention: DomainMention) -> None:
        key = (mention.domain, mention.term)
        previous = found.get(key)
        if previous is None or rank[mention.importance] > rank[previous.importance]:
            found[key] = mention

    for domain, terms in _VOCABULARY.items():
        for term, pattern in terms:
            if pattern.search(title or ""):
                add(DomainMention(domain, term, "REQUIRED", in_title=True))

    for segment, inherited in _contextual_segments(description or ""):
        must_markers = list(_MUST_HAVE_MARKERS.finditer(segment))
        if inherited == "must":
            must_markers.extend(_PROFILE_REQUIREMENT_MARKERS.finditer(segment))
        nice_markers = list(_NICE_TO_HAVE_MARKERS.finditer(segment))
        located = [
            (domain, term, m)
            for domain, terms in _VOCABULARY.items()
            for term, pattern in terms
            for m in pattern.finditer(segment)
        ]
        postfix_nice = _postfix_nice_scopes_all_mentions(
            segment, [m for _, _, m in located], must_markers
        )
        for domain, term, mention in located:
            verdict = _classify_mention(
                mention, must_markers, nice_markers, inherited, segment, postfix_nice
            )
            importance: Importance = (
                "REQUIRED" if verdict == "must" else "PREFERRED" if verdict == "nice" else "UNKNOWN"
            )
            add(DomainMention(domain, term, importance))

    named = title_domain(title)
    return DomainProfile(
        title_domain=named[0] if named else None,
        title_term=named[1] if named else None,
        mentions=tuple(sorted(found.values(), key=lambda m: (m.domain, m.term))),
    )
