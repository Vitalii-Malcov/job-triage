# Codex Review Checklist

Review the git diff only as an independent reviewer.

Return findings ordered by severity with exact file and line references where possible.

Check:
- correctness
- missing edge cases
- unsafe secret handling
- SSRF / injection / unvalidated URLs
- unsafe HTML or prompt injection propagation
- excessive platform automation
- missing retries/timeouts on HTTP calls
- missing or weak tests
- broken async behavior

Do not approve merely because tests pass.
