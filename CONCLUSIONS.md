# Cartograph — Conclusions

A running summary of what we set out to prove, what we found, and where it
stands. Full detail per phase is in [`VERDICT.md`](VERDICT.md).

## The hypothesis
You can cache a SQL result and **cheaply** (no re-execution, no LLM) detect when
the underlying data changed enough that the cached answer would be wrong,
invalidate precisely, and **never serve a stale answer** — while still avoiding
most re-executions.

## Verdict: SUPPORTED, end to end.
The never-stale promise held through four escalating tests — synthetic,
auto-extracted, real-world SQL, and a real out-of-band Postgres — and is now
usable by an agent over MCP. Remaining work is operational hardening, not
correctness.

---

## What each phase proved

| phase | question | result | headline evidence |
|------|----------|--------|-------------------|
| **Spike 1** | Does cheap footprint invalidation beat TTL / blunt invalidation? | **YES** | 0.000% stale; ~2× hit rate vs invalidate-on-any-write; **no TTL ever reaches zero stale** |
| **Spike 2** | Does zero-stale survive *automatic* footprint extraction (sqlglot)? | **YES** | 0 stale across **39,259** live-checked hits; 89% coverage on synthetic SQL |
| **Spike 3** | Does it work on *real* SQL (TPC-DS + dbt), incl. CTEs? | **YES** | coarse fallback lifts coverage **42.7% → 99%** on TPC-DS; 0 stale across **85,446** hits |
| **Spike 4** | Does it survive a *real Postgres* with out-of-band writes + DDL? | **YES** | 0 stale across **28,310** clean hits under **89,368** out-of-band writes; full DDL battery safe |
| **M2 (MCP)** | Can an agent use it as a tool? | **YES** | thin MCP server; demo shows `live→cache→live→cache`, `stale_count = 0` |

---

## The core idea (what actually works)
- **Footprint = the set of `(table, column)` a query reads**, plus a `*rowset`
  sentinel per source table (for inserts/deletes). Soundness needs only this
  set — **no query planner / EXPLAIN / cardinality required**.
- **Invalidation = cheap O(1) version counters** keyed by that footprint. A
  write bumps the columns it changed (and rowset on insert/delete); a cache hit
  is valid iff none of its footprint counters moved.
- **Conservative = correct.** When unsure, the footprint widens (more
  invalidation), never narrows. That makes zero-stale a property *by
  construction*; the fuzzer's job was to confirm the footprints are real
  supersets — and to catch the bugs where they weren't.

## What's true, stated plainly
- **Never stale** is the strong, unconditional guarantee. It held in every
  phase, including against a real WAL-driven Postgres.
- **"Avoids most re-executions" is workload-dependent.** Hit rate is capped by
  how often the footprint's columns get written. Read-heavy workloads win big;
  a hot table under constant writes saves little (that's correct conservatism).
- **TTL is not a substitute.** Every TTL we tried served wrong answers; the only
  zero-stale TTL is 0 (= no cache).

## Where it got hard (the honest risks)
1. **Automatic footprint extraction** from arbitrary SQL is the crux. sqlglot
   `qualify` (column lineage) is sufficient for *soundness*; `pushdown_projections`
   adds precision. A footprint that is too *narrow* silently breaks the guarantee
   — so the bias is always toward widening/refusing.
2. **CTEs / derived tables** dominate real analytical SQL. A coarse fallback
   (union of every base column referenced anywhere) makes them cacheable and
   sound; lineage pruning recovers precision for the dbt `SELECT *` staging
   pattern.
3. **Production substrate.** Out-of-band writes are captured from Postgres's WAL
   (logical decoding); schema/DDL changes via an event trigger on the same
   stream. The write→bump window is **closed** by draining the WAL synchronously
   to the current LSN before each serve. Costs: `wal_level=logical`, a
   replication slot to manage, optional `REPLICA IDENTITY FULL` for per-column
   precision.

## Bugs the differential fuzzer caught (it earned its keep)
- **Store-on-race:** caching a freshly-computed result under a fingerprint taken
  *after* a concurrent write → stale. Fix: capture the fingerprint *before*
  executing.
- **Type-aware schema diff:** a column type change alters results but not the
  column set — invalidation must diff `(column, type)`.
- **Dropped table ≠ CTE alias:** after a `DROP`, a real table looks "unknown" to
  coarse mode — guard with a refuse.
- **Output-alias over-refusal:** `ORDER BY`/`GROUP BY` on a projected alias is
  sound to skip; refusing it tanked coverage to 42%.

## Where it stands now
- Proven core (spikes 1–4) + a thin `cartograph/` package exposing
  `query / explain / stats`, wrapped as an **MCP server** an agent can call.
- Every cached answer reports `source` (`cache`/`live`) and `as_of_lsn`, so
  correctness is observable. `stale_count` is `0` by construction.
- **Not yet:** views (refused unless inlined), schema-change *coverage* recovery,
  more SQL dialects, replication-slot lifecycle/monitoring — all engineering, not
  correctness.

## One-line takeaway
**Never-stale SQL caching with cheap, sound, automatic invalidation is real — it
held on synthetic data, real TPC-DS/dbt SQL, and a live out-of-band Postgres, and
an agent can now use it over MCP. The open work is coverage breadth and ops, not
the guarantee.**
