# Agent Collaboration Rules

## Operating model

The project uses three review levels:

1. **Claude Code — Primary Developer**
2. **Codex — Independent Reviewer / Merge Quality Gate**
3. **Astra — Expensive Milestone / Deep-Audit Layer**

The default operating target is to place approximately 70–80% of routine development work on
Claude Code and approximately 20–30% of ordinary independent review work on Codex. Astra is not
assigned a routine percentage: it is event-driven and reserved for high-value deep audits.

Optimize agent usage without weakening security, data-integrity, transaction, concurrency,
provenance/trust-boundary, backward-compatibility, or human-approval gates.

## Claude Code — Primary Developer

Claude owns the complete development loop before independent review, including:

- implementation and refactoring;
- debugging;
- writing and repairing tests;
- targeted tests during development;
- self-review of the complete diff;
- regression analysis;
- documentation;
- remediation of Codex findings;
- remediation of Astra findings;
- the normal full regression suite;
- preparation of a structured reviewer handoff.

Before a normal Codex handoff, Claude should normally provide passing evidence for:

- `pytest -q`
- `ruff check .`
- `ruff format --check .`

If database schema changed, Claude must also verify the Alembic migration and relevant database
or PostgreSQL integration behavior.

Claude must not rely on Codex to discover ordinary implementation mistakes that can reasonably be
caught during self-review.

## Codex — Independent Reviewer

Codex must independently review proposed changes and must not merely confirm Claude's conclusions.
For ordinary stages, Codex reviews the proposed commit/diff, not the entire repository.

Codex should start from:

- base commit SHA;
- reviewed commit SHA;
- `git diff` between those revisions;
- Claude's structured handoff;
- only the additional repository context required to understand the changed behavior.

Repository context may be inspected whenever necessary for correctness, but Codex must not
silently turn every normal Stage review into a full repository audit.

### Review priorities

1. Correctness and regressions
2. Security and secret/PII handling
3. Transactions and data integrity
4. Concurrency, leases, CAS and idempotency
5. Provenance and trust boundaries
6. Human-approval and outbound-action boundaries
7. Backward compatibility
8. API/input validation
9. Async/network failure, retry and timeout behavior
10. Test adequacy
11. Architecture boundaries affected by the diff

### Codex test policy

Codex should run the smallest targeted tests necessary to independently validate review concerns.

Codex must not automatically repeat:

- the full `pytest` suite;
- full Ruff checks;
- full formatting checks;
- a full repository audit;

when Claude already supplied credible passing evidence and no concrete review reason requires
repetition.

A full independent regression run is reserved for cases where:

- Claude regression evidence is absent, incomplete, stale, or unreliable;
- a finding indicates a systemic regression;
- the diff has unusually broad blast radius;
- CI disagrees with local evidence;
- a milestone review explicitly requires an independent full run.

### Codex remediation policy

Codex is READ-ONLY by default while acting as reviewer. Codex must not fix its own findings.

When Codex returns findings:

1. Claude performs the remediation.
2. Claude runs affected tests and broader regression when appropriate.
3. Codex re-reviews only the affected findings, remediation diff, and necessary surrounding context.

Do not restart a complete Stage or repository review for every remediation round. Closed findings
must not be reopened without new technical evidence.

### Codex reasoning effort

Use the minimum reasoning effort adequate for the review:

- **LOW** — documentation-only, test-only, trivial isolated changes, and narrow re-checks of an
  already-understood finding.
- **MEDIUM** — default normal Stage review, including ordinary service/domain/API/database changes.
- **HIGH** — only when deeper reasoning is justified by concurrency, transaction isolation, race
  conditions, idempotency/CAS, authentication/authorization, security boundaries, provenance/trust
  boundaries, outbound-action uncertainty, or substantial cross-module architectural interaction.
- **EXTRA HIGH** — exceptional use only; never automatic.

## Astra — Deep Audit Layer

Astra is not part of the normal per-Stage development loop.

Do not use Astra for:

- ordinary implementation;
- small fixes or refactors;
- README/documentation-only work;
- naming;
- lint/format work;
- routine test repair;
- normal per-Stage review.

Use Astra for:

- major milestone audits;
- architectural audits;
- security audits;
- production-readiness audits;
- major persistence, concurrency, privileged-action, provenance, or trust-boundary redesigns.

Astra audits are READ-ONLY by default and should prioritize:

1. Critical findings
2. High findings
3. production-blocking Medium findings

Claude owns remediation of Astra findings. Codex normally verifies Astra remediation.

Astra re-review is required only when:

- the original finding is Critical; or
- the remediation materially changes an architectural, security, transaction, concurrency,
  provenance, privileged-action, or trust boundary.

Otherwise, use Codex targeted verification rather than spending another Astra audit cycle.

See `docs/ASTRA_AUDIT_POLICY.md` for the reusable deep-audit policy.

## Review frequency

Do **not** perform a full repository audit after every Stage.

### Normal Stage

Claude development → targeted tests → self-review → fixes → full regression → structured handoff
→ Codex targeted diff review → merge when approved.

If Codex returns findings: Claude fixes → targeted tests/full suite when warranted → Codex re-check
of only the affected findings.

### Major milestone

Claude integration review + full regression → Codex milestone review when warranted → Astra
READ-ONLY deep audit → Claude remediation → targeted verification (normally Codex).

## Merge gate

A change is not ready to merge with unresolved:

- Critical findings;
- High findings;
- security/data-integrity/transaction/concurrency/provenance/trust-boundary Medium findings;
- human-approval boundary regressions;
- persistence/deduplication regressions;
- notification isolation regressions (Telegram failure must not break successful core work);
- API authentication/rate-limiting regressions on non-health endpoints;
- secrets committed to source control;
- required test, Ruff, or formatting failures;
- SQLAlchemy model changes without an accompanying Alembic migration under `alembic/versions/`.

A non-security/non-integrity Medium finding may be deferred only explicitly, with rationale, when it
is not production blocking and the deferral itself does not weaken a project invariant.

Human approval before final job application or email submission where approval is required remains
a non-negotiable project invariant.
