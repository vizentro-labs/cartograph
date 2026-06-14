"""
Cartograph public API — the production serve path over the graduated core
(`extract` + `runtime`). No caching/extraction/capture logic lives here; this
just orchestrates the proven primitives and surfaces correctness signals.

The serve path encodes the one discipline spike 4's fuzzer established: capture a
column footprint's fingerprint BEFORE executing, so a result is never stored
under a fingerprint that is "ahead" of it (the store-on-race bug).
"""

import time
import datetime
from collections import deque
from dataclasses import dataclass, asdict
from decimal import Decimal

from . import config
from .runtime import (WalVersions, Cache, read_schema, on_schema_change,
                      conn, bootstrap as _bootstrap, discover_tables_pk)


@dataclass
class QueryResult:
    rows: list
    source: str            # "cache" | "live"
    footprint_mode: str    # "row" | "column" | "coarse" | "refused:<reason>"
    as_of_lsn: str

    def to_dict(self):
        return asdict(self)


def _j(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def _json_rows(rows):
    return [[_j(v) for v in row] for row in rows]


class Cartograph:
    """Never-stale SQL result cache over a real Postgres."""

    def __init__(self, dsn=None, mode=None, slot=None, discover=True):
        if dsn:
            config.DSN = dsn
        if mode:
            config.MODE = mode
        if slot:
            config.SLOT = slot
        if discover:
            try:
                tables, pk = discover_tables_pk()
                if tables:
                    config.TABLES, config.PK = tables, pk
            except Exception:
                pass                       # keep configured defaults
        self.dsn = config.DSN
        self.mode = config.MODE
        self._ver = WalVersions()          # WAL reader (its own connection)
        self._cache = Cache(self._ver, read_schema())
        self._qconn = conn()               # query connection
        self._stats = {"queries": 0, "hits": 0, "misses": 0, "refused": 0,
                       "refusals_by_reason": {}, "stale_count": 0}
        self._events = deque(maxlen=200)   # invalidation / serve feed for the dashboard

    def _emit(self, kind, detail, source):
        self._events.appendleft({
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "kind": kind, "detail": detail, "source": source,
        })

    # ---- lifecycle ----
    @staticmethod
    def bootstrap():
        """(Re)create the demo schema + WAL slot + DDL trigger (admin step)."""
        _bootstrap()

    def close(self):
        try:
            self._qconn.close()
        except Exception:
            pass

    # ---- internals (orchestration only) ----
    def _refresh(self):
        self._ver.drain()                  # synchronous drain to current LSN
        if self._ver.schema_dirty:
            changed, n = on_schema_change(self._cache, self._ver)
            if changed:
                self._emit("schema", f"DDL on {', '.join(sorted(changed))}; "
                           f"invalidated {n}", "recompute")

    def _lsn(self):
        cur = self._qconn.cursor()
        cur.execute("SELECT pg_current_wal_lsn()::text")
        return cur.fetchone()[0]

    def _fp_mode(self, fp):
        return "row" if fp[0] == "row" else self.mode

    def _execute(self, sql):
        cur = self._qconn.cursor()
        cur.execute(sql)
        return cur.fetchall()

    # ---- public API ----
    def query(self, sql):
        """Serve from cache if the footprint is unchanged, else execute live and
        cache it. Never stale (same guarantee as the core)."""
        self._stats["queries"] += 1
        self._refresh()
        as_of = self._lsn()
        fp = self._cache.footprint(sql)

        label = " ".join(sql.split())[:70]
        if fp[0] == "refuse":
            self._stats["refused"] += 1
            self._stats["refusals_by_reason"][fp[1]] = \
                self._stats["refusals_by_reason"].get(fp[1], 0) + 1
            rows = self._execute(sql)
            self._emit(f"refused:{fp[1]}", label, "live")
            return QueryResult(_json_rows(rows), "live", f"refused:{fp[1]}", as_of)

        prior = sql in self._cache.store_      # was it cached before this lookup?
        served, hit = self._cache.lookup(sql, self._qconn)
        if hit:
            self._stats["hits"] += 1
            self._emit("hit", label, "cache")
            return QueryResult(_json_rows(served), "cache", self._fp_mode(fp), as_of)

        # MISS: capture the fingerprint BEFORE executing (proven serve ordering).
        fp_val = self._cache.fingerprint(fp, self._qconn)
        rows = self._execute(sql)
        self._cache.store_[sql] = (rows, fp_val)
        self._stats["misses"] += 1
        self._emit("recompute" if prior else "cold", label, "live")
        return QueryResult(_json_rows(rows), "live", self._fp_mode(fp), as_of)

    def explain(self, sql):
        """Dry run: cacheable? at what precision? or why refused. No execution."""
        self._refresh()
        fp = self._cache.footprint(sql)
        if fp[0] == "refuse":
            return {"cacheable": False, "refusal_reason": fp[1], "footprint": None}
        if fp[0] == "row":
            footprint = {"kind": "row", "table": fp[1], "pk": fp[2], "value": fp[3]}
        else:
            footprint = {"kind": "column",
                         "columns": sorted(f"{t}.{c}" for (t, c) in fp[1])}
        return {"cacheable": True, "footprint_mode": self._fp_mode(fp),
                "footprint": footprint}

    def stats(self):
        cacheable = self._stats["hits"] + self._stats["misses"]
        return {
            "queries": self._stats["queries"],
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "refused": self._stats["refused"],
            "hit_rate": round(self._stats["hits"] / cacheable, 4) if cacheable else 0.0,
            "refusals_by_reason": dict(self._stats["refusals_by_reason"]),
            "stale_count": self._stats["stale_count"],   # 0 by construction
        }

    # ---- introspection (for the dashboard / "map the DB") ----
    def events(self, limit=40):
        """Recent serve/invalidation events, newest first."""
        return list(self._events)[:limit]

    def schema_map(self):
        """Tables, their columns, and which columns are 'watched' (appear in a
        cached query's footprint)."""
        self._refresh()
        watched = {}
        for fp in self._cache.fp_.values():
            if fp[0] == "column":
                for (t, c) in fp[1]:
                    watched.setdefault(t, set()).add(c)
            elif fp[0] == "row":
                watched.setdefault(fp[1], set()).add(fp[2])
        return [
            {"name": t, "columns": list(cols.keys()),
             "watched": sorted(watched.get(t, set()))}
            for t, cols in sorted(self._cache.schema.items())
        ]

    def cached_queries(self):
        """Currently-cached queries with their footprint precision."""
        out = []
        for sql in list(self._cache.store_.keys()):
            fp = self._cache.fp_.get(sql)
            if not fp or fp[0] == "refuse":
                continue
            out.append({
                "sql": " ".join(sql.split())[:120],
                "mode": "row" if fp[0] == "row" else self.mode,
                "tables": sorted({k[0] for k in fp[1]} if fp[0] == "column" else {fp[1]}),
            })
        return out
