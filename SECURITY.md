# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Use GitHub's private vulnerability reporting: go to the **Security** tab →
**Report a vulnerability**. We aim to acknowledge reports within a few business
days and will coordinate a fix and disclosure timeline with you.

## Scope

The most security-relevant property of Cartograph is **correctness**: a cache
that serves a *stale* answer is the primary failure mode we treat as a defect.
If you can make Cartograph serve a stale result (a cache hit that differs from a
live query at the reported `as_of_lsn`), that is a high-priority report — ideally
with a reproduction via the differential fuzzer in
[`packages/core/benchmarks`](packages/core/benchmarks).

Other things worth reporting: SQL injection / unsafe query handling, replication
slot or credential handling issues, and dependency vulnerabilities.

## Automated scanning

This repository runs **CodeQL** code scanning and **Dependabot** for dependency
updates and security alerts. Secret scanning and Dependabot alerts are enabled
through GitHub for the public repository.
