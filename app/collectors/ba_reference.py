"""The Bundesagentur fuer Arbeit `referenznummer` of a posting (Stage 22, P0-1).

The reference is the posting's only stable identity at the BA, but before
Stage 22 it was not persisted: `JobRecord` had no column for it, and the
reference tokens derived from the URL (`email_matching._URL_ID_PATTERN`)
miss every reference containing `_` or `.` (`12265-444700_JB5270016-S`,
`13410-k13572.15943-S`) and every posting whose URL is the employer's own
(`externeURL`); a reference may even contain `/` (`13319-896770/1_626382LS-S`,
20 rows on 2026-10-30), which ends a URL path segment. Measured on the
2026-10-30 catalogue: 478 of 644 "closed" vacancies were false 404s caused by
such truncated references.

Three readers, deliberately different in strictness:

- `storable_reference` -- what the collector got from the BA's own
  structured field: kept when it fits the column (length, safe characters),
  whatever its exact shape;
- `ba_reference_from_url` -- the reference in a public BA job URL
  (`arbeitsagentur.de/jobsuche/jobdetail/<ref>`), used by the backfill;
- `extract_ba_references` -- references quoted in free text (e-mails), only
  in the strict observed shape `<4-5 digits>-<id>-S`, so prose never
  produces one.

`is_valid_ba_reference` is the strict shape; the liveness check treats a
404 as "closed" only for a reference in that shape (see
`app.services.ba_liveness`).
"""

import re
from urllib.parse import unquote, urlsplit

# Matches the `jobs.source_reference` column (String(64)); the same limit as
# `applications.source_reference` and `VacancyIdentity.source_reference`.
MAX_REFERENCE_LENGTH = 64
MAX_TEXT_REFERENCES = 8

_SAFE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./\-]*")
_STRICT_BODY = r"\d{4,5}-[A-Za-z0-9_./\-]{1,50}-S"
_STRICT_REFERENCE = re.compile(_STRICT_BODY)
# Free text (mails) is read in whitespace-separated chunks, so a URL is
# always taken whole (its query, brackets and quotes included). Each chunk
# loses its wrapping brackets/quotes/sentence punctuation and a leading label
# ("Referenznummer:", "Link:"), then EITHER is exactly one reference in the
# strict shape (any case), OR -- if it looks like a URL -- is parsed whole and
# yields a reference only when its own hostname is the BA's. Anything else
# (another site's URL, a longer identifier, "10001-123-S.html") yields nothing.
# ">" also strips a mail quote prefix (">", ">>") glued to the chunk.
_WRAP_OPEN = ">([{<\"'„“‚‘«»"
_WRAP_CLOSE = ")]}>\"'“”‘’«».,;:!?"
_LABEL = re.compile(r"^[^\W\d_][\w.\-]{0,30}:", re.UNICODE)
_URL_LIKE = re.compile(r"[/?#@]|://|^www\.", re.IGNORECASE)
_TEXT_REFERENCE_SHAPE = re.compile(_STRICT_BODY, re.IGNORECASE)
_BA_HOSTS = ("arbeitsagentur.de",)
# Everything after the prefix: a reference may itself contain "/".
_DETAIL_PATH = re.compile(r"^/jobsuche/jobdetail/(.+?)/?$")


def storable_reference(value: object) -> str | None:
    """The trimmed reference when it can be stored, else None (empty, too
    long for the column, or containing anything but letters, digits, `_`,
    `.`, `/` and `-`)."""
    if not isinstance(value, str | int) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_REFERENCE_LENGTH or not _SAFE_REFERENCE.fullmatch(text):
        return None
    return text


def is_valid_ba_reference(value: str | None) -> bool:
    """True for the observed BA shape `<4-5 digits>-<id>-S` within the column limit."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_REFERENCE_LENGTH
        and _STRICT_REFERENCE.fullmatch(value) is not None
    )


def ba_reference_from_url(url: str | None, *, any_case: bool = False) -> str | None:
    """The full reference of a public BA job URL, or None for any other URL
    (an employer's own `externeURL` carries no BA reference). `any_case`
    (mail text only) also accepts e.g. a lower-cased "-s" ending."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if not any(host == name or host.endswith("." + name) for name in _BA_HOSTS):
        return None
    match = _DETAIL_PATH.match(parts.path)
    if match is None:
        return None
    reference = unquote(match.group(1))
    if any_case:
        valid = len(reference) <= MAX_REFERENCE_LENGTH and _TEXT_REFERENCE_SHAPE.fullmatch(
            reference
        )
        return reference if valid else None
    return reference if is_valid_ba_reference(reference) else None


def _text_chunk_reference(chunk: str) -> str | None:
    chunk = chunk.lstrip(_WRAP_OPEN).rstrip(_WRAP_CLOSE)
    label = _LABEL.match(chunk)
    if label is not None and not chunk[label.end() :].startswith("//"):
        chunk = chunk[label.end() :].lstrip(_WRAP_OPEN)
    if not chunk:
        return None
    if len(chunk) <= MAX_REFERENCE_LENGTH and _TEXT_REFERENCE_SHAPE.fullmatch(chunk):
        return chunk
    if _URL_LIKE.search(chunk):
        url = chunk if "://" in chunk else "https://" + chunk
        return ba_reference_from_url(url.rstrip("/"), any_case=True)
    return None


def extract_ba_references(text: str | None) -> frozenset[str]:
    """BA references quoted in free text: a chunk that is exactly one
    reference (strict shape, any case), or a pasted BA job URL. Bounded to
    `MAX_TEXT_REFERENCES` (sorted, deterministic)."""
    found = {ref for chunk in (text or "").split() if (ref := _text_chunk_reference(chunk))}
    return frozenset(sorted(found)[:MAX_TEXT_REFERENCES])
