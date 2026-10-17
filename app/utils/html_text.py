"""Inert HTML -> text reduction (Stage 15).

Purely lexical: tags are removed with regular expressions, `<script>`/
`<style>` contents are dropped, entities are unescaped. Nothing is parsed
into a DOM, rendered, executed or fetched -- no URL, image or stylesheet
referenced by the markup is ever touched (same rule as
app/providers/email/base.py's hard security constraint).
"""

import html
import re

_DROP = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BREAK = re.compile(r"<\s*(?:br|/p|/div|/tr|/li|/h\d|/table)\s*/?\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def html_to_text(markup: str) -> str:
    text = _COMMENT.sub(" ", markup)
    text = _DROP.sub(" ", text)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    lines = (line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n", "\n".join(line for line in lines if line)).strip()
