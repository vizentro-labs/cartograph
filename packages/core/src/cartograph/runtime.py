"""
Production substrate (graduated from spike 4, unchanged algorithm):
  - WalVersions: version counters derived purely from Postgres logical decoding
    (plugin=test_decoding). INSERT/DELETE -> bump (table,"*rowset"); UPDATE ->
    diff old/new tuple (REPLICA IDENTITY FULL) -> bump exactly changed columns.
  - Cache: footprint cache keyed by SQL text; fingerprints from the counters;
    invalidation by changed table; refuse on tables missing from the schema.
  - on_schema_change: type-aware schema diff -> invalidate touched tables.
  - bootstrap(): create the demo schema + slot + DDL event trigger.

Everything is driven by `config` (DSN/SLOT/MODE/DIALECT/TABLES/PK) so the WAL
reader, cache, and extractor share one source of truth.
"""

import re
import random
import hashlib
from collections import defaultdict

import psycopg2
import psycopg2.extras
from sqlglot import parse_one, exp

from . import config
from .extract import extract


def conn(autocommit=True):
    c = psycopg2.connect(config.DSN)
    c.autocommit = autocommit
    return c


def rhash(rows):
    return hashlib.blake2b(repr(rows).encode(), digest_size=16).hexdigest()


# --------------------------------------------------------------------------
# schema introspection
# --------------------------------------------------------------------------
def read_schema():
    """{table: {col: data_type}} for the tracked tables. Types are kept because a
    column TYPE change can alter results and must invalidate (footprint ignores
    types, invalidation does not)."""
    c = conn(); cur = c.cursor()
    cur.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                   WHERE table_schema='public' AND table_name = ANY(%s)
                   ORDER BY table_name, ordinal_position""", (config.TABLES,))
    sch = defaultdict(dict)
    for t, col, typ in cur.fetchall():
        sch[t][col] = typ
    c.close()
    return {t: dict(cols) for t, cols in sch.items()}


def discover_tables_pk():
    """Auto-discover base tables + primary keys from the catalog (excludes the
    cg_meta signalling table)."""
    c = conn(); cur = c.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE'
                     AND table_name <> 'cg_meta' ORDER BY table_name""")
    tables = [r[0] for r in cur.fetchall()]
    pk = {}
    for t in tables:
        cur.execute("""SELECT a.attname FROM pg_index i
            JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = %s::regclass AND i.indisprimary""", (t,))
        row = cur.fetchone()
        if row:
            pk[t] = row[0]
    c.close()
    return tables, pk


# --------------------------------------------------------------------------
# WAL reader -> version counters
# --------------------------------------------------------------------------
_TOK = re.compile(r"(\w+)\[[^\]]+\]:")


def _cols_with_values(segment):
    """Parse 'col[type]:value col2[type]:value2 ...' -> {col: rawvalue}."""
    out = {}
    matches = list(_TOK.finditer(segment))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(segment)
        out[m.group(1)] = segment[start:end].strip()
    return out


class WalVersions:
    """Version counters derived purely from the Postgres WAL change stream."""

    def __init__(self):
        self.v = defaultdict(int)
        self.schema_dirty = False
        self._rdr = conn()

    def bump(self, table, col):
        self.v[(table, col)] += 1

    def drain(self):
        """Synchronously decode all committed WAL up to the current LSN and apply
        bumps. Returns the set of (table,col) keys bumped in this drain."""
        cur = self._rdr.cursor()
        cur.execute("SELECT data FROM pg_logical_slot_get_changes(%s, NULL, NULL)", (config.SLOT,))
        bumped = set()
        for (data,) in cur.fetchall():
            if not data.startswith("table "):
                continue
            try:
                head, rest = data[len("table "):].split(": ", 1)
                tname = head.split(".", 1)[1]
                op, payload = rest.split(":", 1) if ":" in rest else (rest, "")
                op = op.strip()
            except ValueError:
                continue
            if tname == "cg_meta":
                self.schema_dirty = True
                continue
            if tname not in config.TABLES:
                continue
            if op == "INSERT" or op == "DELETE":
                self.bump(tname, "*rowset"); bumped.add((tname, "*rowset"))
            elif op == "UPDATE":
                if "new-tuple:" in payload:
                    old_s, new_s = payload.split("new-tuple:", 1)
                    old_s = old_s.replace("old-key:", "")
                    old = _cols_with_values(old_s)
                    new = _cols_with_values(new_s)
                    changed = [c for c in new if old.get(c) != new.get(c)] or list(new)
                else:
                    changed = list(_cols_with_values(payload))   # fallback: bump all seen
                for col in changed:
                    self.bump(tname, col); bumped.add((tname, col))
        return bumped


# --------------------------------------------------------------------------
# footprint cache
# --------------------------------------------------------------------------
class Cache:
    def __init__(self, ver, schema):
        self.ver = ver
        self.schema = schema
        self.store_ = {}
        self.fp_ = {}   # sql -> extracted footprint (re-extracted on schema change)

    def footprint(self, sql):
        if sql not in self.fp_:
            self.fp_[sql] = self._extract(sql)
        return self.fp_[sql]

    def _extract(self, sql):
        # Guard: a base table referenced by the query but absent from the current
        # schema snapshot (e.g. DROPped out-of-band) must REFUSE -- coarse mode
        # would otherwise mistake it for a CTE alias and silently drop it.
        try:
            tree = parse_one(sql, dialect=config.DIALECT)
            cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
            unknown = {t.name for t in tree.find_all(exp.Table)
                       if t.name not in self.schema and t.name not in cte_names}
            if unknown:
                return ("refuse", "unknown_or_dropped_table")
        except Exception:
            pass
        return extract(sql, self.schema, config.PK, config.DIALECT, config.MODE)

    def fingerprint(self, fp, qconn):
        if fp[0] == "column":
            return tuple(self.ver.v[k] for k in fp[1])
        _, t, pkc, lit = fp                  # row-level
        cur = qconn.cursor()
        cur.execute(f"SELECT * FROM {t} WHERE {pkc}=%s", (lit,))
        return rhash(cur.fetchall())

    def invalidate_tables(self, tables):
        """Drop every cached entry whose footprint references a changed table."""
        drop = []
        for sql, fp in self.fp_.items():
            refs = {k[0] for k in fp[1]} if fp[0] == "column" else {fp[1]}
            if refs & tables:
                drop.append(sql)
        for sql in drop:
            self.store_.pop(sql, None)
            self.fp_.pop(sql, None)        # force re-extract against new schema
        return len(drop)

    def lookup(self, sql, qconn):
        fp = self.footprint(sql)
        if fp[0] == "refuse":
            return ("refuse", fp[1]), False
        e = self.store_.get(sql)
        if e and self.fingerprint(fp, qconn) == e[1]:
            return e[0], True
        return None, False

    def store(self, sql, result, qconn):
        fp = self.footprint(sql)
        if fp[0] != "refuse":
            self.store_[sql] = (result, self.fingerprint(fp, qconn))


def on_schema_change(cache, ver):
    """Re-read schema, invalidate footprints touching changed tables, refresh."""
    new_schema = read_schema()
    old, new = cache.schema, new_schema
    changed = set()
    for t in set(old) | set(new):
        # type-aware: dropped/added table, or any (column set OR column type) change
        if old.get(t) != new.get(t):
            changed.add(t)
    cache.schema = new_schema
    n = cache.invalidate_tables(changed) if changed else 0
    ver.schema_dirty = False
    return changed, n


# --------------------------------------------------------------------------
# demo bootstrap: schema (REPLICA IDENTITY FULL), seed, DDL trigger, WAL slot
# --------------------------------------------------------------------------
def bootstrap(seed=1234):
    rnd = random.Random(seed)
    c = conn(); cur = c.cursor()
    cur.execute("DROP EVENT TRIGGER IF EXISTS cg_ddl_end")      # drop triggers FIRST
    cur.execute("DROP EVENT TRIGGER IF EXISTS cg_ddl_drop")     # else they fire on the drops below
    cur.execute("DROP TABLE IF EXISTS line_items, orders, customers, cg_meta CASCADE")
    cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name=%s", (config.SLOT,))
    if cur.fetchone():
        cur.execute("SELECT pg_drop_replication_slot(%s)", (config.SLOT,))

    cur.execute("""
        CREATE TABLE customers(id int PRIMARY KEY, name text, email text, city text, segment text);
        CREATE TABLE orders(id int PRIMARY KEY, customer_id int, amount numeric(10,2), status text, order_day int);
        CREATE TABLE line_items(id int PRIMARY KEY, order_id int, sku text, qty int);
    """)
    for t in ("customers", "orders", "line_items"):
        cur.execute(f"ALTER TABLE {t} REPLICA IDENTITY FULL")   # full old-tuple in WAL

    cities = ["NYC", "LA", "CHI", "HOU", "PHX", "SF"]
    segs = ["smb", "mid", "ent"]
    stats = ["new", "paid", "shipped", "refunded"]
    psycopg2.extras.execute_values(cur, "INSERT INTO customers VALUES %s",
        [(i, f"cust{i}", f"cust{i}@ex.com", rnd.choice(cities), rnd.choice(segs))
         for i in range(1, 801)])
    psycopg2.extras.execute_values(cur, "INSERT INTO orders VALUES %s",
        [(i, rnd.randint(1, 800), round(rnd.uniform(5, 500), 2),
          rnd.choice(stats), rnd.randint(1, 365)) for i in range(1, 4001)])
    psycopg2.extras.execute_values(cur, "INSERT INTO line_items VALUES %s",
        [(i, rnd.randint(1, 4000), f"SKU{rnd.randint(1,200)}", rnd.randint(1, 9))
         for i in range(1, 9001)])

    # DDL signal: event trigger bumps cg_meta -> rides the WAL stream
    cur.execute("CREATE TABLE cg_meta(id int PRIMARY KEY, schema_version int)")
    cur.execute("INSERT INTO cg_meta VALUES (1,0)")
    cur.execute("""
        CREATE OR REPLACE FUNCTION cg_on_ddl() RETURNS event_trigger AS $$
        BEGIN UPDATE cg_meta SET schema_version = schema_version + 1 WHERE id=1; END
        $$ LANGUAGE plpgsql;
    """)
    cur.execute("CREATE EVENT TRIGGER cg_ddl_end ON ddl_command_end EXECUTE FUNCTION cg_on_ddl()")
    cur.execute("CREATE EVENT TRIGGER cg_ddl_drop ON sql_drop EXECUTE FUNCTION cg_on_ddl()")
    # create slot AFTER seeding so the baseline isn't in the stream
    cur.execute("SELECT pg_create_logical_replication_slot(%s,'test_decoding')", (config.SLOT,))
    c.close()
