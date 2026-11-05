# Test inventory — what the numbers mean

Measured on the remediation branch (`fix/hard-review-integration`), Windows 11, Python 3.13, SQLite only unless stated.
Reproduce: `python -m pytest -q -rs --junitxml=run.xml` (the summary line and the `SKIPPED` lines give every number below).

| Quantity | Value | Meaning |
| --- | --- | --- |
| `def test_` functions in `tests/` | 4732 | what a source search counts |
| Collected items | 7,756 | `pytest --collect-only -q`: functions x parametrized cases (this is the "~6,900" style number, now larger) |
| Passed | 7,608 | executed and green in the last completed full run |
| Failed | 0 | that run; the previous full run had 1 failure, a wall-clock heartbeat test that was then made deterministic (the heartbeat's first renewal is gated on the provider having entered `send()`, and the provider waits for the heartbeat's own `lock_lost` event) |
| Skipped | 148 | see below |

## The 148 skips (local run)

| Count | Reason | Is it covered elsewhere? |
| --- | --- | --- |
| 141 | `TEST_POSTGRES_URL` not set — the PostgreSQL integration modules | Yes, but **not by the local SQLite run**: the CI job `scheduler-postgres` runs them with a zero-skip gate. They were also executed against a local `postgres:16` during TASK 16 (105 at head + 36 migration-cycle, 0 skipped). A green local `pytest -q` therefore says nothing about PostgreSQL behaviour. |
| 5 | symlinks unsupported for this Windows account (`WinError 1314`) | not covered on this machine (runs where the right exists, e.g. CI Linux) |
| 1 | the local canonical database is not present | environment-specific by design |
| 1 | the checkout has no other git worktree | environment-specific by design |

`pytest -q` printing "N passed" is **not** a statement about PostgreSQL or GitHub Actions; only the CI jobs defined in `docs/CI.md` are.

## Source-inspection tests (review §4)

The audit criticised tests that grep source text (`".store(" not in source`) — they break on harmless refactors and miss indirect calls.
Converted so far (behaviour or AST instead of substrings): the IMAP provider's no-mutation and `BODY.PEEK[]` guarantees (a strict client that raises on any STORE/EXPUNGE/COPY/APPEND, plus the recorded wire commands), the follow-up send service's "no network" guarantee (every `socket.connect` / `httpx` send fails the test during a full send), and the "never imports an HTTP client / smtplib / imaplib" checks (AST import scan, fixture `imported_modules`; static imports only — relative imports are resolved, dynamic `importlib` calls are a documented blind spot).
Still source-based (`inspect.getsource`): 27 occurrences in 13 files (34 before this task; the AST fixture itself reads source once), mostly docstring/contract checks (`test_gmail_docs_contract.py`) and import guards for other modules; they are listed by `grep -rn "inspect.getsource" tests`.
Decision rule going forward: a guarantee about what the code *does* gets a behavioural test; a guarantee about what a module *imports* uses the AST fixture; text is only checked when the text itself is the contract (documentation).
