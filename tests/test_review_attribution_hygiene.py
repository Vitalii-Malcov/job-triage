"""Architecture 3 / quality: production comments carry no review-process provenance."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "strip_review_attribution", ROOT / "scripts" / "strip_review_attribution.py"
)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("# S10-RR-002 (Codex Stage 10 re-review, BLOCKING): never reassign", "# never reassign"),
        ("# HARD-007: keep the winner", "# keep the winner"),
        (
            "# the recipient is pinned (Codex review A, P2) at approval",
            "# the recipient is pinned at approval",
        ),
        ("# Codex gate follow-up (Astra R4A, lease-loss MEDIUM): stop at once", "# stop at once"),
        ("    CSP-001: `candidate_skills` is immutable", "    `candidate_skills` is immutable"),
    ],
)
def test_provenance_is_removed_and_the_explanation_is_kept(before, after):
    assert tool.clean_text(before) == after


@pytest.mark.parametrize(
    "text",
    [
        "# see docs/DEPLOYMENT.md for the proxy setup",
        "# the limit (60 requests per minute) applies per client",
        "# Codex reproduced the raw value in repr(); that is why it is masked",  # narrative: kept
        "# keep (a, b) as a tuple",
    ],
)
def test_prose_and_non_provenance_parentheticals_are_left_alone(text):
    assert tool.clean_text(text) == text


def test_only_comments_and_docstrings_are_touched_string_literals_never():
    source = (
        '"""Module (Codex review): docstring."""\n'
        "SQL = 'SELECT 1  -- (Codex review) not a comment'\n"
        "TEXT = '''HARD-007: inside a string, not a docstring'''\n"
        "x = 1  # HARD-001: trailing comment\n"
    )
    cleaned = tool.clean_source(source)
    assert '"""Module: docstring."""' in cleaned
    assert "'SELECT 1  -- (Codex review) not a comment'" in cleaned
    assert "'''HARD-007: inside a string, not a docstring'''" in cleaned
    assert "x = 1  # trailing comment" in cleaned
    assert tool.same_code(source, cleaned)


def test_a_comment_line_that_only_carried_a_label_disappears():
    cleaned = tool.clean_source("# Codex gate follow-up:\n# the real explanation\nx = 1\n")
    assert cleaned == "# the real explanation\nx = 1\n"


def test_the_safety_check_catches_a_code_change():
    assert tool.same_code("x = 1  # a\n", "x = 1  # b\n")
    assert not tool.same_code("x = 1\n", "x = 2\n")
    assert tool.same_code('"""a"""\nx = 1\n', '"""b"""\nx = 1\n')  # docstrings are not code


def test_cleaning_is_idempotent():
    source = "# S10-RR-002 (Codex re-review): keep this\nx = 1\n"
    once = tool.clean_source(source)
    assert tool.clean_source(once) == once


def test_write_mode_preserves_crlf_and_check_mode_reports(tmp_path, capsys):
    target = tmp_path / "pkg"
    target.mkdir()
    (target / "a.py").write_bytes(b"# HARD-007: keep\r\nx = 1\r\n")
    assert tool.run(["--check"], target=target) == 1
    assert tool.run(["--write"], target=target) == 0
    assert (target / "a.py").read_bytes() == b"# keep\r\nx = 1\r\n"
    assert tool.run(["--check"], target=target) == 0


def test_the_application_sources_carry_no_review_attribution():
    """Ratchet: run `python -m app.cli strip-review-attribution --write` if this fails."""
    assert tool.run(["--check"]) == 0


# --- review round 1 regressions -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "# SHA-256 hex digest over every trusted field",
        "# an RFC-3501 mailbox name, UTF-8 encoded (UTF-7 is rejected)",
        "    `Connection.send()` writes synchronously; `put()` would buffer",
        "# keep (SHA-256) and (RFC-2047) as written",
    ],
)
def test_standards_and_code_references_are_never_mistaken_for_tickets(text):
    assert tool.clean_text(text) == text


def test_matching_is_linear_no_regex_backtracking():
    import time

    started = time.perf_counter()
    for tail in ("1" * 200, "a-" * 200, "Codex " * 100 + "!"):
        tool.clean_text("# Codex " + tail)
        tool.clean_text("(" * 50 + "Codex" + ")" * 50)
    assert time.perf_counter() - started < 1.0


def test_only_the_comment_token_is_edited_never_a_string_before_it():
    source = 'x = "# HARD-007: keep"  # HARD-001: ordinary comment\n'
    cleaned = tool.clean_source(source)
    assert cleaned == 'x = "# HARD-007: keep"  # ordinary comment\n'
    semicolon = 'y = "(Codex review) text"; z = 1  # (HARD-002) note\n'
    assert tool.clean_source(semicolon) == 'y = "(Codex review) text"; z = 1  # note\n'


def test_nested_annotations_are_removed_in_one_pass_and_stay_idempotent():
    once = tool.clean_text("# keep (Codex (Astra review)) this")
    assert once == "# keep this"
    assert tool.clean_text(once) == once


def test_a_docstring_first_line_label_is_removed():
    assert tool.clean_source('"""Codex gate follow-up: a pre-check."""\n') == '"""a pre-check."""\n'
    cleaned = tool.clean_source('def f():\n    """HARD-007: stays honest."""\n')
    assert cleaned == 'def f():\n    """stays honest."""\n'


def test_a_tag_at_the_start_of_an_indented_line_keeps_the_indentation():
    assert (
        tool.clean_text("    (RR-M-01): two concurrent creators") == "    two concurrent creators"
    )


# --- review round 2 regressions -------------------------------------------------------


def test_whitespace_runs_are_linear_time():
    import time

    started = time.perf_counter()
    for n in (200, 800, 3000):
        tool.clean_text("# " + " " * n + "x")
    tool.clean_text("# Codex " + "1" * 5000 + "!")
    assert time.perf_counter() - started < 1.0


def test_unicode_docstrings_are_edited_at_character_not_byte_offsets():
    source = 'def café():\n    """HARD-007: keep "hi" there"""  # HARD-001: trailing\n'
    cleaned = tool.clean_source(source)
    assert cleaned == 'def café():\n    """keep "hi" there"""  # trailing\n'
    assert tool.same_code(source, cleaned)


def test_adjacent_labels_are_removed_to_a_fixed_point():
    once = tool.clean_text("# Codex: HARD-007: keep")
    assert once == "# keep"
    assert tool.clean_text(once) == once


def test_list_numbers_and_bullets_survive_label_removal():
    assert tool.clean_text("1. HARD-007: first item") == "1. first item"
    assert tool.clean_text("- HARD-001: bullet") == "- bullet"
    assert tool.clean_text("    2) S8A-002: second") == "    2) second"


def test_a_label_inside_a_multi_line_parenthetical_is_left_intact():
    # "follow-up, Astra R4A MEDIUM take 3: UIDVALIDITY CAS) that ..." closes a "(" from the
    # previous line; cutting before the ")" would leave a broken sentence
    line = "follow-up, Astra R4A MEDIUM take 3: UIDVALIDITY CAS) that same module's"
    assert tool.clean_text(line) == line


def test_a_label_whose_parenthetical_contains_a_colon_is_not_cut_in_the_middle():
    line = "# Codex gate follow-up (Astra R4B, NEW-003: XING log leakage): the rest"
    assert tool.clean_text(line) == line  # prose inside the parentheses: left alone, not split


# --- review round 3 regressions -------------------------------------------------------


def test_non_ascii_prose_inside_a_parenthetical_is_never_deleted():
    text = "# keep (Codex нет) now"
    assert tool.clean_text(text) == text


def test_ticket_matching_is_linear_on_near_matches():
    import time

    started = time.perf_counter()
    tool.clean_text("# HARD-" + "1" * 30000 + "q: keep")
    assert time.perf_counter() - started < 1.0


def test_a_trailing_comment_inside_a_parenthesized_docstring_does_not_corrupt_the_file():
    source = 'def f():\n    ("doc"  # HARD-007: keep\n     " next")\nx = 1\n'
    cleaned = tool.clean_source(source)
    assert "x = 1" in cleaned
    assert tool.same_code(source, cleaned)


def test_standards_with_a_dash_are_not_tickets_even_as_a_label():
    assert tool.clean_text("# SHA-256: digest of the fields") == "# SHA-256: digest of the fields"
