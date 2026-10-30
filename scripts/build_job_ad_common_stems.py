"""Regenerate `app/agents/job_ad_common_stems.py` from a job catalogue.

The list holds every word stem (same tokenizer as
`app.agents.description_similarity`) that appears in the role sections of at
least `--min-share` of the given job descriptions -- generic job-ad
vocabulary that must not count as evidence that two postings are the same
job. Reads the database strictly read-only (SQLite URI `mode=ro`); writes
only the generated module (or prints it with --stdout).

    python scripts/build_job_ad_common_stems.py --database <copy.db> [--min-share 0.01] [--stdout]

Run it against a COPY of a catalogue, never against a database another
process is writing to.
"""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.description_similarity import role_section_text, text_stems  # noqa: E402

TARGET = ROOT / "app" / "agents" / "job_ad_common_stems.py"
HEADER = '''"""Generic job-ad vocabulary (word stems) -- GENERATED, do not edit by hand.

Stems appearing in the task/requirement sections of at least {share:.0%} of
{documents} real job descriptions.
Catalogue snapshot: {label}.
Regenerate with `scripts/build_job_ad_common_stems.py`;
see `app.agents.description_similarity`.
"""

COMMON_JOB_AD_STEMS = frozenset(
    """
'''


def build(database: Path, min_share: float) -> tuple[list[str], int]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "select description from jobs where length(trim(description)) > 300"
        ).fetchall()
    counts: Counter[str] = Counter()
    documents = 0
    for (description,) in rows:
        stems = set(text_stems(role_section_text(description)))
        if stems:
            documents += 1
            counts.update(stems)
    threshold = max(2, round(min_share * documents))
    return sorted(stem for stem, count in counts.items() if count >= threshold), documents


def render(stems: list[str], documents: int, share: float, label: str) -> str:
    lines = []
    line = "   "
    for stem in stems:
        if len(line) + len(stem) + 1 > 96:
            lines.append(line)
            line = "   "
        line += " " + stem
    lines.append(line)
    body = "\n".join(lines)
    return (
        HEADER.format(share=share, documents=documents, label=label)
        + body
        + '\n    """.split()\n)\n'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--min-share", type=float, default=0.01)
    parser.add_argument("--label", default="unspecified")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.min_share < 1:
        parser.error("--min-share must be between 0 and 1")
    stems, documents = build(args.database, args.min_share)
    source = render(stems, documents, args.min_share, args.label)
    if args.stdout:
        sys.stdout.write(source)
    else:
        TARGET.write_text(source, encoding="utf-8")
        print(f"wrote {len(stems)} stems from {documents} descriptions to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
