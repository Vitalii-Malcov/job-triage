"""Stage 15: deterministic meaning of one inbound application e-mail.

Builds on -- never duplicates -- the Stage 7B classifier
(`app.agents.email_classifier.classify_email`), which owns the phrase
patterns, clause-scoped negation and the REJECTION-vs-positive conflict
rule. This module only adds what the application lifecycle needs:

1. **Normalization** before classification: HTML bodies are reduced to
   text, quoted reply history is cut off ("Am ... schrieb ...:", "On ...
   wrote:", "-----Original Message-----", lines starting with ">") so text
   the CANDIDATE wrote earlier in a thread can never classify the
   employer's new message, and reply/forward subject prefixes are removed.
2. **Two refinements** the Stage 7B vocabulary does not have:
   - TECHNICAL_INTERVIEW_INVITATION: an interview invitation that names a
     technical/coding format, or an explicit "Einladung zum technischen
     Interview / Fachgespräch".
   - UNDER_REVIEW_NOTICE: an explicit present-tense "your application is
     being reviewed" message -- only when NO Stage 7B lifecycle category
     matched. A receipt that also says the application is "in Bearbeitung"
     stays RECEIPT_CONFIRMATION; a future-tense "sobald wir Ihre Unterlagen
     geprüft haben" is not a review notice.
3. A **closed mapping** to `ResponseClassification`. Stage 7B categories
   with no lifecycle meaning (position closed, general recruiter mail,
   automated notices, conflicts) become OTHER_APPLICATION_MESSAGE or
   UNKNOWN and therefore never change a status by themselves.

Pure: no DB, no network, no LLM. E-mail text is untrusted data, never
instructions.
"""

import re
import unicodedata
from dataclasses import dataclass

from app.agents.email_classifier import Confidence, EmailCategory, classify_email
from app.models.application_lifecycle import ResponseClassification as R
from app.utils.html_text import html_to_text

_HTML_HINT = re.compile(r"<\s*(?:html|body|div|p|br|table|span|td)\b", re.IGNORECASE)
_QUOTE_HEADER = re.compile(
    r"^\s*(?:"
    r"am\s.{3,120}\sschrieb\s.{0,200}:|"
    r"on\s.{3,120}\swrote:|"
    r"-{2,}\s*(?:original\s+message|urspr(?:ü|ue)ngliche\s+nachricht)\s*-{2,}"
    r")\s*$",
    re.IGNORECASE,
)
_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|aw|antw|wg|fw|fwd|tr|sv|vs)\s*(?:\[\d+\])?\s*:\s*)+", re.I
)

_TECHNICAL_MARKER = re.compile(
    r"technische[nmrs]?\s+(?:interview|gespr(?:ä|ae)ch|vorstellungsgespr(?:ä|ae)ch|test)|"
    r"fachgespr(?:ä|ae)ch|"
    r"fachliche[nmrs]?\s+(?:interview|gespr(?:ä|ae)ch)|"
    r"coding[- ]?(?:challenge|interview|aufgabe|test|session)|"
    r"live[- ]?coding|"
    r"technical\s+(?:interview|assessment|round|screening)",
    re.IGNORECASE,
)
_TECHNICAL_INVITATION = re.compile(
    r"einladung\s+zu[mr]?\s+(?:einem\s+)?(?:technischen|fachlichen)\s+"
    r"(?:interview|gespr(?:ä|ae)ch)|"
    r"einladung\s+zu[mr]?\s+(?:einem\s+)?fachgespr(?:ä|ae)ch|"
    r"invite\s+you\s+to\s+a\s+technical\s+(?:interview|assessment)|"
    r"technical\s+interview\s+invitation",
    re.IGNORECASE,
)
_UNDER_REVIEW = re.compile(
    r"ihre\s+bewerbung\s+(?:befindet\s+sich|ist)\s+(?:derzeit\s+|aktuell\s+|momentan\s+|noch\s+)?"
    r"in\s+(?:der\s+)?(?:pr(?:ü|ue)fung|sichtung|bearbeitung)|"
    r"wir\s+pr(?:ü|ue)fen\s+(?:derzeit|aktuell|momentan|gerade)\s+ihre\s+"
    r"(?:bewerbung|unterlagen|bewerbungsunterlagen)|"
    r"your\s+application\s+is\s+(?:currently\s+)?(?:under\s+review|being\s+reviewed)|"
    r"we\s+are\s+(?:currently\s+)?reviewing\s+your\s+application",
    re.IGNORECASE,
)

_MAPPING: dict[EmailCategory, R] = {
    "APPLICATION_RECEIVED": R.RECEIPT_CONFIRMATION,
    "REQUEST_FOR_INFORMATION": R.REQUEST_FOR_INFORMATION,
    "INTERVIEW_INVITATION": R.INTERVIEW_INVITATION,
    "INTERVIEW_RESCHEDULE": R.INTERVIEW_INVITATION,
    "REJECTION": R.REJECTION,
    "OFFER": R.OFFER,
    "WITHDRAWAL_OR_POSITION_CLOSED": R.OTHER_APPLICATION_MESSAGE,
    "GENERAL_RECRUITER_MESSAGE": R.OTHER_APPLICATION_MESSAGE,
    "AUTOMATED_NOTIFICATION": R.OTHER_APPLICATION_MESSAGE,
    "OTHER": R.OTHER_APPLICATION_MESSAGE,
    "UNKNOWN": R.UNKNOWN,
}
_NO_LIFECYCLE_CATEGORY = frozenset(
    {"UNKNOWN", "GENERAL_RECRUITER_MESSAGE", "AUTOMATED_NOTIFICATION"}
)
_RULE_FRAGMENT_LENGTH = 120


@dataclass(frozen=True)
class ResponseClassificationResult:
    classification: R
    confidence: Confidence
    base_category: EmailCategory
    rule: str
    evidence_fragment: str


def normalize_subject(subject: str | None) -> str:
    return _SUBJECT_PREFIX.sub("", unicodedata.normalize("NFC", subject or "")).strip()


def strip_quoted_history(body: str) -> str:
    kept: list[str] = []
    for line in body.splitlines():
        if _QUOTE_HEADER.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def normalize_body(body: str | None, *, has_html: bool = False) -> str:
    text = unicodedata.normalize("NFC", body or "")
    if has_html or _HTML_HINT.search(text):
        text = html_to_text(text)
    text = strip_quoted_history(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _fragment(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 40)
    return text[start : match.end() + 40].strip()[:_RULE_FRAGMENT_LENGTH]


def classify_application_response(
    subject: str | None, body: str | None, from_address: str | None, *, has_html: bool = False
) -> ResponseClassificationResult:
    clean_subject = normalize_subject(subject)
    clean_body = normalize_body(body, has_html=has_html)
    base = classify_email(clean_subject, clean_body, from_address)
    fragment = base.evidence[0].value if base.evidence else ""
    rule = base.evidence[0].kind if base.evidence else f"BASE:{base.category}"
    classification = _MAPPING[base.category]
    text = f"{clean_subject}\n{clean_body}"

    if classification == R.INTERVIEW_INVITATION:
        marker = _TECHNICAL_MARKER.search(text)
        if marker:
            return ResponseClassificationResult(
                R.TECHNICAL_INTERVIEW_INVITATION,
                base.confidence,
                base.category,
                "PHRASE_MATCH:INTERVIEW_INVITATION+TECHNICAL_MARKER",
                _fragment(text, marker),
            )
    if base.category in _NO_LIFECYCLE_CATEGORY:
        technical = _TECHNICAL_INVITATION.search(text)
        if technical:
            return ResponseClassificationResult(
                R.TECHNICAL_INTERVIEW_INVITATION,
                "HIGH",
                base.category,
                "STAGE15_PHRASE:TECHNICAL_INTERVIEW_INVITATION",
                _fragment(text, technical),
            )
        review = _UNDER_REVIEW.search(text)
        if review:
            return ResponseClassificationResult(
                R.UNDER_REVIEW_NOTICE,
                "HIGH",
                base.category,
                "STAGE15_PHRASE:UNDER_REVIEW_NOTICE",
                _fragment(text, review),
            )
    return ResponseClassificationResult(
        classification, base.confidence, base.category, rule, fragment[:_RULE_FRAGMENT_LENGTH]
    )
