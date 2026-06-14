"""Runtime configuration. Env-overridable; the Cartograph facade may also set
these at construction time. Kept as module-level state so the WAL reader, cache,
and extractor share one source of truth (the pattern the fuzzer validates)."""

import os

# libpq DSN for the Postgres we cache over
DSN = os.environ.get(
    "CARTOGRAPH_DSN",
    "host=/tmp/pgsock port=55432 dbname=postgres user=postgres",
)
# logical replication slot (plugin=test_decoding)
SLOT = os.environ.get("CARTOGRAPH_SLOT", "cg")
# footprint mode: "coarse" (CTE-safe, default) | "column" | "lineage"
MODE = os.environ.get("CARTOGRAPH_MODE", "coarse")
# sqlglot dialect used for parse/qualify
DIALECT = os.environ.get("CARTOGRAPH_DIALECT", "postgres")

# Tables Cartograph tracks + their primary keys. Defaults are the demo schema;
# Cartograph.discover() can repopulate these from the live catalog.
TABLES = ["customers", "orders", "line_items"]
PK = {"customers": "id", "orders": "id", "line_items": "id"}
