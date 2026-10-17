"""Stage 15: German operator-notification TEXT for lifecycle changes.

RENDERING ONLY. This module builds a string; it has no Telegram client,
no bot token, no network access and is not called by any sender. Wiring it
to the (disabled-by-default) Telegram notifier is Stage 16 backlog.
"""

from app.models.application_lifecycle import ApplicationLifecycleStatus as S

_HEADLINES: dict[S, str] = {
    S.RECEIVED: "Bewerbung bei {company} bestätigt",
    S.UNDER_REVIEW: "Bewerbung bei {company} wird geprüft",
    S.INTERVIEW: "Einladung zum Gespräch von {company} erhalten",
    S.TECHNICAL_INTERVIEW: "Einladung zum technischen Interview von {company} erhalten",
    S.OFFER: "Angebot von {company} erhalten",
    S.REJECTED: "Absage von {company} erhalten",
    S.WITHDRAWN: "Bewerbung bei {company} zurückgezogen",
    S.SUBMITTED: "Bewerbung bei {company} abgeschickt",
}


def render_lifecycle_notification(
    *, company: str, role: str, previous: S | None, new: S, needs_review: bool
) -> str | None:
    """None when there is nothing to tell (no status change)."""
    if previous == new or new not in _HEADLINES:
        return None
    lines = [_HEADLINES[new].format(company=company), f"Stelle: {role}"]
    if previous is not None:
        lines.append(f"Status: {previous.value} → {new.value}")
    if needs_review:
        lines.append("Bitte prüfen: automatisch aus einer eingehenden E-Mail erkannt.")
    return "\n".join(lines)
