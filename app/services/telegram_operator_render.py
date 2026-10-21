"""Stage 16: pure rendering of operator cards, detail views, callback
payloads and lifecycle notifications. No database, no network, no clock
(callers pass `now`).

**Plain text only** (no `parse_mode`), so untrusted vacancy or e-mail
content can never be interpreted as Telegram markup.

**PII boundary.** A view model carries vacancy, matcher, package and
lifecycle facts only -- never CV or Bewerbung text, never the candidate
profile's contact data. Free text that originates outside the system
(requirement wording, employer research) is additionally scrubbed of
e-mail addresses and phone numbers before it is shown.

**Size.** The card stays under `CARD_SOFT_LIMIT` and the detail view under
`DETAILS_SOFT_LIMIT` (both well below Telegram's 4096-character hard cap):
least important sections are dropped first. Callback data is
`s16:<a|s|d|l>:<16-char token>` = 22 bytes (limit 64).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.telegram_operator import OperatorAction, OperatorDecision

CARD_SOFT_LIMIT = 3000
DETAILS_SOFT_LIMIT = 3800
NOTIFICATION_SOFT_LIMIT = 1500
CALLBACK_DATA_LIMIT = 64
FIELD_MAX = 120
ITEM_MAX = 40
CARD_ITEMS = 6
DETAIL_ITEMS = 12
SUMMARY_MAX = 400
URL_MAX = 500

CALLBACK_PREFIX = "s16"
_ACTION_CODES: dict[OperatorAction, str] = {
    OperatorAction.APPROVE: "a",
    OperatorAction.SKIP: "s",
    OperatorAction.DETAILS: "d",
    OperatorAction.LATER: "l",
}
_CODE_ACTIONS = {code: action for action, code in _ACTION_CODES.items()}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_CANDIDATE = re.compile(r"(?:\+|\b00)?\d[\d \-/().]{7,}\d")
_MIN_PHONE_DIGITS = 9

_CHANNEL_LABELS = {
    "EMPLOYER_PORTAL": "Employer portal",
    "EMAIL": "E-mail",
    "JOB_BOARD": "Job board",
    "OTHER": "Other",
}
DECISION_LABELS: dict[OperatorDecision, str] = {
    OperatorDecision.APPROVE: "✅ APPROVED",
    OperatorDecision.SKIP: "⏭ SKIPPED",
    OperatorDecision.LATER: "🕒 LATER",
}
NO_SEND_FOOTER = "Telegram approval never sends an application."


# --- view models -----------------------------------------------------------------


@dataclass(frozen=True)
class MatchSummary:
    overall: int
    required: int
    preferred: int
    experience: int
    matched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    partial: tuple[str, ...] = ()
    unknown_count: int = 0
    language_issues: tuple[str, ...] = ()
    language_requirements: tuple[str, ...] = ()
    experience_requirements: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageSummary:
    ready: bool
    reasons: tuple[str, ...] = ()
    review_package_id: int | None = None
    review_status: str | None = None
    review_version: int | None = None
    revision_id: int | None = None
    cv_draft_id: int | None = None
    bewerbung_draft_id: int | None = None
    has_manual_overrides: bool = False


@dataclass(frozen=True)
class HistoryItem:
    sequence: int
    event_type: str
    new_status: str | None
    origin: str
    at: datetime | None


@dataclass(frozen=True)
class OperatorCardView:
    application_id: int
    company: str
    role: str
    location: str | None
    source: str
    reference: str | None
    channel: str
    status: str
    package: PackageSummary
    match: MatchSummary | None = None
    job_url: str | None = None
    first_seen_at: datetime | None = None
    research_summary: str | None = None
    research_risks: tuple[str, ...] = ()
    history: tuple[HistoryItem, ...] = ()
    decision: OperatorDecision | None = None


@dataclass(frozen=True)
class LifecycleNotificationView:
    company: str
    role: str
    event_type: str
    origin: str
    previous_status: str | None
    new_status: str | None
    current_status: str
    classification: str | None = None
    confirmation_required: bool = False
    proposed_status: str | None = None


@dataclass(frozen=True)
class RenderedMessage:
    text: str
    reply_markup: dict | None = None
    extra: dict = field(default_factory=dict)


# --- primitives ------------------------------------------------------------------


def scrub(value: str | None) -> str:
    """Remove e-mail addresses and phone numbers from external free text."""
    if not value:
        return ""
    text = _EMAIL.sub("[e-mail removed]", value)

    def _phone(match: re.Match) -> str:
        digits = sum(ch.isdigit() for ch in match.group(0))
        return "[phone removed]" if digits >= _MIN_PHONE_DIGITS else match.group(0)

    return _PHONE_CANDIDATE.sub(_phone, text)


def cap(value: str | None, limit: int = FIELD_MAX) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _items(values: tuple[str, ...], limit: int) -> str:
    shown = [cap(scrub(v), ITEM_MAX) for v in values[:limit]]
    hidden = len(values) - len(shown)
    text = " · ".join(shown)
    return f"{text} (+{hidden} more)" if hidden > 0 else text


def _age(first_seen: datetime | None, now: datetime) -> str | None:
    if first_seen is None:
        return None
    days = max((now - first_seen).days, 0)
    if days == 0:
        return "first seen today"
    return f"first seen {days} day{'s' if days != 1 else ''} ago"


def channel_label(channel: str) -> str:
    return _CHANNEL_LABELS.get(channel, cap(channel, 30))


def _fit(core: str, sections: list[tuple[int, str]], tail: str, limit: int) -> str:
    """Join sections in display order; while too long, drop the section
    with the highest drop rank (least important) first."""

    def assemble(parts: list[tuple[int, str]]) -> str:
        return "\n\n".join(p for p in [core, *(text for _, text in parts), tail] if p)

    parts = list(sections)
    text = assemble(parts)
    while len(text) > limit and parts:
        parts.remove(max(parts, key=lambda part: part[0]))
        text = assemble(parts)
    return text[:limit]


# --- callback data ---------------------------------------------------------------


def build_callback_data(action: OperatorAction, token: str) -> str:
    data = f"{CALLBACK_PREFIX}:{_ACTION_CODES[action]}:{token}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError("callback_data exceeds Telegram's 64-byte limit")
    return data


def parse_callback_data(data: str | None) -> tuple[OperatorAction, str] | None:
    """Strict parse of `s16:<code>:<token>`; anything else is None. The
    token's SHAPE is validated by the repository lookup."""
    if not data or len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in _CODE_ACTIONS:
        return None
    return _CODE_ACTIONS[parts[1]], parts[2]


def build_card_keyboard(token: str, *, approve_offered: bool, final: bool) -> dict:
    """Inline keyboard. After a final decision only DETAILS stays active;
    APPROVE is offered only for a ready package."""

    def button(label: str, action: OperatorAction) -> dict:
        return {"text": label, "callback_data": build_callback_data(action, token)}

    details = button("📄 DETAILS", OperatorAction.DETAILS)
    if final:
        return {"inline_keyboard": [[details]]}
    first = [button("⏭ SKIP", OperatorAction.SKIP)]
    if approve_offered:
        first.insert(0, button("✅ APPROVE", OperatorAction.APPROVE))
    return {"inline_keyboard": [first, [details, button("🕒 LATER", OperatorAction.LATER)]]}


# --- card ------------------------------------------------------------------------


def _package_line(package: PackageSummary) -> str:
    if package.ready:
        parts = []
        if package.cv_draft_id is not None:
            parts.append("CV READY")
        if package.bewerbung_draft_id is not None:
            parts.append("ANSCHREIBEN READY")
        return "Package: " + (" · ".join(parts) or "READY")
    reasons = ", ".join(package.reasons) or "UNKNOWN"
    return f"Package: PACKAGE_NOT_READY ({cap(reasons, 160)})"


def _open_requirements(m: MatchSummary) -> int:
    return len(m.missing) + len(m.partial) + m.unknown_count


def _open_requirements_line(m: MatchSummary) -> str:
    # V1 quality patch: the matcher percentage counts confirmed evidence
    # only, so 100% next to an unverified requirement is not a full fit.
    return f"Open requirements: {_open_requirements(m)} (not a full fit)"


def render_card(view: OperatorCardView, *, now: datetime) -> str:
    header = [f"💼 {cap(view.company)} — {cap(view.role)}"]
    header.append(f"📍 {cap(view.location) if view.location else 'Location not stated'}")
    core = "\n".join(header)

    # (drop rank, text) in display order; the highest rank is dropped first.
    sections: list[tuple[int, str]] = []
    m = view.match
    if m is not None:
        match_text = (
            f"Match: {m.overall}%\nRequired: {m.required}%\n"
            f"Preferred: {m.preferred}%\nExperience: {m.experience}%"
        )
        if _open_requirements(m):
            match_text += "\n" + _open_requirements_line(m)
        sections.append((0, match_text))
        if m.matched:
            sections.append((4, "Matched:\n" + _items(m.matched, CARD_ITEMS)))
        if m.missing:
            sections.append((3, "Missing:\n" + _items(m.missing, CARD_ITEMS)))
        if m.language_issues:
            sections.append((2, "Language:\n" + _items(m.language_issues, 3)))
    else:
        sections.append((0, "Match: not available"))
    status_lines = [f"Channel: {channel_label(view.channel)}", _package_line(view.package)]
    status_lines.append(f"Status: {view.status}")
    if view.decision is not None:
        status_lines.append(f"Decision: {DECISION_LABELS[view.decision]}")
    sections.append((0, "\n".join(status_lines)))
    source = [f"Source: {cap(view.source, 40)}"]
    if view.reference:
        source.append(f"Ref: {cap(view.reference, 64)}")
    age = _age(view.first_seen_at, now)
    if age:
        source.append(f"Fresh: {age}")
    sections.append((1, "\n".join(source)))
    return _fit(core, sections, NO_SEND_FOOTER, CARD_SOFT_LIMIT)


def render_operator_card(
    view: OperatorCardView, token: str, *, approve_offered: bool, now: datetime
) -> RenderedMessage:
    final = view.decision in (OperatorDecision.APPROVE, OperatorDecision.SKIP)
    return RenderedMessage(
        text=render_card(view, now=now),
        reply_markup=build_card_keyboard(token, approve_offered=approve_offered, final=final),
    )


# --- details ---------------------------------------------------------------------


def render_details(view: OperatorCardView, *, now: datetime) -> str:
    head = [
        f"📄 DETAILS — application #{view.application_id}",
        f"{cap(view.company)} — {cap(view.role)}",
        f"Location: {cap(view.location) if view.location else 'not stated'}",
        f"Source: {cap(view.source, 40)}"
        + (f" · Ref {cap(view.reference, 64)}" if view.reference else ""),
        f"Channel: {channel_label(view.channel)}",
        f"Status: {view.status}",
    ]
    age = _age(view.first_seen_at, now)
    if age:
        head.append(f"Fresh: {age}")
    if view.decision is not None:
        head.append(f"Decision: {DECISION_LABELS[view.decision]}")
    core = "\n".join(head)

    sections: list[tuple[int, str]] = []
    m = view.match
    if m is not None:
        breakdown = [
            "Matcher:",
            f"overall {m.overall}% · required {m.required}% · preferred {m.preferred}% · "
            f"experience {m.experience}%",
        ]
        if m.unknown_count:
            breakdown.append(f"not evaluable: {m.unknown_count}")
        if _open_requirements(m):
            breakdown.append(_open_requirements_line(m))
        sections.append((0, "\n".join(breakdown)))
        if m.missing:
            sections.append((1, "Missing:\n" + _items(m.missing, DETAIL_ITEMS)))
        if m.partial:
            sections.append((5, "Partial:\n" + _items(m.partial, DETAIL_ITEMS)))
        if m.matched:
            sections.append((4, "Matched:\n" + _items(m.matched, DETAIL_ITEMS)))
        if m.language_requirements:
            sections.append((2, "Language requirements:\n" + _items(m.language_requirements, 5)))
        if m.experience_requirements:
            sections.append(
                (2, "Experience requirements:\n" + _items(m.experience_requirements, 5))
            )
    else:
        sections.append((0, "Matcher: no match result linked to this package"))

    p = view.package
    package_lines = [_package_line(p)]
    if p.review_package_id is not None:
        package_lines.append(
            f"Review package #{p.review_package_id}: {p.review_status} "
            f"(version {p.review_version}, revision {p.revision_id})"
        )
    if p.cv_draft_id is not None:
        package_lines.append(
            f"CV draft #{p.cv_draft_id} · Anschreiben draft #{p.bewerbung_draft_id}"
        )
    if p.has_manual_overrides:
        package_lines.append("Contains manual edits (USER_EDIT)")
    sections.append((0, "\n".join(package_lines)))

    risks = list(view.research_risks)
    if m is not None:
        risks.extend(m.warnings)
    if risks:
        sections.append(
            (1, "Review risks:\n" + "\n".join(f"- {cap(scrub(r), 150)}" for r in risks[:6]))
        )
    if view.research_summary:
        sections.append(
            (6, "Employer research:\n" + cap(scrub(view.research_summary), SUMMARY_MAX))
        )
    if view.history:
        lines = ["History:"]
        for item in view.history[-8:]:
            when = item.at.strftime("%Y-%m-%d") if item.at else "—"
            status = item.new_status or "(no status change)"
            lines.append(f"{item.sequence}. {when} {item.event_type} {status} [{item.origin}]")
        sections.append((3, "\n".join(lines)))

    url = ""
    if view.job_url:
        url = view.job_url if len(view.job_url) <= URL_MAX else "(job link too long)"
    tail = "\n".join(part for part in (url, NO_SEND_FOOTER) if part)
    return _fit(core, sections, tail, DETAILS_SOFT_LIMIT)


# --- lifecycle notifications -------------------------------------------------------

_STATUS_ICONS = {
    S.RECEIVED.value: "📩",
    S.UNDER_REVIEW.value: "🔎",
    S.INTERVIEW.value: "📅",
    S.TECHNICAL_INTERVIEW.value: "🧑‍💻",
    S.OFFER.value: "🎉",
    S.REJECTED.value: "✉️",
}
_CLASSIFICATION_LABELS = {
    "RECEIPT_CONFIRMATION": "receipt confirmation",
    "UNDER_REVIEW_NOTICE": "under-review notice",
    "INTERVIEW_INVITATION": "interview invitation",
    "TECHNICAL_INTERVIEW_INVITATION": "technical interview invitation",
    "OFFER": "offer",
    "REJECTION": "rejection",
}
_ACTION_LINES = {
    S.RECEIVED.value: "No action required.",
    S.UNDER_REVIEW.value: "No action required.",
    S.INTERVIEW.value: "Action: answer the invitation yourself. Nothing is sent automatically.",
    S.TECHNICAL_INTERVIEW.value: (
        "Action: answer the invitation yourself. Nothing is sent automatically."
    ),
    S.OFFER.value: "Recorded by the operator.",
    S.REJECTED.value: "Recorded by the operator.",
}


def _source_label(view: LifecycleNotificationView) -> str:
    if view.origin == "GMAIL":
        label = _CLASSIFICATION_LABELS.get(view.classification or "", "message")
        return f"Gmail {label}"
    if view.origin == "OPERATOR":
        return "Operator update"
    return view.origin.capitalize()


def render_lifecycle_notification(view: LifecycleNotificationView) -> str:
    if view.confirmation_required:
        kind = "rejection" if view.proposed_status == S.REJECTED.value else "offer"
        lines = [
            f"⚠️ {cap(view.company)}",
            cap(view.role),
            "",
            f"Possible {kind} detected in an e-mail.",
            f"Status unchanged: {view.current_status}",
            "",
            "Source:",
            _source_label(view),
            "",
            "Operator confirmation required. Nothing was changed automatically; confirm "
            "locally with the Stage 15 tracker if it is genuine.",
        ]
        return "\n".join(lines)[:NOTIFICATION_SOFT_LIMIT]
    icon = _STATUS_ICONS.get(view.new_status or "", "ℹ️")
    lines = [
        f"{icon} {cap(view.company)}",
        cap(view.role),
        "",
        "Status changed:",
        f"{view.previous_status} → {view.new_status}",
        "",
        "Source:",
        _source_label(view),
        "",
        _ACTION_LINES.get(view.new_status or "", "No action required."),
    ]
    return "\n".join(lines)[:NOTIFICATION_SOFT_LIMIT]
