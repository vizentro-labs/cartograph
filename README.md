# Cartograph

**Connect your Postgres. Your agent's answers are never stale.**

Cartograph is a never-stale result cache for AI agents that query Postgres. It
caches query results and uses the database's own change stream (WAL logical
decoding) to invalidate the instant the underlying data changes, so an agent's
answer is never stale, while skipping re-execution (and the LLM round-trip) when
nothing changed. MIT-licensed, exposed to agents over MCP.

The correctness story is proven, not asserted: **0 stale across 153,015
live-checked hits**, including against a real out-of-band Postgres. The evidence
is the differential fuzzer in
[`packages/core/benchmarks`](packages/core/benchmarks).

## Quickstart: see it in under 5 minutes

The fastest path needs only Docker. It spins up a throwaway sample Postgres and
Cartograph wired to it, then runs a ~60-second scripted demo that proves the
cache can't serve a stale answer. It never touches your own database.

```bash
docker compose -f quickstart/docker-compose.yml up --build
# tear down (clears the ephemeral demo DB):
docker compose -f quickstart/docker-compose.yml down -v
```

No connection strings, no `wal_level` config, no replication slot. Details in
[`quickstart/`](quickstart).

## Use it against your own Postgres

Requires Postgres with `wal_level=logical`.

```bash
cd packages/core
pip install -e .

# Provision an EXISTING database (idempotent, non-destructive): creates a
# replication slot + DDL trigger and sets REPLICA IDENTITY FULL. Tells you the
# one manual step (wal_level) if it's missing.
cartograph-doctor "postgres://user@host:5432/dbname"

# ...or spin up a throwaway demo schema instead:
python -m cartograph.bootstrap_demo
python examples/demo.py            # live -> cache -> live -> cache, stale_count=0
```

> **Windows:** if `python` / `pip` open the Microsoft Store, use the bundled
> launcher instead: `py -m pip install -e .`, `py -m cartograph.bootstrap_demo`.

## Point an agent at it (MCP)

```json
{
  "mcpServers": {
    "cartograph": {
      "command": "cartograph-mcp",
      "env": {
        "CARTOGRAPH_DSN": "postgres://… (wal_level=logical)",
        "CARTOGRAPH_MODE": "coarse"
      }
    }
  }
}
```

Tools: `query(sql)` → `{rows, source, footprint_mode, as_of_lsn}` ·
`explain(sql)` · `stats()` (`stale_count` is `0` by construction). More in
[`packages/core/README.md`](packages/core/README.md).

## How it works

1. **Footprint.** Parse the SQL (sqlglot) and derive the exact set of
   `(table, column)` the answer depends on, plus a `*rowset` sentinel per source
   table (for inserts/deletes). No query planner or `EXPLAIN` required.
2. **Invalidate from the WAL.** Cheap O(1) version counters keyed by that
   footprint. A write bumps the columns it changed; a cache hit is valid only if
   none of its footprint counters moved, checked by draining the WAL to the
   current LSN before serving.
3. **Conservative = correct.** When extraction is unsure, the footprint widens or
   refuses, never narrows. That makes zero-stale a property by construction; the
   fuzzer's job is to confirm the footprints are real supersets.

## Honest limits

- **Never-stale is unconditional; the savings are workload-dependent.**
  Read-heavy workloads win big; a hot table under constant writes correctly
  invalidates and saves little (that's the conservatism working, not a bug).
- **Setup is real:** `wal_level=logical` plus a replication slot you must monitor
  (an abandoned slot retains WAL and can fill the disk). `REPLICA IDENTITY FULL`
  buys per-column precision; without it invalidation is coarser but still
  zero-stale.
- CTEs and derived tables use a coarse-but-sound fallback (99% of the 103 TPC-DS
  queries are cacheable); views are refused unless inlined.

## Layout

| path | what |
|------|------|
| [`packages/core`](packages/core) | the never-stale cache + MCP server (Python · sqlglot · psycopg2 · MCP) |
| [`packages/core/benchmarks`](packages/core/benchmarks) | the differential fuzzer + spikes that prove zero-stale |
| [`quickstart`](quickstart) | zero-config Docker demo |

## License

MIT.
