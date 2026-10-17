"""Stage 14D: which trusted language facts are RELEVANT to one vacancy.

A cover letter cites a language only when the vacancy gives a concrete
reason, never merely because the fact is verified. Deterministic rules, in
priority order:

1. the pinned match evaluated the language against a vacancy requirement
   (`CVLanguageItem.matched_requirement`);
2. the vacancy text names the language ("Deutschkenntnisse", "English",
   "deutschsprachig", ...);
3. the vacancy itself is written in that language (a function-word count,
   see `posting_language`): applying to a German-language posting makes
   German the working language of the application.

Everything else is left out. A language whose level is UNKNOWN carries no
renderable fact and is never selected (the level is never guessed).
Recognized languages are a closed table; an unlisted language can still be
selected by rule 1, never by rules 2/3.
"""

import re
from collections.abc import Sequence

from app.models.cv_draft import CVLanguageItem

# key -> word stems (German and English names, casefolded)
_LANGUAGE_STEMS: dict[str, tuple[str, ...]] = {
    "de": ("deutsch", "german"),
    "en": ("englisch", "english"),
    "fr": ("französisch", "franzoesisch", "french"),
    "es": ("spanisch", "spanish"),
    "it": ("italienisch", "italian"),
    "nl": ("niederländisch", "niederlaendisch", "dutch"),
    "pl": ("polnisch", "polish"),
    "pt": ("portugiesisch", "portuguese"),
    "ru": ("russisch", "russian"),
    "uk": ("ukrainisch", "ukrainian"),
    "ro": ("rumänisch", "rumaenisch", "romanian"),
    "bg": ("bulgarisch", "bulgarian"),
    "tr": ("türkisch", "tuerkisch", "turkish"),
    "ar": ("arabisch", "arabic"),
    "zh": ("chinesisch", "chinese", "mandarin"),
}
# Word forms that name the LANGUAGE (not e.g. the country: "Deutschland").
_SUFFIXES = r"(?:e|en|er|es|kenntniss\w*|sprachig\w*|sprache|-)?"

_FUNCTION_WORDS: dict[str, frozenset[str]] = {
    "de": frozenset(
        "und der die das mit für wir sie ist ein eine einen zu von im auf bei "
        "unsere unser ihre ihr oder auch sich als werden".split()
    ),
    "en": frozenset(
        "and the with for we you is a an to of in on at our your or also as are be".split()
    ),
}
_MIN_FUNCTION_WORDS = 15
_DOMINANCE = 3
_WORD_RE = re.compile(r"[a-zäöüß]+")


def language_key(name: str) -> str | None:
    """The table key for a language NAME such as "Deutsch" / "English"."""
    folded = name.strip().casefold()
    for key, stems in _LANGUAGE_STEMS.items():
        if folded in stems:
            return key
    return None


def _named_in(text: str, key: str) -> bool:
    return any(
        re.search(rf"\b{re.escape(stem)}{_SUFFIXES}\b", text) for stem in _LANGUAGE_STEMS[key]
    )


def posting_language(text: str) -> str | None:
    """ "de"/"en" when the text is clearly written in that language (at
    least `_MIN_FUNCTION_WORDS` function words and `_DOMINANCE` times the
    other language's count), else None."""
    words = _WORD_RE.findall(text.casefold())
    counts = {key: sum(w in vocab for w in words) for key, vocab in _FUNCTION_WORDS.items()}
    for key, other in (("de", "en"), ("en", "de")):
        if counts[key] >= _MIN_FUNCTION_WORDS and counts[key] >= _DOMINANCE * counts[other]:
            return key
    return None


def relevant_languages(
    languages: Sequence[CVLanguageItem], *, job_title: str, job_description: str
) -> list[CVLanguageItem]:
    """The relevant subset of `languages`, best reason first (stable)."""
    text = f"{job_title}\n{job_description}".casefold()
    written_in = posting_language(text)
    ranked: list[tuple[int, int, CVLanguageItem]] = []
    for index, item in enumerate(languages):
        if item.level == "UNKNOWN":
            continue
        key = language_key(item.language)
        if item.matched_requirement is not None:
            rank = 0
        elif key is not None and _named_in(text, key):
            rank = 1
        elif key is not None and key == written_in:
            rank = 2
        else:
            continue
        ranked.append((rank, index, item))
    return [item for _, _, item in sorted(ranked, key=lambda entry: entry[:2])]
