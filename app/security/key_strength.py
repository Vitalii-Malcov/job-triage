"""Credential strength rules shared by Settings validation, auth and diagnostics.

Audit SEC-02 / SEC-03: `.env.example` shipped a non-empty, publicly known
placeholder for `API_KEY` / `POSTGRES_PASSWORD`, and the failed-auth budget
locked out even a correct key. A *placeholder* is refused outright; a key is
"strong" (never locked out by other clients' failures) only when it is long
enough that 10 guesses per 5 minutes cannot reach it.

No secret value is ever returned, logged or placed in an exception message.
"""

# secrets.token_urlsafe(32) is 43 characters; 32 characters of any mixed
# alphabet is >= 160 bits when generated randomly.
MIN_STRONG_KEY_CHARS = 32

_PLACEHOLDER_PREFIXES = (
    "replace",
    "changeme",
    "change-me",
    "change_me",
    "your-",
    "your_",
    "example",
    "placeholder",
    "todo",
    "xxx",
    "<",
)


def is_placeholder_secret(value: str) -> bool:
    """True for the well-known template strings of the public repository."""
    normalized = value.strip().lower()
    return bool(normalized) and normalized.startswith(_PLACEHOLDER_PREFIXES)


def is_strong_secret(value: str) -> bool:
    return len(value) >= MIN_STRONG_KEY_CHARS and not is_placeholder_secret(value)


def secret_strength(value: str) -> str:
    """'unset' | 'placeholder' | 'weak' | 'strong' -- safe to log."""
    if not value:
        return "unset"
    if is_placeholder_secret(value):
        return "placeholder"
    return "strong" if is_strong_secret(value) else "weak"
