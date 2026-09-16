# Astra Deep Audit Policy

Astra is the project's most expensive control layer. It is reserved for situations where a broad,
deep, cross-component audit can provide materially more value than a normal Codex diff review.

## Default mode

Astra audits are **READ-ONLY by default**.

Do not modify repository files.
Do not implement findings.
Do not perform ordinary development work.

Claude Code owns remediation. Codex normally verifies remediation.

## When to use Astra

Use Astra for:

- major milestone audits;
- architectural audits;
- security audits;
- production-readiness audits;
- major persistence redesigns;
- major transaction/concurrency/idempotency redesigns;
- new privileged or externally visible action boundaries;
- major provenance/trust-boundary changes;
- cross-component failure-mode analysis where a repository-wide view is justified.

Do not use Astra for:

- routine Stage completion;
- ordinary implementation;
- small fixes;
- README/documentation-only work;
- naming;
- lint or formatting;
- routine unit/integration test repair;
- ordinary refactoring;
- verification of normal Codex findings.

## Audit priorities

Spend audit budget primarily on:

1. **Critical** findings
2. **High** findings
3. **production-blocking Medium** findings

Focus especially on:

- authentication and authorization boundaries;
- secret and PII leakage;
- persistence and data integrity;
- transaction correctness;
- concurrency, CAS, leases, stale writers and race conditions;
- crash safety, retries and idempotency;
- duplicate or phantom outbound actions;
- definite-vs-uncertain delivery classification;
- provenance and untrusted external content;
- prompt-injection propagation into privileged actions;
- human-approval boundaries;
- migration and production deployment correctness;
- cross-component architectural failure modes;
- backward compatibility when persisted state or external interfaces are involved.

Avoid spending significant audit budget on:

- naming/style preferences;
- formatting;
- lint;
- cosmetic refactors;
- minor test style;
- documentation wording unless it creates an operational or security risk;
- Low findings without meaningful production impact.

## Recommended audit prompt

Use this policy as the default prompt contract:

> Perform a READ-ONLY deep audit of the supplied milestone/revision. Do not modify code and do not
> implement fixes. Prioritize Critical, High, and production-blocking Medium findings. Concentrate
> on security, data integrity, transactions, concurrency, idempotency, provenance/trust boundaries,
> outbound side effects, human-approval boundaries, migration/deployment correctness, and realistic
> production failure modes. Avoid cosmetic, naming, lint, formatting, and low-value style findings.
> Return actionable findings with IDs, severity, evidence, failure scenario, impact, and acceptance
> conditions for remediation.

## Handoff to Astra

Astra should receive a bounded but sufficient milestone handoff containing:

- milestone name and purpose;
- base and head revisions;
- major components changed since the previous milestone;
- architecture/security/trust boundaries introduced or changed;
- persistence/migration changes;
- external integrations and outbound actions involved;
- Claude full-regression evidence;
- relevant Codex milestone findings/status;
- known accepted risks and intentionally deferred issues;
- explicit audit focus areas.

Astra may inspect the full repository when the milestone audit requires it. That broad repository
scope is precisely why Astra is reserved for milestones rather than ordinary Stage reviews.

## Remediation flow

Normal Astra remediation flow:

Astra READ-ONLY findings
→ Claude fixes
→ Claude targeted tests and broader regression as appropriate
→ Codex targeted verification of the affected findings.

Do **not** automatically run Astra again after remediation.

## Astra re-review gate

A targeted Astra re-review is required only when:

- the original finding is Critical; or
- the remediation materially changes a serious architectural boundary;
- the remediation materially changes a security/authentication/authorization boundary;
- the remediation changes the transaction/concurrency/idempotency model in a substantial way;
- the remediation changes a privileged-action, provenance, or trust boundary.

Otherwise Codex closes the finding through targeted independent verification.

When Astra re-review is required, re-review the affected Critical/architectural finding and its
necessary context. Do not automatically restart a complete repository audit.

## Milestone cadence

Normal Stage:

Claude implementation → tests → self-review → full regression → Codex targeted diff review.

Major milestone:

Claude integration review + full regression
→ Codex milestone review when warranted
→ Astra READ-ONLY deep audit
→ Claude remediation
→ Codex targeted verification by default.

The objective is to maximize Astra's value per audit while keeping ordinary development velocity
high and preserving all security/data-integrity gates.
