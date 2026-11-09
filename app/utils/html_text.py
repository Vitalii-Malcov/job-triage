"""Inert HTML -> text reduction (Stage 15).

Purely lexical: tags are removed with regular expressions, `<script>`/
`<style>` contents are dropped, entities are unescaped. Nothing is parsed
into a DOM, rendered, executed or fetched -- no URL, image or stylesheet
referenced by the markup is ever touched (same rule as
app/providers/email/base.py's hard security constraint).

Every step is linear-time on hostile input (audit SEC-01). The previous
`<!--.*?-->`, `<(script|style|head)\\b.*?</\\1\\s*>` and `<[^>]*>` forms
re-scanned to the end of the string from each unclosed opener (O(n^2)):
one 200 000-character HTML-only e-mail held the GIL for ~108 s.
"""

import html
import re

# Stage 1 removes comments, stage 2 the `script`/`style`/`head` blocks (the
# old order, so `<head><!-- </head> --></head>` keeps working). Inside a stage
# the leftmost opener wins, so a `<script>` that appears *inside* a `<style>`
# block is just CSS text and cannot swallow the body up to a later `</script>`.
_COMMENT_OPENER = re.compile(r"<!--")
_COMMENT_CLOSERS = {"": re.compile(r"-->")}
_BLOCK_OPENER = re.compile(
    r"<(?:(?P<script>script)|(?P<style>style)|(?P<head>head))\b", re.IGNORECASE
)
_BLOCK_CLOSERS = {
    "script": re.compile(r"</script\s*>", re.IGNORECASE),
    "style": re.compile(r"</style\s*>", re.IGNORECASE),
    "head": re.compile(r"</head\s*>", re.IGNORECASE),
}

# A tag body is a run of single characters that are not `<`, `>` or a quote,
# or one complete quoted string. The three alternatives start with disjoint
# characters, so matching is deterministic (no catastrophic backtracking) and
# `<p title="x < y">` is still one tag. Break tags are recognised on the whole
# matched tag (`_tag_replacement`), never by a second scan of the markup.
_TAG_BODY = r"""(?:[^<>"']|"[^"]*"|'[^']*')*"""
_BREAK_TAG = re.compile(r"<\s*(?:br|/p|/div|/tr|/li|/h\d|/table)(?=[\s/>])", re.IGNORECASE)
_TAG = re.compile("<" + _TAG_BODY + ">")
_SPACES = re.compile("[ \\t\\r\\f\\v ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
# html.unescape raises ValueError on a decimal reference with > 4300 digits.
_LONG_NUMREF = re.compile(r"&#[xX]?[0-9a-fA-F]{12,};?")


def _drop_blocks(text: str, opener_re: re.Pattern[str], closers: dict[str, re.Pattern[str]]) -> str:
    """Replace each `opener ... closer` block by a space, in one pass.

    Linear: the opener scan only moves forward; a successful block advances
    past its closer; a kind whose closer is missing is remembered (any later
    opener of that kind cannot have one either), so its tail is scanned once.
    An unclosed opener leaves the remainder untouched, as the old regex did.
    """
    parts: list[str] = []
    pos = 0
    dead: set[str] = set()
    for opener in opener_re.finditer(text):
        if opener.start() < pos:
            continue
        # the branch that matched, not the matched text: IGNORECASE also accepts
        # Unicode look-alikes (dotless i, long s) that no ASCII key would match
        kind = opener.lastgroup or ""
        if kind in dead:
            continue
        closer = closers[kind].search(text, opener.end())
        if closer is None:
            dead.add(kind)
            continue
        parts.append(text[pos : opener.start()])
        parts.append(" ")
        pos = closer.end()
    parts.append(text[pos:])
    return "".join(parts)


def _unescape(text: str) -> str:
    try:
        return html.unescape(text)
    except ValueError:
        return html.unescape(_LONG_NUMREF.sub(" ", text))


def _tag_replacement(tag: re.Match[str]) -> str:
    # Decide on the COMPLETE tag, so a `<br` inside a quoted attribute of an
    # earlier tag can never start a break match that eats visible text.
    return "\n" if _BREAK_TAG.match(tag.group()) else " "


def html_to_text(markup: str) -> str:
    text = _drop_blocks(markup, _COMMENT_OPENER, _COMMENT_CLOSERS)
    text = _drop_blocks(text, _BLOCK_OPENER, _BLOCK_CLOSERS)
    text = _TAG.sub(_tag_replacement, text)
    text = _unescape(text)
    text = _SPACES.sub(" ", text)
    lines = (line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n", "\n".join(line for line in lines if line)).strip()
