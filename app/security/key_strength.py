"""Credential strength rules shared by Settings validation, auth and diagnostics.

Audit SEC-02 / SEC-03: `.env.example` shipped a non-empty, publicly known
placeholder for `API_KEY` / `POSTGRES_PASSWORD`, and the failed-auth budget
locked out even a correct key. A *placeholder* is refused outright; a key is
"strong" (never locked out by other clients' failures) only when it is long
enough that 10 guesses per 5 minutes cannot reach it.

No secret value is ever returned, logged or placed in an exception message.
"""

import re

# secrets.token_urlsafe(32) is 43 characters; 32 characters of any mixed
# alphabet is >= 160 bits when generated randomly.
MIN_STRONG_KEY_CHARS = 32
MIN_DISTINCT_CHARS = 12

# Known template values / explicit placeholder syntax only. A bare prefix test ("todo...",
# "xxx...", "example...") would refuse legitimate random secrets that merely start that way.
_PLACEHOLDER = re.compile(
    r"(?:replace[-_ ](?:with|me)"
    r"|change[-_ ]?me"
    r"|your[-_ ](?:api[-_ ]?key|secret|password|key|token)"
    r"|insert[-_ ](?:key|secret|password)"
    r"|placeholder"
    r"|<[^<>]*>)"
    r"(?:[-_ ].*)?",
    re.IGNORECASE,
)


def is_placeholder_secret(value: str) -> bool:
    """True for the well-known template strings of the public repository."""
    return bool(_PLACEHOLDER.fullmatch(value.strip()))


def is_strong_secret(value: str) -> bool:
    # length plus a minimal diversity floor: 32 x the same character is not a secret
    return (
        len(value) >= MIN_STRONG_KEY_CHARS
        and len(set(value)) >= MIN_DISTINCT_CHARS
        and not is_placeholder_secret(value)
    )


def secret_strength(value: str) -> str:
    """'unset' | 'placeholder' | 'weak' | 'strong' -- safe to log."""
    if not value:
        return "unset"
    if is_placeholder_secret(value):
        return "placeholder"
    return "strong" if is_strong_secret(value) else "weak"
