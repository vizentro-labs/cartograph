# cartograph-cache (Python core)

Never-stale SQL result cache for AI agents on Postgres. Caches query results and
uses Postgres's own WAL (logical decoding) to invalidate precisely the instant
the underlying data changes — cached or live, the answer always matches the DB.

Proven over four escalating tests (the differential fuzzer + spikes in
`benchmarks/`): **0 stale across 153,015 live-checked hits**, including a real
out-of-band Postgres.

## Layout
```
src/cartograph/
  extract.py   sound footprint extraction (sqlglot parse+qualify; coarse/lineage)
  runtime.py   WAL change capture, footprint cache, schema-drift handling, bootstrap
  core.py      public API: Cartograph.query / explain / stats
  mcp_server.py  thin MCP (stdio) server exposing the three tools
benchmarks/    the original spikes + the differential fuzzer (validate the package)
tests/         end-to-end MCP integration test against real Postgres
examples/      scripted live→cache→live→cache demo
```

## Install & run
```bash
pip install -e .            # or: pip install -r requirements.txt
python -m cartograph.bootstrap_demo     # demo schema + WAL slot + DDL trigger
python examples/demo.py                 # see live→cache→live→cache, stale_count=0
cartograph-mcp                          # start the MCP server over stdio
```

## API
```python
from cartograph import Cartograph
cg = Cartograph(dsn="postgres://…")     # auto-discovers tables + PKs
r  = cg.query("SELECT count(*) FROM orders WHERE status='paid'")
r.source        # 'live' (cold) → 'cache' (warm); never stale
r.as_of_lsn     # the WAL LSN the answer is correct as of
cg.explain(sql) # {cacheable, footprint_mode, footprint} — no execution
cg.stats()      # {hit_rate, refusals_by_reason, stale_count: 0}
```

## Config (env)
| var | default | meaning |
|-----|---------|---------|
| `CARTOGRAPH_DSN`  | local demo socket | libpq DSN |
| `CARTOGRAPH_MODE` | `coarse` | footprint mode: `coarse` \| `column` \| `lineage` |
| `CARTOGRAPH_SLOT` | `cg` | logical replication slot (plugin `test_decoding`) |

## Requirements
PostgreSQL with `wal_level=logical` + a replication slot. `REPLICA IDENTITY FULL`
on cached tables buys per-column update precision; without it invalidation is
coarser but still zero-stale.

## Verify
```bash
PYTHONPATH=src python tests/test_mcp_integration.py     # live/cache/live/cache, stale=0
PYTHONPATH=src python benchmarks/fuzz_postgres.py       # the differential fuzzer
```
