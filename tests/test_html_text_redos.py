"""SEC-01: `html_to_text` must be linear on hostile input.

The old regexes needed ~108 s for 200 000 characters of unclosed `<!--`.
Each hostile fixture runs in a child process with a 60 s kill timeout: no
timing threshold to flake on a slow runner, and a quadratic regression is
killed (test fails) instead of hanging the suite. Real measurements belong
in scripts/, not in the suite.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.utils.html_text import html_to_text
from tests.html_hostile_fixtures import hostile

REPO = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_SECONDS = 60  # old code needed 100+ s; linear code needs ~0.05 s

# Run in a child process so that a quadratic regression is killed by the
# timeout instead of hanging the whole pytest run.
_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from tests.html_hostile_fixtures import hostile
from app.utils.html_text import html_to_text
from app.agents.application_response_classifier import normalize_body
markup = hostile()[sys.argv[2]]
html_to_text(markup)
if sys.argv[2] == "stored_body_for_normalize":
    for _ in range(5):
        normalize_body(markup)
"""


@pytest.mark.parametrize("name", sorted(hostile()))
def test_hostile_input_finishes_before_the_kill_timeout(name):
    subprocess.run(
        [sys.executable, "-c", _CHILD, str(REPO), name],
        check=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def test_empty_and_whitespace_inputs():
    assert html_to_text("") == ""
    assert html_to_text("  \n\t ") == ""
    assert html_to_text("<br><br>") == ""


def test_html_only_employer_letter_keeps_its_text():
    markup = (
        "<html><head><title>x</title><style>p{color:red}</style></head><body>"
        "<p>Guten Tag,</p><p>wir laden Sie zum <b>Vorstellungsgespr&auml;ch</b> ein.</p>"
        "<script>track()</script><!-- hidden --><br/>"
        "Mit freundlichen Gr&uuml;&szlig;en</body></html>"
    )
    text = html_to_text(markup)
    assert "Guten Tag," in text
    assert "Vorstellungsgespräch" in text
    assert "Grüßen" in text
    for dropped in ("color:red", "track()", "hidden", "<"):
        assert dropped not in text


def test_block_removal_semantics_match_the_old_regex_on_wellformed_html():
    assert html_to_text("a<script>x</script>b") == "a b"
    assert html_to_text("a<SCRIPT >x</SCRIPT >b") == "a b"
    assert html_to_text("a<!-- x -->b") == "a b"
    assert html_to_text("a<!-- x --><!-- y -->b") == "a b"
    # `<header>` is not `<head>`
    assert html_to_text("<header>keep</header>") == "keep"
    # nested same-name blocks end at the first closer, as before
    assert html_to_text("<script>a<script>b</script>c</script>d") == "c d"


def test_unclosed_block_keeps_the_remainder_like_the_old_regex():
    assert html_to_text("a <script> b") == "a b"  # tag stripped, text kept
    assert html_to_text("a <!-- b") == "a <!-- b"


def test_line_breaks_become_newlines():
    assert html_to_text("one<br>two<br />three<BR/>four") == "one\ntwo\nthree\nfour"
    assert html_to_text("<div>a</div><div>b</div>") == "a\nb"
    assert html_to_text("<br class='x'>a") == "a"


def test_unicode_and_nbsp_survive():
    assert html_to_text("<p>Grüße aus 日本</p>") == "Grüße aus 日本"


def test_script_inside_style_text_does_not_swallow_the_body():
    markup = (
        '<html><head><style>p::before{content:"<script>"}</style></head>'
        "<body><p>Interview invitation</p><script>track()</script></body></html>"
    )
    assert html_to_text(markup) == "Interview invitation"


def test_quoted_attribute_containing_lt_or_gt_is_one_tag():
    assert html_to_text('<p title="x < y">Interview</p>') == "Interview"
    assert html_to_text("<a href='a>b'>Link</a>") == "Link"
    assert html_to_text('<br title="a<b">x') == "x"


def test_overlong_numeric_character_reference_does_not_raise():
    assert "ok" in html_to_text("&#" + "9" * 5_000 + ";<p>ok</p>")
    assert html_to_text("&#65;&#x42;") == "AB"


def test_comment_containing_a_head_closer_does_not_end_the_head_early():
    markup = "<head><!-- </head> --><title>hidden</title></head><p>Visible</p>"
    assert html_to_text(markup) == "Visible"


@pytest.mark.parametrize(
    "markup",
    [
        "<scrıpt>x</scrıpt><p>ok</p>",  # dotless i
        "<ſcript>x</script><p>ok</p>",  # long s
        "<İstyle>x</style><p>ok</p>",  # dotted capital I
        "<head>x</head><p>ok</p>",
    ],
)
def test_unicode_lookalike_tag_names_never_raise(markup):
    assert "ok" in html_to_text(markup)


def test_break_lookalike_inside_a_quoted_attribute_does_not_eat_text():
    markup = '<p title="<br x">Interview invitation</p><a title="a>b">Contact</a>'
    text = html_to_text(markup)
    assert "Interview invitation" in text
    assert "Contact" in text
