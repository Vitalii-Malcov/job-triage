"""Hostile HTML inputs for the SEC-01 regression tests (data only).

Imported by the test and, in a child process, by `_CHILD` in
test_html_text_redos.py -- the fixtures are generated there instead of being
passed on the command line (Windows caps a command line at ~32 KB).
"""

LIMIT = 200_000  # MAX_BODY_LENGTH * 10, the bound applied in imap.py


def hostile() -> dict[str, str]:
    return {
        "unclosed_comment": "<!--" * (LIMIT // 4),
        "unclosed_script": "<script>" * (LIMIT // 8),
        "unclosed_style": "<style>" * (LIMIT // 7),
        "unclosed_head": "<head>" * (LIMIT // 6),
        "lt_run": "<" * LIMIT,
        "lt_a_run": "<a" * (LIMIT // 2),
        "br_then_blanks": "<br" + " " * LIMIT,
        "br_blank_runs": ("<br" + " " * 50) * (LIMIT // 53),
        "closing_p_blanks": "</p" + "\t" * LIMIT,
        "quotes": '<a title="' * (LIMIT // 10),
        "open_quotes": "<a '" * (LIMIT // 4),
        "alternating_quotes": "<a \"<a '" * (LIMIT // 8),
        "quote_then_text": '<a "' + "x" * LIMIT,
        "mixed_openers": "<!--<script><style><head><" * (LIMIT // 26),
        "closers_only": "-->" * (LIMIT // 3) + "</script>" * (LIMIT // 9),
        "nbsp_runs": "  " * (LIMIT // 2),
        "newline_blanks": "a\n \n \n" * (LIMIT // 6),
        "entities": "&amp;" * (LIMIT // 5),
        "long_numref": "&#" + "9" * 5_000 + ";" + "<p>ok</p>" * 100,
        "unicode": "<p>Grüße 日本語 😀</p>" * (LIMIT // 20),
        "unicode_lookalike_tags": ("<scrıpt>x</scrıpt><p>ok</p><İstyle>a</style>" * 2_000),
        "stored_body_for_normalize": "<p " + "<!--" * 5_000,
    }
