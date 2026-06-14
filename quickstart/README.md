# Cartograph quickstart — see "never stale" in under 5 minutes

One command spins up a **pre-seeded sample Postgres** *and* **Cartograph wired to
it**, then runs a ~60-second scripted demo that proves the cache can't serve you a
stale answer. **It never touches your own database** — the sample DB is throwaway
and lives only inside the container.

## Run it

```bash
docker compose -f quickstart/docker-compose.yml up --build
```

That's the whole setup. No connection strings, no `wal_level` config, no
replication slot, no permissions — Compose handles the database, and Cartograph
provisions its own change-capture infra on it automatically.

When the demo finishes, tear everything down (the `-v` clears the ephemeral DB):

```bash
docker compose -f quickstart/docker-compose.yml down -v
```

## What you'll watch happen

The runner drives the real engine through a scripted sequence and narrates it:

| step | what happens | result |
|------|--------------|--------|
| 1 | An agent asks an expensive `JOIN + GROUP BY` for the first time | **LIVE** (cold) |
| 2 | The agent asks the **same** query again | **CACHE HIT** (no DB hit) |
| 3 | A *separate* connection changes a row **in the query's footprint** | — |
| 4 | The agent asks the same query | **LIVE** — change caught, returns fresh data |
| 5 | A separate connection changes an **unrelated** column | — |
| 6 | The agent asks the same query | **CACHE HIT** — precise, no over-invalidation |

The dramatic moment is step 4: a stark **BEFORE / AFTER** block shows the cached
total and the fresh total side by side — the number moved, and Cartograph
returned the *new* truth, never the stale one. It ends with a verdict:
`stale answers served: 0 — zero, by construction.`

## How it's wired (no engine changes)

This is purely an onboarding/demo layer on top of the proven core:

- **`db`** — `postgres:16` started with `wal_level=logical` and a `pg_isready`
  healthcheck. Ephemeral storage (`tmpfs`), so every run is a clean slate.
- **`cartograph`** — built from [`packages/core`](../packages/core) (installed
  unchanged) plus [`demo.py`](demo.py). It waits for the DB, calls
  `Cartograph.bootstrap()` to seed `customers/orders/line_items`, set
  `REPLICA IDENTITY FULL`, and create the logical slot + DDL trigger, then runs
  the demo against the live engine via the same public API an agent uses.

## Knobs

| env var | effect |
|---------|--------|
| `CARTOGRAPH_DEMO_PACE` | seconds-per-beat for the paced reveals (default `0.6`; set `0` for instant) |
| `NO_COLOR` | disable ANSI color (clean logs / CI) |
| `CARTOGRAPH_MODE` | footprint precision: `coarse` (default), `column`, `lineage` |

Set them under the `cartograph` service's `environment:` in
[`docker-compose.yml`](docker-compose.yml).

## Next step

Point your own agent at Cartograph over MCP, or connect a real database with
`cartograph-doctor` — see the [repo README](../README.md).
