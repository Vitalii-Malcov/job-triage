"""Remove review-process provenance from comments and docstrings of `app/`.

Review history ("Codex gate follow-up (Astra R4A MEDIUM, starvation)", ticket tags such as
`HARD-007-RR1`, ...) belongs in git history and the audit documents, not in production
comments, where it buries the logic and goes stale. This tool removes ONLY provenance-only
fragments and keeps the explanatory sentence:

* a parenthetical made up solely of provenance words and ticket ids -- "(Codex review A, P2)",
  "(Astra R4A, lease-loss MEDIUM)", "(HARD-007)", also nested -- is deleted;
* a leading provenance label -- "# S10-RR-002 (Codex Stage 10 re-review, BLOCKING): ...",
  "# HARD-007: ...", a docstring's first line "Codex gate follow-up: ..." -- is deleted together
  with its colon (list numbers / bullets in front of it are kept).

Prose ("Codex reproduced ...") is never rewritten. Only the exact source spans of comment tokens
and docstrings are edited (`tokenize` / `ast`), so string literals are never touched; ticket ids
are an explicit allow-list of prefixes (so `SHA-256` or `UTF-8` are not tickets); all matching is
linear (token scans, no backtracking regexes); and the tool refuses to write a file whose AST
(docstrings blanked) would differ from the original.

    python -m app.cli strip-review-attribution --check
    python -m app.cli strip-review-attribution --write
"""

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app"

# Ticket families used by the review rounds (an allow-list: standards such as SHA-256, UTF-8 or
# RFC-3501 are NOT tickets).
_TICKET_PREFIX = (
    r"(?:S[0-9]+[A-Z]*(?:-[A-Z]+)?|ASTRA(?:-[A-Z]+)?|CODEX-[A-Z]+|GMAIL|HARD|AUD|NEW|BOUND|DEPLOY"
    r"|CP-M|FINAL|RR-M|FR-[MH]|CSP|R3|P[0-9]|UID32)"
)
# digits, then optional letters(+digits), then "-"-separated groups: no overlapping quantifiers
_ID = re.compile(rf"{_TICKET_PREFIX}-[0-9]+(?:[A-Z]+[0-9]*)?(?:-[A-Z0-9]+)*")
_MARKER_WORDS = {"codex", "astra"}
_VOCABULARY = frozenset(
    """master targeted gate review re-review re-re-review follow-up finding findings
    remediation round rounds take plan audit hardening adversarial blocking high medium low
    stage and of the second third pass final early lease-loss starvation watermark gap mailbox
    scope width cas uidvalidity privacy log leakage isolation lease loss crash recovery
    correctness fix bug mid-way prior earlier night-audit safety concurrency acceptance
    verification blocker deploy""".split()
)
_SHORT_TOKEN = re.compile(
    r"[A-D]|R[0-9][A-Z]?|P[0-9]|RR[0-9]|S[0-9]+|r[0-9]|[0-9]+(?:-[0-9]+)?[A-Za-z]?"
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_SEPARATORS = set(" \t,;:./+()'\"-")
_INNERMOST_PAREN = re.compile(r"\([^()]*\)")
_OPENERS = ('"""', "'''")


def _skip_spaces(line: str, i: int) -> int:
    while i < len(line) and line[i] in " \t":
        i += 1
    return i


def _split_label(line: str) -> tuple[str, str, str] | None:
    """(lead, label, rest) when the line starts with `lead LABEL: rest`, where the label ends at
    the first colon outside parentheses that is followed by a space or the end of the line.
    `lead` = indentation plus an optional comment marker / docstring opener / list marker / bold
    marker (all kept). A single linear scan -- no regex, so no backtracking."""
    n = len(line)
    i = _skip_spaces(line, 0)
    if i < n and line[i] == "#":
        i = _skip_spaces(line, i + 1)
    j = i + 1 if i < n and line[i] in "rRbBuU" and line[i + 1 : i + 4] in _OPENERS else i
    if line[j : j + 3] in _OPENERS:
        i = _skip_spaces(line, j + 3)
    k = i
    while k < n and line[k].isdigit():
        k += 1
    if k > i and k + 1 < n and line[k] in ".)" and line[k + 1] == " ":
        i = _skip_spaces(line, k + 1)  # "1. " list numbering
    elif i + 1 < n and line[i] in "-*" and line[i + 1] == " ":
        i = _skip_spaces(line, i + 1)  # "- " bullet
    if line.startswith("**", i):
        i += 2
    depth = 0
    for position in range(i, n):
        char = line[position]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return None
            depth -= 1
        elif char == ":" and depth == 0:
            if position + 1 == n or line[position + 1] in " \t":
                return line[:i], line[i:position], line[_skip_spaces(line, position + 1) :]
            return None
    return None


def _closes_an_earlier_line(text: str) -> bool:
    """True when `text` has a ")" with no matching "(" in it (it closes a parenthesis that was
    opened on an earlier line, so cutting before it would leave a broken sentence)."""
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return True
            depth -= 1
    return False


def _leading_colon(line: str) -> str:
    """After a tag stood at the start of the text ("(RR-M-01): two ..."), drop the colon."""
    i = _skip_spaces(line, 0)
    if i < len(line) and line[i] == "#":
        i = _skip_spaces(line, i + 1)
    if line[i : i + 3] in _OPENERS:
        i = _skip_spaces(line, i + 3)
    if i < len(line) and line[i] == ":" and (i + 1 == len(line) or line[i + 1] in " \t"):
        return line[:i] + line[_skip_spaces(line, i + 1) :]
    return line


def _is_provenance(fragment: str) -> bool:
    """True when the fragment is nothing but provenance: ids, vocabulary, short review tags,
    separators -- and it contains at least one marker (Codex/Astra or a ticket id)."""
    # ASCII only: any other character (e.g. Cyrillic prose) means this is not provenance
    if any(not (ch.isascii() and ch.isalnum()) and ch not in _SEPARATORS for ch in fragment):
        return False
    tokens = _TOKEN.findall(fragment)
    if not tokens:
        return False
    marker = False
    for token in tokens:
        if _ID.fullmatch(token) or token.lower() in _MARKER_WORDS:
            marker = True
        elif token.lower() in _VOCABULARY or _SHORT_TOKEN.fullmatch(token):
            continue
        else:
            return False
    return marker


def _drop_parentheticals(line: str) -> str:
    """Delete provenance-only parentheticals, innermost first, until stable (nested too)."""
    while True:
        changed = False
        for match in reversed(list(_INNERMOST_PAREN.finditer(line))):
            inner = match.group(0)[1:-1]
            if _is_provenance(inner):  # empty "()" is code (`send()`), never provenance
                start = match.start()
                if line[:start].strip():  # keep the indentation of a line that starts with a tag
                    while start > 0 and line[start - 1] == " ":
                        start -= 1
                line = line[:start] + line[match.end() :]
                changed = True
        if not changed:
            return line


def clean_text(text: str) -> str:
    """Apply both rules line by line (a label is only removed at the start of a line, repeatedly
    until stable). A comment line left empty by the removal is returned as exactly "#"."""
    out = []
    for line in text.split("\n"):
        without = _drop_parentheticals(line)
        if without != line:
            without = _leading_colon(without)  # "(RR-M-01): two ..." -> "two ..."
        line = without
        while True:
            parts = _split_label(line)
            if parts is None or not _is_provenance(parts[1].replace("(", " ").replace(")", " ")):
                break
            if _closes_an_earlier_line(parts[2]):
                break  # the label sits inside a parenthetical opened on a previous line
            line = parts[0] + parts[2]
        out.append(line.rstrip() if line.strip() == "#" else line)
    return "\n".join(out)


def _docstring_nodes(tree: ast.AST) -> list[ast.Expr]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.append(first)
    return found


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in _docstring_nodes(tree):
        node.value.value = ""
    return tree


def clean_source(source: str) -> str:
    """Rewrite only the exact source spans of comments and docstrings."""
    lines = source.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    def offset(row: int, col: int) -> int:
        return starts[row - 1] + col

    def char_col(row: int, byte_col: int) -> int:
        # ast columns are UTF-8 byte offsets; tokenize and string slicing use characters
        return len(lines[row - 1].encode("utf-8")[:byte_col].decode("utf-8"))

    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)
    for node in _docstring_nodes(ast.parse(source)):
        start = offset(node.lineno, char_col(node.lineno, node.col_offset))
        end = offset(node.end_lineno, char_col(node.end_lineno, node.end_col_offset))
        edits.append((start, end, clean_text(source[start:end])))
    docstring_count = len(edits)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        row, col = token.start
        start, end = offset(row, col), offset(token.end[0], token.end[1])
        cleaned = clean_text(source[start:end])
        whole_line = not lines[row - 1][:col].strip()
        if whole_line and cleaned.strip() == "#" and source[start:end].strip() != "#":
            # the comment only carried a label: drop the whole line including its newline
            edits.append((offset(row, 0), offset(row, 0) + len(lines[row - 1]) + 1, ""))
        else:
            edits.append((start, end, cleaned))
    # Disjoint spans only: a docstring edit wins over any comment token inside it (a parenthesized
    # docstring may carry a trailing comment on its own lines).
    docstring_spans = [(start, end) for start, end, _ in edits[:docstring_count]]
    kept = edits[:docstring_count] + [
        edit
        for edit in edits[docstring_count:]
        if not any(edit[0] < end and start < edit[1] for start, end in docstring_spans)
    ]
    for start, end, replacement in sorted(kept, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def same_code(before: str, after: str) -> bool:
    return ast.dump(_strip_docstrings(ast.parse(before))) == ast.dump(
        _strip_docstrings(ast.parse(after))
    )


def run(argv: list[str] | None = None, *, target: Path = TARGET) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if any fragment is left")
    mode.add_argument("--write", action="store_true", help="rewrite the files in place")
    args = parser.parse_args(argv)
    dirty = 0
    for path in sorted(target.rglob("*.py")):
        raw = path.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in raw else "\n"
        before = raw.replace("\r\n", "\n")
        after = clean_source(before)
        if after == before:
            continue
        if not same_code(before, after):
            print(f"REFUSED {path}: the code would change", file=sys.stderr)
            return 2
        dirty += 1
        if args.write:
            path.write_bytes(after.replace("\n", newline).encode("utf-8"))
        else:
            print(f"review attribution left in {path}")
    if args.check and dirty:
        print(f"{dirty} file(s) carry review attribution; run with --write", file=sys.stderr)
        return 1
    print(f"{'rewrote' if args.write else 'clean:'} {dirty} file(s) needed changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
