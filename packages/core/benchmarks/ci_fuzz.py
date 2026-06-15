#!/usr/bin/env python3
"""CI soundness gate: a reduced out-of-band fuzz + the full DDL battery, asserting
0 stale. The fuzzer is the safety net for the never-stale guarantee, so CI runs a
bounded version of it on every push.

Run from packages/core, with CARTOGRAPH_DSN pointed at a wal_level=logical Postgres:
    python benchmarks/ci_fuzz.py [iterations]    # default 8000 (or $CG_FUZZ_ITERS)
"""
import os
import sys
import importlib.util

ITERS = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("CG_FUZZ_ITERS", "8000"))

# load the full fuzzer module by path (it is a script, not an importable package)
_spec = importlib.util.spec_from_file_location(
    "fz", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fuzz_postgres.py"))
fz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fz)

fz.bootstrap()
r = fz.fuzz_outofband(ITERS)
assert r["stale"] == 0, f"STALE HITS under out-of-band writes: {r}"

dres = fz.fuzz_ddl()
assert all(ok for *_, ok in dres), f"DDL battery failure: {dres}"
assert all(outcome != "stale" for _, _, outcome, _, _ in dres), f"DDL produced a stale hit: {dres}"

print(f"CI fuzz OK: 0 stale across {r['hits']} clean hits ({ITERS} iters); DDL battery safe")
