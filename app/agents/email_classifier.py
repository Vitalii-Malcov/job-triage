"""Deterministic classification of inbound recruiter/employer email
correspondence (Stage 7B).

**Why no LLM.** Mirrors app.agents.requirement_extractor's own rationale
(Stage 6B): classification here must be auditable, reproducible, and
evidence-first — every category assigned traces back to a literal,
bounded phrase match a human can read in `ClassificationResult.evidence`.
An LLM is never consulted; CLAUDE.md and the Stage 7B spec both require
that classification stay deterministic and never become the authority
for status interpretation.

**INFORMATION ONLY.** This module only reads `subject`/`body_plain`/
`from_address` strings and returns a `ClassificationResult` — it makes no
DB calls, no network calls, and has no side effects whatsoever. It never
follows a URL or opens an attachment referenced in the text; email
content is treated purely as untrusted correspondence text to
pattern-match against, never as instructions (see module docstring
convention shared with app/collectors/xing_email.py: phrases like "mark
this application rejected" appearing IN the email body are correspondence
content, not commands to this code).

**Negation, clause-scoped.** A single shared `_NEGATION_PATTERN`
(German + English) is checked per CLAUSE — the comma/semicolon/colon/
dash-delimited span immediately around one specific category match (see
`_clause_span`/`_CLAUSE_BOUNDARY_PATTERN`), not the whole sentence: if a
sentence contains both a negation ("Dies ist keine Absage") AND an
unrelated genuine match in a LATER clause ("...; wir laden Sie zu einem
Gespräch ein."), only the negated clause's own match is suppressed — the
genuine invitation survives. This is deliberately a single shared
negation pattern rather than one per category: "Dies ist keine Absage"
(negates REJECTION) and "Interview is not required" (negates
INTERVIEW_INVITATION) are both instances of the same general "keine/
nicht ... erforderlich/noun" negation shape, not two unrelated rules.

**Precedence over ambiguity.** When more than one of the seven specific
lifecycle categories matches (non-negated) in the same email, a
genuinely CONTRADICTORY pair (REJECTION alongside any positive-outcome
category — see `_CONTRADICTORY_WITH_REJECTION`) resolves to `OTHER`
(classification conflict, low confidence, flagged for human review) —
never guessed. A non-contradictory combination (e.g. an interview
reschedule email that also still says "invitation", or an offer email
that restates the original application-received acknowledgement) is a
normal, monotonic pipeline progression and resolves via
`_CATEGORY_PRECEDENCE` to the single most-specific/most-recent category,
not a conflict.
"""

import re
from dataclasses import dataclass
from typing import Literal

EmailCategory = Literal[
    "APPLICATION_RECEIVED",
    "REQUEST_FOR_INFORMATION",
    "INTERVIEW_INVITATION",
    "INTERVIEW_RESCHEDULE",
    "REJECTION",
    "OFFER",
    "WITHDRAWAL_OR_POSITION_CLOSED",
    "GENERAL_RECRUITER_MESSAGE",
    "AUTOMATED_NOTIFICATION",
    "OTHER",
    "UNKNOWN",
]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]

# Bounded so persisted evidence never approaches full-body size — a
# matched fragment is the sentence it was found in, hard-truncated here.
EVIDENCE_FRAGMENT_MAX_LENGTH = 160
CLASSIFICATION_EVIDENCE_MAX_ITEMS = 8

# Sentence/line boundary — same shape as
# app.agents.skill_extractor._SEGMENT_BOUNDARY, reused for the same
# reason: bound negation scope to one sentence, not the whole email.
_SENTENCE_BOUNDARY = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")

_NEGATION_PATTERN = re.compile(
    r"\bdies(?:e[rs]?)?\s+ist\s+keine?\b|"
    r"\bdas\s+ist\s+keine?\b|"
    r"\bthis\s+is\s+not\s+a\b|"
    r"\bnot\s+a\s+rejection\b|"
    r"\bnicht\s+(?:erforderlich|notwendig|zwingend)\b|"
    r"\bnot\s+(?:required|necessary|mandatory)\b|"
    r"\b(?:isn|aren)'?t\s+(?:required|necessary|mandatory)\b|"
    r"\bkeine?\b.{0,40}\b(?:erforderlich|notwendig)\b",
    re.IGNORECASE,
)

_AUTOMATED_SENDER_PATTERN = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-?daemon|automated|system)\b", re.IGNORECASE
)
_AUTOMATED_PHRASE_PATTERN = re.compile(
    r"diese\s+(?:e-?mail|nachricht)\s+wurde\s+automatisch\s+(?:generiert|erstellt|versendet)|"
    r"automatisch\s+generierte\s+nachricht|"
    r"bitte\s+antworten\s+sie\s+nicht\s+auf\s+diese\s+e-?mail|"
    r"this\s+is\s+an\s+automated\s+message|"
    r"this\s+email\s+was\s+sent\s+automatically|"
    r"do\s+not\s+reply\s+to\s+this\s+email|"
    r"automatically\s+generated",
    re.IGNORECASE,
)

_GENERAL_RECRUITER_SIGNAL_PATTERN = re.compile(
    r"bewerbung|stelle|position|vacancy|application|recruiting|karriere|career",
    re.IGNORECASE,
)

# Each of the seven lifecycle categories gets its own compiled
# alternation of German-primary + English-secondary phrases. Every entry
# is a whole-phrase/near-phrase pattern (not a bare keyword) so a single
# generic word can never classify a message on its own — mirrors this
# project's existing "no naive keyword substring checks" convention
# (see app.agents.requirement_extractor's module docstring).
_CATEGORY_PATTERNS: dict[EmailCategory, re.Pattern[str]] = {
    "OFFER": re.compile(
        r"wir\s+freuen\s+uns.{0,40}ihnen.{0,20}(?:die\s+stelle\s+)?anzubieten|"
        r"wir\s+m(?:ö|oe)chten\s+ihnen\s+die\s+(?:stelle|position)\s+anbieten|"
        r"vertragsangebot|"
        r"wir\s+bieten\s+ihnen\s+die\s+(?:stelle|position)\s+an|"
        r"we\s+are\s+pleased\s+to\s+offer\s+you|"
        r"we\s+would\s+like\s+to\s+offer\s+you\s+the\s+position|"
        r"job\s+offer|"
        r"offer\s+of\s+employment",
        re.IGNORECASE,
    ),
    "REJECTION": re.compile(
        r"\babsage\b|"
        r"leider\s+(?:k(?:ö|oe)nnen|m(?:ü|ue)ssen)\s+wir\s+ihre\s+bewerbung|"
        r"wir\s+haben\s+uns\s+(?:leider\s+)?f(?:ü|ue)r\s+eine[nr]?\s+anderen?\s+kandidat|"
        r"nicht\s+(?:weiter\s+)?ber(?:ü|ue)cksichtigen|"
        r"(?:leider\s+)?keine\s+zusage|"
        r"entscheidung\s+gegen\s+ihre\s+bewerbung|"
        r"unfortunately[, ]+.{0,20}(?:will\s+not|won't|cannot|decided\s+not\s+to)"
        r"\s+(?:move\s+forward|proceed)|"
        r"we\s+have\s+decided\s+to\s+move\s+forward\s+with\s+other\s+candidates|"
        r"we\s+regret\s+to\s+inform\s+you|"
        r"you\s+have\s+not\s+been\s+selected|"
        r"reject(?:ed|ion)\s+of\s+your\s+application",
        re.IGNORECASE,
    ),
    "INTERVIEW_RESCHEDULE": re.compile(
        r"termin.{0,20}verschieben|"
        r"neuen\s+termin\s+(?:vorschlagen|finden)|"
        r"gespr(?:ä|ae)ch.{0,20}verlegen|"
        r"k(?:ö|oe)nnen\s+wir\s+den\s+termin\s+(?:ä|ae)ndern|"
        r"reschedule|"
        r"new\s+interview\s+time|"
        r"move\s+(?:our|the)\s+interview|"
        r"change\s+(?:the|our)\s+interview\s+(?:date|time)",
        re.IGNORECASE,
    ),
    "INTERVIEW_INVITATION": re.compile(
        r"(?:wir\s+m(?:ö|oe)chten\s+sie\s+)?(?:gerne\s+)?zu\s+einem\s+"
        r"(?:vorstellungsgespr(?:ä|ae)ch|gespr(?:ä|ae)ch|interview)\s+einladen|"
        r"einladung\s+zum\s+vorstellungsgespr(?:ä|ae)ch|"
        r"gerne\s+laden\s+wir\s+sie\s+ein|"
        r"laden\s+(?:wir\s+)?sie\s+(?:gerne\s+|herzlich\s+)?zu\s+einem\s+"
        r"(?:vorstellungsgespr(?:ä|ae)ch|gespr(?:ä|ae)ch|interview)\s+ein\b|"
        r"terminvorschlag\s+f(?:ü|ue)r\s+ein\s+(?:gespr(?:ä|ae)ch|interview)|"
        r"we\s+would\s+like\s+to\s+invite\s+you\s+(?:for|to)\s+(?:an?\s+)?interview|"
        r"invite\s+you\s+to\s+(?:an?\s+)?interview|"
        r"schedule\s+an\s+interview|"
        r"interview\s+invitation",
        re.IGNORECASE,
    ),
    "WITHDRAWAL_OR_POSITION_CLOSED": re.compile(
        r"stelle\s+(?:wurde\s+)?(?:bereits\s+)?besetzt|"
        r"position\s+ist\s+nicht\s+mehr\s+verf(?:ü|ue)gbar|"
        r"(?:die\s+)?ausschreibung\s+wurde\s+zur(?:ü|ue)ckgezogen|"
        r"stelle\s+wurde\s+zur(?:ü|ue)ckgezogen|"
        r"position\s+(?:wurde\s+)?geschlossen|"
        r"position\s+has\s+been\s+filled|"
        r"position\s+is\s+no\s+longer\s+available|"
        r"job\s+posting\s+has\s+been\s+withdrawn|"
        r"role\s+has\s+been\s+closed",
        re.IGNORECASE,
    ),
    "REQUEST_FOR_INFORMATION": re.compile(
        r"bitte\s+senden\s+sie\s+uns|"
        r"k(?:ö|oe)nnten\s+sie\s+uns.{0,30}zusenden|"
        r"wir\s+ben(?:ö|oe)tigen\s+(?:noch\s+)?(?:weitere\s+)?unterlagen|"
        r"fehlende\s+unterlagen|"
        r"bitte\s+reichen\s+sie.{0,30}nach|"
        r"please\s+send\s+us|"
        r"could\s+you\s+(?:please\s+)?provide|"
        r"we\s+need\s+(?:some\s+)?additional\s+(?:information|documents)|"
        r"please\s+provide\s+the\s+following\s+documents",
        re.IGNORECASE,
    ),
    "APPLICATION_RECEIVED": re.compile(
        r"wir\s+haben\s+ihre\s+bewerbung\s+erhalten|"
        r"vielen\s+dank\s+f(?:ü|ue)r\s+ihre\s+bewerbung|"
        r"ihre\s+bewerbung\s+ist\s+bei\s+uns\s+eingegangen|"
        r"bewerbungseingang|"
        r"we\s+have\s+received\s+your\s+application|"
        r"thank\s+you\s+for\s+your\s+application|"
        r"your\s+application\s+has\s+been\s+received",
        re.IGNORECASE,
    ),
}

# Precedence order used only when >=2 non-contradictory categories match
# the same email (see module docstring) — highest first.
_CATEGORY_PRECEDENCE: tuple[EmailCategory, ...] = (
    "OFFER",
    "REJECTION",
    "INTERVIEW_RESCHEDULE",
    "INTERVIEW_INVITATION",
    "WITHDRAWAL_OR_POSITION_CLOSED",
    "REQUEST_FOR_INFORMATION",
    "APPLICATION_RECEIVED",
)

# REJECTION is the only category treated as unconditionally
# contradictory with every positive-outcome category — a message cannot
# simultaneously be a genuine rejection and a genuine offer/invitation;
# any other combination (e.g. RESCHEDULE + INVITATION, OFFER +
# APPLICATION_RECEIVED) is a normal monotonic pipeline progression, not
# ambiguity — see module docstring.
_CONTRADICTORY_WITH_REJECTION: frozenset[EmailCategory] = frozenset(
    {"OFFER", "INTERVIEW_INVITATION", "INTERVIEW_RESCHEDULE"}
)

# Categories consequential enough that this project's "consequential
# correspondence stays visible even at high confidence" bias (Stage 7B
# spec) applies — used by
# app.services.gmail_message_analysis.determine_requires_human_review.
#
# 7B-009 (Codex remediation round 1): REQUEST_FOR_INFORMATION was
# reproduced reaching HIGH confidence with requires_human_review=False —
# unsafe, since a recruiter asking for documents/availability/salary
# expectations/work authorization IS actionable correspondence a human
# needs to see, even though this project never acts on it automatically.
# GENERAL_RECRUITER_MESSAGE is included too, for the same "genuinely
# non-actionable only" bar (spec: "requires_human_review=False should be
# reserved for genuinely non-actionable informational mail where no user
# response/decision is implied") — explicit here rather than relying
# only on that category's classifier confidence happening to always be
# LOW (see classify_email's GENERAL_RECRUITER_MESSAGE branch), since a
# future classifier tuning change could otherwise silently reopen this
# gap. APPLICATION_RECEIVED, AUTOMATED_NOTIFICATION, and UNKNOWN remain
# excluded — genuinely non-actionable acknowledgements/unclassifiable
# mail, the only cases requires_human_review=False may still apply to
# (and even then only when match confidence is also non-LOW — see
# determine_requires_human_review).
CONSEQUENTIAL_CLASSIFICATIONS: frozenset[EmailCategory] = frozenset(
    {
        "OFFER",
        "INTERVIEW_INVITATION",
        "INTERVIEW_RESCHEDULE",
        "REJECTION",
        "WITHDRAWAL_OR_POSITION_CLOSED",
        "REQUEST_FOR_INFORMATION",
        "GENERAL_RECRUITER_MESSAGE",
        "OTHER",
    }
)


@dataclass(frozen=True)
class ClassificationEvidenceItem:
    kind: str
    value: str
    weight: int


@dataclass(frozen=True)
class ClassificationResult:
    category: EmailCategory
    confidence: Confidence
    evidence: tuple[ClassificationEvidenceItem, ...]
    is_automated: bool


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]


def _truncate(fragment: str) -> str:
    fragment = fragment.strip()
    if len(fragment) <= EVIDENCE_FRAGMENT_MAX_LENGTH:
        return fragment
    return fragment[:EVIDENCE_FRAGMENT_MAX_LENGTH].rstrip() + "..."


# Codex remediation round 1 (negation/punctuation hardening): a clause
# boundary is comma, semicolon, or colon, OR a dash/en-dash/em-dash
# SURROUNDED BY WHITESPACE (so "E-Mail"/"Vorstellungsgespräch-Termin"
# compound words are never mistaken for a clause break — only a real
# interpunction dash like " - "/" – "/" — " counts). Originally
# comma-only, which under-scoped negation for equally common German
# recruiter punctuation ("Dies ist keine Absage; wir laden Sie ein.").
_CLAUSE_BOUNDARY_PATTERN = re.compile(r"[,;:]|(?<=\s)[-–—](?=\s)")


def _clause_span(sentence: str, match: re.Match[str]) -> tuple[int, int]:
    """The delimited clause containing `match` — same technique as
    app.agents.requirement_extractor._clause_span (duplicated, not
    imported — that function is private to its own module and this is a
    few lines of arithmetic, not a decision), generalized from
    comma-only to `_CLAUSE_BOUNDARY_PATTERN`. Needed so "Dies ist keine
    Absage; wir laden Sie herzlich zu einem Vorstellungsgespräch ein."
    only suppresses REJECTION's own clause — the genuine
    INTERVIEW_INVITATION in a LATER clause of the SAME sentence must not
    be discarded merely because the sentence also contains a negation
    elsewhere in it, regardless of which punctuation mark separates them.
    """
    start = 0
    end = len(sentence)
    for boundary in _CLAUSE_BOUNDARY_PATTERN.finditer(sentence):
        if boundary.start() < match.start():
            start = boundary.end()
        elif boundary.start() >= match.end():
            end = boundary.start()
            break
    return start, end


def _matched_categories(text: str) -> dict[EmailCategory, str]:
    """Every specific category with at least one non-negated match,
    mapped to one bounded evidence fragment (the sentence containing the
    first matching, non-negated mention found — matches this project's
    existing "first match wins for evidence text" convention, e.g.
    extract_education_requirement). Negation is checked per CLAUSE
    (comma/semicolon/colon/dash-delimited span around the specific
    match, see `_CLAUSE_BOUNDARY_PATTERN`), not per whole sentence — see
    `_clause_span`.
    """
    matches: dict[EmailCategory, str] = {}
    for sentence in _sentences(text):
        for category, pattern in _CATEGORY_PATTERNS.items():
            if category in matches:
                continue
            mention = pattern.search(sentence)
            if mention is None:
                continue
            clause_start, clause_end = _clause_span(sentence, mention)
            clause = sentence[clause_start:clause_end]
            if _NEGATION_PATTERN.search(clause):
                continue
            matches[category] = sentence
    return matches


def _resolve_category(matches: dict[EmailCategory, str]) -> tuple[EmailCategory, Confidence, str]:
    matched = set(matches)
    if "REJECTION" in matched and matched & _CONTRADICTORY_WITH_REJECTION:
        conflicting = ", ".join(sorted(matched & ({"REJECTION"} | _CONTRADICTORY_WITH_REJECTION)))
        return "OTHER", "LOW", conflicting
    for category in _CATEGORY_PRECEDENCE:
        if category in matched:
            return category, "HIGH", matches[category]
    return "UNKNOWN", "LOW", ""


def _is_automated(from_address: str | None, text: str) -> bool:
    local_part = (from_address or "").split("@", 1)[0]
    if _AUTOMATED_SENDER_PATTERN.search(local_part):
        return True
    return bool(_AUTOMATED_PHRASE_PATTERN.search(text))


def classify_email(subject: str, body_plain: str, from_address: str | None) -> ClassificationResult:
    """Pure, deterministic classification — no DB/network access, no side
    effects. `subject`/`body_plain`/`from_address` are treated purely as
    untrusted correspondence text (see module docstring).
    """
    text = f"{subject}\n{body_plain}"
    matches = _matched_categories(text)
    category, confidence, evidence_fragment = _resolve_category(matches)
    is_automated = _is_automated(from_address, text)

    evidence: list[ClassificationEvidenceItem] = []
    if category == "OTHER":
        # Conflict: cite every contradicting category's own fragment
        # (bounded by CLASSIFICATION_EVIDENCE_MAX_ITEMS), not just one.
        conflicting_categories = {"REJECTION"} | (set(matches) & _CONTRADICTORY_WITH_REJECTION)
        for conflicting_category in sorted(conflicting_categories):
            evidence.append(
                ClassificationEvidenceItem(
                    kind=f"CONFLICTING_PHRASE:{conflicting_category}",
                    value=_truncate(matches[conflicting_category]),
                    weight=1,
                )
            )
    elif category != "UNKNOWN":
        evidence.append(
            ClassificationEvidenceItem(
                kind=f"PHRASE_MATCH:{category}", value=_truncate(evidence_fragment), weight=1
            )
        )
    else:
        automated_phrase_match = _AUTOMATED_PHRASE_PATTERN.search(text)
        if is_automated:
            category = "AUTOMATED_NOTIFICATION"
            confidence = "MEDIUM"
            fragment = (
                automated_phrase_match.group(0) if automated_phrase_match else (from_address or "")
            )
            evidence.append(
                ClassificationEvidenceItem(
                    kind="AUTOMATED_SIGNAL", value=_truncate(fragment), weight=1
                )
            )
        elif _GENERAL_RECRUITER_SIGNAL_PATTERN.search(text):
            category = "GENERAL_RECRUITER_MESSAGE"
            confidence = "LOW"
            signal_match = _GENERAL_RECRUITER_SIGNAL_PATTERN.search(text)
            evidence.append(
                ClassificationEvidenceItem(
                    kind="GENERAL_RECRUITER_SIGNAL",
                    value=_truncate(signal_match.group(0)) if signal_match else "",
                    weight=1,
                )
            )

    return ClassificationResult(
        category=category,
        confidence=confidence,
        evidence=tuple(evidence[:CLASSIFICATION_EVIDENCE_MAX_ITEMS]),
        is_automated=is_automated,
    )
