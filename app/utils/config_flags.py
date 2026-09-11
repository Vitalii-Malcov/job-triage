"""Generic, stateless configuration-value helpers with no domain
knowledge of their own -- shared only because the check was
byte-identical across independent call sites.

HARD-004 (adversarial hardening r1): `is_configured` used to live in
`app/collectors/base.py`, which created a layering smell -- both
`app/providers/email/{imap,smtp}.py` (a lower/sibling layer) and
`app/collectors/{bundesagentur,xing_email}.py` imported across package
boundaries for a one-line, side-effect-free string check that has
nothing to do with the `JobCollector` interface `collectors/base.py`
actually exists to define. Moved here (a leaf module with zero `app.*`
imports) as a pure, zero-behavior-change relocation -- confirmed via an
AST-based import-graph scan that no real cycle existed either way, and
that every one of this function's importers uses only this one
stateless helper, nothing else from the old location tied to it.
See docs/ADVERSARIAL_HARDENING_REPORT.md HARD-004.
"""


def is_configured(value: str) -> bool:
    """True if `value` is a real, usable config value rather than empty/whitespace-only.

    Shared across collectors and email providers (e.g. Bundesagentur's
    API key, XING/Gmail's mailbox username/app password) so "is this
    thing configured" is defined once instead of duplicated as slightly
    different `if not value:` checks per source, which could drift out
    of sync (see the whitespace-only-key bug this pattern was introduced
    to fix).
    """
    return bool(value and value.strip())
