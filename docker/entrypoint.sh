#!/usr/bin/env sh
# Deliberately a thin passthrough — no wait-loops, no swallowed exit
# codes. DB-readiness is handled by compose's healthcheck-gated
# depends_on (see docs/DEPLOYMENT.md), not by retry logic in here, so a
# migration or startup failure fails loudly instead of being hidden.
# `exec` replaces this shell with the target process so it becomes PID
# 1's direct child and receives SIGTERM directly for a clean shutdown.
set -e
exec "$@"
