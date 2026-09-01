"""Regression guard (GMAIL-006): Stage 7A source must never claim
attachment content is "never downloaded"/"not downloaded" — a full
`BODY.PEEK[]` fetch transfers the complete MIME message, including
attachment bytes, from the IMAP server. The only guarantee Stage 7A
actually provides is that attachment content is never persisted, opened,
rendered, or analyzed as business content — see
app.providers.email.base.ParsedAttachment's docstring for the accurate
contract.
"""

import inspect

import app.db.gmail_repository as gmail_repository_module
import app.db.models as db_models_module
import app.models.gmail as gmail_models_module
import app.providers.email.base as email_base_module
import app.providers.email.imap as email_imap_module
import app.services.gmail_inbox as gmail_inbox_module

FORBIDDEN_PHRASES = (
    "never downloaded",
    "not downloaded",
    "never be downloaded",
    "never scanned",
)

MODULES = (
    db_models_module,
    gmail_repository_module,
    gmail_models_module,
    email_base_module,
    email_imap_module,
    gmail_inbox_module,
)


def test_no_module_falsely_claims_attachments_are_never_downloaded():
    for module in MODULES:
        source = inspect.getsource(module).lower()
        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in source, (
                f"{module.__name__} falsely claims attachment content is "
                f"{phrase!r} — BODY.PEEK[] transfers the full message "
                "including attachment bytes (GMAIL-006)"
            )


def test_truthful_attachment_guarantees_are_still_stated():
    """Don't let the fix over-correct into silence — the real, narrower
    guarantee must still be documented somewhere findable."""
    combined_source = "\n".join(inspect.getsource(module).lower() for module in MODULES)
    assert "never persisted" in combined_source or "never stored" in combined_source
