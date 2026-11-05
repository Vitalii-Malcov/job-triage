"""Task 21: what the stored `Authentication-Results` of an inbound Gmail message may prove.

`From` is a header anyone can write. The only mechanism in this pipeline that says more is
the `Authentication-Results` header (RFC 8601) that the receiving mail server adds. It is
evidence only under strict conditions:

* Only the TOPMOST `Authentication-Results` header is read. For mail delivered to the mailbox by
  SMTP, the receiving server prepends its own header, so an attacker-supplied header (it travels
  inside the message) sits BELOW it and is ignored. A message whose topmost header is not from a
  trusted authserv-id gets no credit at all, even when a lower header claims to be trusted.
* The authserv-id must be EXACTLY one of `TRUSTED_AUTHSERV_IDS` (Gmail's own receiving service).
* Credit requires exactly one well-formed `dmarc=pass` result with exactly one `header.from`
  property equal to the (validated, canonical) domain of the stored `From` address, and exactly
  one `From` header on the message. `spf=pass` or `dkim=pass` alone are NOT credited: they can
  pass for a domain that has nothing to do with the visible `From`.
* The header is read with a bounded tokenizer that honours quoted strings and (nested, escaped)
  comments; text inside a quoted string or comment is never a result. Anything malformed is
  `UNVERIFIED`.

**Trust boundary (limit, not proof).** Nothing inside a message can prove that Google wrote the
topmost header: a party able to WRITE into the mailbox (IMAP APPEND, an import tool, the Gmail
API insert call) can place a forged first header. That capability is mailbox compromise, the same
accepted class as the full-access App Password in CLAUDE.md, and is outside what this evaluator
can defend. The trust is in Gmail's own receiving service for mail delivered to the mailbox, and
only INBOUND messages are credited.

`DMARC_ALIGNED` therefore means: the DOMAIN of the From address authorised the mail, as reported
by the trusted receiving service. It does NOT mean the person is who they claim to be, that the
display name is genuine, or that the domain's owner (or a lookalike domain's owner) is
trustworthy. Missing, malformed, oversized, ambiguous or untrusted evidence is `UNVERIFIED`; an
explicit DMARC failure is `FAILED`. Nothing here is ever "upgraded" later and nothing is guessed.
"""

import re
from collections.abc import Sequence
from enum import StrEnum

TRUSTED_AUTHSERV_IDS: frozenset[str] = frozenset({"mx.google.com"})

# Longest header value that is parsed at all (the stored value is already clamped by the IMAP
# provider; anything longer is not a header a receiving server wrote).
MAX_AUTH_RESULTS_LENGTH = 4096
_MAX_CLAUSES = 32
_MAX_TOKENS_PER_CLAUSE = 64

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# RFC 5322 folding (a line break followed by whitespace) is legitimate and is unfolded first;
# any line break that remains is header injection.
_FOLD_RE = re.compile(r"\r?\n[ \t]")
_LINE_BREAK_RE = re.compile(r"[\r\n]")
_RESULT_TOKEN_RE = re.compile(r"^(?P<method>[A-Za-z][A-Za-z0-9-]*)=(?P<result>[A-Za-z]+)$")
# A resinfo property: `ptype.property=value` (value non-empty, may be a quoted string), or the
# `reason=` token. Anything else in a DMARC clause (a stray `=`, `header . from`, an empty
# value) makes the whole clause malformed.
_PROPERTY_TOKEN_RE = re.compile(
    r"^(?P<ptype>[A-Za-z][A-Za-z0-9-]*)\.(?P<prop>[A-Za-z0-9_.-]+)=(?P<value>\S.*)$"
)
# Bare (unquoted) value: MIME token characters plus `@`; `=`, `/`, `:`, `,`, quotes etc. are
# not allowed.
_BARE_VALUE_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`{|}~@-]+$")
_REASON_TOKEN_RE = re.compile(r"^reason=\S.*$", re.IGNORECASE)
_AUTHSERV_VERSION_RE = re.compile(r"^\d{1,3}$")
_DOMAIN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class SenderAuth(StrEnum):
    DMARC_ALIGNED = "DMARC_ALIGNED"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"


SENDER_AUTH_VALUES: tuple[str, ...] = tuple(member.value for member in SenderAuth)

UNVERIFIED_SENDER_WARNING = (
    "The sender address is NOT authenticated: the From header is forgeable, and no trusted "
    "DMARC pass aligned with the From domain is recorded. Do not reply from this system; "
    "verify the sender by another channel and answer manually."
)
VERIFIED_SENDER_NOTE = (
    "DMARC passed for the From domain. This proves the domain authorised the mail, not who "
    "the person is."
)


def canonical_domain(value: str | None) -> str | None:
    """Validated lower-case ASCII domain, or None. One trailing root dot is accepted and
    removed; repeated dots, empty labels, over-long labels and anything that is not a plain
    ASCII hostname are rejected. Non-ASCII (internationalized) domains are NOT converted:
    the IDNA 2003 and 2008 mappings disagree (`faß` vs `fass`), so such a sender simply stays
    unverified. A punycode (`xn--`) label is compared as written, never decoded."""
    if not value or not value.isascii():
        return None
    candidate = value.strip()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    candidate = candidate.casefold()
    if len(candidate) > 253 or _DOMAIN_RE.match(candidate) is None:
        return None
    return candidate


def _domain_of(address: str | None) -> str | None:
    if not address or address.count("@") != 1:
        return None
    local, domain = address.rsplit("@", 1)
    if not local or any(ch.isspace() for ch in address):
        return None
    return canonical_domain(domain)


def _split_clauses(text: str) -> list[str] | None:
    """Split on top-level `;`, dropping comments. Quoted strings (with backslash escapes) are
    kept intact and may contain `;` or parentheses; comments nest and may contain escapes.
    Returns None when quotes or comments are unbalanced."""
    clauses: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote:
            current.append(ch)
            if ch == "\\":
                i += 1
                if i >= len(text):
                    return None
                current.append(text[i])
            elif ch == '"':
                in_quote = False
        elif depth:
            if ch == "\\":
                i += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == "(":
            depth = 1
            current.append(" ")
        elif ch == ")":
            return None
        elif ch == '"':
            in_quote = True
            current.append(ch)
        elif ch == ";":
            clauses.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if in_quote or depth:
        return None
    clauses.append("".join(current).strip())
    return clauses


def _tokens(clause: str) -> list[str] | None:
    """Whitespace tokens of one clause; quoted strings stay inside their token."""
    tokens: list[str] = []
    current: list[str] = []
    in_quote = False
    i = 0
    while i < len(clause):
        ch = clause[i]
        if in_quote:
            current.append(ch)
            if ch == "\\":
                i += 1
                if i >= len(clause):
                    return None
                current.append(clause[i])
            elif ch == '"':
                in_quote = False
        elif ch == '"':
            in_quote = True
            current.append(ch)
        elif ch in " \t":
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1
    if in_quote:
        return None
    if current:
        tokens.append("".join(current))
    return tokens if len(tokens) <= _MAX_TOKENS_PER_CLAUSE else None


def _complete_value(value: str) -> bool:
    """A reason/property value must be ONE complete token: a bare MIME-token value, or a single
    quoted string whose closing quote is the last character. Trailing syntax after a quoted
    string (`"ok"header.from=...`) is malformed, never ignored."""
    if not value.startswith('"'):
        return _BARE_VALUE_RE.match(value) is not None
    i = 1
    while i < len(value):
        if value[i] == "\\":
            i += 2
            continue
        if value[i] == '"':
            return i == len(value) - 1
        i += 1
    return False


def _unquote(value: str) -> str | None:
    """A property value: a bare token, or a simple quoted string without escapes."""
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            return None
        inner = value[1:-1]
        return None if '"' in inner or "\\" in inner else inner
    return value


def evaluate_sender_authentication(
    authentication_results: Sequence[str],
    from_address: str | None,
    *,
    from_header_count: int = 1,
    trusted_authserv_ids: frozenset[str] = TRUSTED_AUTHSERV_IDS,
) -> SenderAuth:
    """`authentication_results`: every `Authentication-Results` header value in the message's
    own order, topmost first. `from_header_count`: how many `From` headers the message has
    (anything but exactly one is ambiguous). Never raises."""
    try:
        return _evaluate(
            authentication_results, from_address, from_header_count, trusted_authserv_ids
        )
    except Exception:  # noqa: BLE001 - fail closed on anything unexpected
        return SenderAuth.UNVERIFIED


def _evaluate(
    headers: Sequence[str],
    from_address: str | None,
    from_header_count: int,
    trusted: frozenset[str],
) -> SenderAuth:
    from_domain = _domain_of(from_address)
    if from_header_count != 1 or from_domain is None or not headers:
        return SenderAuth.UNVERIFIED
    topmost = headers[0]
    if not isinstance(topmost, str) or len(topmost) > MAX_AUTH_RESULTS_LENGTH:
        return SenderAuth.UNVERIFIED
    topmost = _FOLD_RE.sub(" ", topmost)
    if _CONTROL_CHARS_RE.search(topmost) or _LINE_BREAK_RE.search(topmost):
        return SenderAuth.UNVERIFIED
    clauses = _split_clauses(topmost)
    if clauses is None or len(clauses) > _MAX_CLAUSES:
        return SenderAuth.UNVERIFIED

    authserv = _tokens(clauses[0])
    if not authserv or len(authserv) > 2 or authserv[0].casefold() not in trusted:
        return SenderAuth.UNVERIFIED
    if len(authserv) == 2 and _AUTHSERV_VERSION_RE.match(authserv[1]) is None:
        return SenderAuth.UNVERIFIED

    dmarc: list[tuple[str, str | None]] = []
    for clause in clauses[1:]:
        if not clause:
            continue
        tokens = _tokens(clause)
        if not tokens:
            return SenderAuth.UNVERIFIED  # malformed clause: no partial credit
        head = _RESULT_TOKEN_RE.match(tokens[0])
        if head is None:
            return SenderAuth.UNVERIFIED
        if head.group("method").casefold() != "dmarc":
            continue
        header_from: list[str] = []
        for token in tokens[1:]:
            if _REASON_TOKEN_RE.match(token):
                if not _complete_value(token.split("=", 1)[1]):
                    return SenderAuth.UNVERIFIED
                continue
            prop = _PROPERTY_TOKEN_RE.match(token)
            if prop is None or not _complete_value(prop.group("value")):
                return SenderAuth.UNVERIFIED  # unconsumed / malformed token: no partial credit
            if (
                prop.group("ptype").casefold() == "header"
                and prop.group("prop").casefold() == "from"
            ):
                header_from.append(prop.group("value"))
        if len(header_from) != 1:
            return SenderAuth.UNVERIFIED
        dmarc.append((head.group("result").casefold(), _unquote(header_from[0])))
    if len(dmarc) != 1:
        return SenderAuth.UNVERIFIED
    result, header_from_value = dmarc[0]
    if result == "fail":
        return SenderAuth.FAILED
    if result != "pass":
        return SenderAuth.UNVERIFIED
    if canonical_domain(header_from_value) != from_domain:
        return SenderAuth.UNVERIFIED
    return SenderAuth.DMARC_ALIGNED


def sender_auth_warning(value: str | None) -> str:
    """Operator-facing text for a stored status; unknown/legacy (None) is unverified."""
    return VERIFIED_SENDER_NOTE if value == SenderAuth.DMARC_ALIGNED else UNVERIFIED_SENDER_WARNING
