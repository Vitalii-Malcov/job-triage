# Codex Review Policy

Act as an independent READ-ONLY reviewer.

## Scope

For a normal Stage, review the supplied commit/diff.

Do not automatically audit the entire repository. Use surrounding repository code only when
necessary to determine whether the changed behavior is correct, safe, compatible, or complete.

Start from the supplied:

- `BASE_SHA`;
- `HEAD_SHA`;
- Stage/finding IDs;
- Claude handoff;
- `git diff BASE_SHA..HEAD_SHA`.

Do not spend review budget rediscovering context that is already accurately supplied in the
handoff, but independently verify every material conclusion needed for approval.

## Independence

Do not approve merely because:

- Claude says the implementation is correct;
- tests pass;
- CI is green;
- the diff looks small.

Independently reason about the affected behavior and boundaries.

## Review priorities

Check, in this order of practical risk:

- correctness and regressions;
- security and secret/PII handling;
- transactions and data integrity;
- concurrency, CAS, leases, races and idempotency;
- provenance and trust boundaries;
- human-approval boundaries;
- outbound-action uncertainty and duplicate/phantom-send risk;
- backward compatibility;
- API/input validation;
- SSRF, injection, untrusted URLs/content and prompt-injection propagation;
- retry/timeout/cancellation behavior;
- missing edge cases;
- test adequacy;
- architecture boundaries actually affected by the diff.

Preserve existing project invariants, including persistence/deduplication semantics, notification
isolation, API authentication/rate limiting, migration correctness, secret handling, and explicit
human approval for final actions where required.

## Test execution

Prefer the smallest targeted tests necessary to validate a review concern.

Do not automatically rerun:

- the full `pytest` suite;
- full Ruff checks;
- full formatting checks;

when Claude has already provided credible green evidence and there is no concrete review reason to
repeat them.

A full independent regression run is appropriate only when:

- Claude's regression evidence is absent, incomplete, stale, or unreliable;
- a finding suggests a systemic regression;
- the change has unusually broad blast radius;
- CI disagrees with local evidence;
- a milestone review explicitly requires an independent full run.

## Findings

Return findings ordered by severity and include, where possible:

- finding ID;
- severity;
- exact file/line;
- failure scenario;
- why it matters;
- required acceptance condition.

Prioritize actionable Critical / High / relevant Medium findings over cosmetic observations.

Do not implement the fix. Claude owns remediation.

## Re-review

After remediation, review only:

- the finding being closed;
- its remediation diff;
- the surrounding context required to establish correctness;
- targeted regression evidence relevant to that finding.

Do not restart a complete Stage or repository review unless the remediation materially changes the
original design, creates a new major boundary, or new evidence shows the original review scope was
insufficient.

Do not reopen a closed finding without new technical evidence.

## Reasoning effort

Use the minimum sufficient reasoning effort:

- **LOW** — documentation-only, test-only, trivial isolated changes, and narrow re-checks of an
  already-understood finding.
- **MEDIUM** — default normal Stage review.
- **HIGH** — only when deeper reasoning is justified by concurrency, transaction isolation, races,
  idempotency/CAS, authentication/authorization, security boundaries, provenance/trust boundaries,
  outbound-action uncertainty, or substantial cross-module architectural interaction.
- **EXTRA HIGH** — exceptional use only; never automatic.

The goal is to reduce duplicate work and context consumption without weakening the independent
security, correctness, data-integrity, or human-approval gate.
