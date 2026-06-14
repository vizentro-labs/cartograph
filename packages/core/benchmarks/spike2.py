#!/usr/bin/env python3
"""
Cartograph spike 2 -- does the zero-stale guarantee SURVIVE *automatic*
footprint extraction from arbitrary SQL?

Spike 1 proved footprint invalidation is 0% stale with O(1) version counters,
but footprints were declared BY HAND. This spike replaces that with automatic
extraction via sqlglot (parse + qualify against the schema) and then attacks it
with a differential fuzzer: tens of thousands of random query/mutation steps,
comparing every cache HIT against the live DB. Any mismatch = an unsoundness bug
in extraction.

Soundness contract for the extractor:
  - OVER-approximate: when uncertain, widen (more invalidation), never narrow.
  - Narrow to row/key-level fingerprinting ONLY for a provably-selective
    equality on a PRIMARY KEY in a single-table query.
  - REFUSE TO CACHE (pass through to live) anything we cannot soundly analyze:
    parse failure, columns that don't qualify to a base table, derived tables /
    CTEs (output columns can't be mapped to base columns), unknown/UDF funcs,
    nondeterministic funcs. Uncached-but-correct is fine; stale is not.

Reuses schema / version counters / live-DB ground truth from spike.py.
"""

import random
import hashlib
from collections import defaultdict, Counter

import sqlglot
from sqlglot import exp, parse_one
from sqlglot.optimizer.qualify import qualify

from spike import build_db, Versions, Mutator, result_hash, SEED

# --------------------------------------------------------------------------
# Schema the extractor reasons about (mirrors spike.build_db()).
# --------------------------------------------------------------------------
SCHEMA = {
    "customers":  {"id": "INT", "name": "TEXT", "email": "TEXT",
                   "city": "TEXT", "segment": "TEXT"},
    "orders":     {"id": "INT", "customer_id": "INT", "amount": "REAL",
                   "status": "TEXT", "order_day": "INT"},
    "line_items": {"id": "INT", "order_id": "INT", "sku": "TEXT", "qty": "INT"},
}
PK = {"customers": "id", "orders": "id", "line_items": "id"}

# functions that make a query nondeterministic -> never cacheable
NONDET_NAMES = {"RAND", "RANDOM", "NOW", "CURRENT_TIMESTAMP", "CURRENT_DATE",
                "CURRENT_TIME", "CURRENT_DATETIME", "UNIXEPOCH", "UUID",
                "RANDOMBLOB", "CURRENT_USER", "LAST_INSERT_ROWID"}
NONDET_CLASSES = (exp.Rand, exp.CurrentTimestamp, exp.CurrentDate,
                  exp.CurrentTime, getattr(exp, "CurrentDatetime", exp.CurrentDate))


# --------------------------------------------------------------------------
# Automatic footprint extraction.
# Returns one of:
#   ("refuse", reason)
#   ("column", frozenset_of_keys)            # keys: (table,col) and (table,'*rowset')
#   ("row", table, pk_col, literal_value)    # provably-selective PK lookup
# --------------------------------------------------------------------------
def _literal_to_py(lit):
    s = lit.this
    if lit.is_string:
        return s
    if "." in s or "e" in s.lower():
        return float(s)
    return int(s)


def _flatten_and(e, out):
    if isinstance(e, exp.And):
        _flatten_and(e.this, out)
        _flatten_and(e.expression, out)
    else:
        out.append(e)


def extract_footprint(sql):
    # 1. parse
    try:
        tree = parse_one(sql, dialect="sqlite")
    except Exception:
        return ("refuse", "parse_error")

    # 2. nondeterministic / unknown functions (check pre-qualify; qualify can
    #    rewrite but these node types/names survive)
    for f in tree.find_all(exp.Func):
        if isinstance(f, exp.Anonymous):
            nm = (f.name or "").upper()
            if nm in NONDET_NAMES:
                return ("refuse", "nondeterministic_func")
            return ("refuse", "unknown_func")        # possible UDF -> unsafe
        if isinstance(f, NONDET_CLASSES):
            return ("refuse", "nondeterministic_func")
        if (f.sql_name() or "").upper() in NONDET_NAMES:
            return ("refuse", "nondeterministic_func")

    # 3. qualify columns against schema (validate => raises if unresolvable)
    try:
        q = qualify(tree, schema=SCHEMA, dialect="sqlite",
                    qualify_columns=True, validate_qualify_columns=True,
                    expand_stars=True)
    except Exception:
        return ("refuse", "qualify_failed")

    # 4. every physical table must be a known base table (catches CTEs &
    #    unknown views, which appear as non-schema table refs)
    alias_to_base = {}
    for t in q.find_all(exp.Table):
        if t.name not in SCHEMA:
            return ("refuse", "unknown_table_or_cte")
        alias_to_base[t.alias_or_name] = t.name

    # 5. every column must resolve to a base table. A derived table (subquery in
    #    FROM) leaves its output columns qualified to a non-base alias -> refuse,
    #    because we cannot map them to base columns soundly.
    #
    #    Exception: qualify leaves ORDER BY / GROUP BY / HAVING references to a
    #    SELECT *output* column with an empty qualifier (e.g. `ORDER BY segment`
    #    where `c.segment` is projected). Those are output-alias references whose
    #    underlying base columns are ALREADY captured by the projection, so
    #    skipping them is sound. We only skip when the name matches a known
    #    output; anything else empty-qualified is treated as unresolved -> refuse.
    output_names = set()
    for sel in q.find_all(exp.Select):
        for proj in sel.expressions:
            nm = proj.alias_or_name
            if nm:
                output_names.add(nm)

    cols = set()
    for c in q.find_all(exp.Column):
        ref = c.table
        if not ref:
            if c.name in output_names:
                continue                 # output-alias ref; deps already captured
            return ("refuse", "derived_or_unresolved_column")
        if ref not in alias_to_base:
            return ("refuse", "derived_or_unresolved_column")
        cols.add((alias_to_base[ref], c.name))

    base_tables = set(alias_to_base.values())

    # 6. row-level narrowing: ONLY a single base table, a single (non-nested)
    #    SELECT, and a top-level conjunctive equality on that table's PK.
    if len(base_tables) == 1 and len(list(q.find_all(exp.Select))) == 1:
        tbl = next(iter(base_tables))
        where = q.find(exp.Where)
        if where is not None:
            conds = []
            _flatten_and(where.this, conds)
            for c in conds:
                if isinstance(c, exp.EQ):
                    for a, b in ((c.this, c.expression), (c.expression, c.this)):
                        if (isinstance(a, exp.Column) and a.name == PK[tbl]
                                and isinstance(b, exp.Literal)):
                            return ("row", tbl, PK[tbl], _literal_to_py(b))

    # 7. column-level (sound over-approx): for each referenced base table add its
    #    '*rowset' (inserts/deletes) plus every referenced column (updates).
    keys = set()
    for t in base_tables:
        keys.add((t, "*rowset"))
    keys.update(cols)
    return ("column", tuple(sorted(keys)))


# --------------------------------------------------------------------------
# Caches keyed by exact SQL text.
# --------------------------------------------------------------------------
class FootprintCache:
    def __init__(self, ver, conn):
        self.ver, self.conn, self.store_ = ver, conn, {}

    def _fpval(self, fp):
        if fp[0] == "column":
            return tuple(self.ver.v[k] for k in fp[1])
        _, tbl, pk, lit = fp                      # row-level: read the actual row
        rows = self.conn.execute(
            f"SELECT * FROM {tbl} WHERE {pk}=?", (lit,)).fetchall()
        return result_hash(tuple(rows))

    def lookup(self, sql):
        e = self.store_.get(sql)
        if e and self._fpval(e[1]) == e[2]:
            return e[0], True
        return None, False

    def store(self, sql, fp, result):
        self.store_[sql] = (result, fp, self._fpval(fp))


class AnyWriteCache:
    def __init__(self, ver):
        self.ver, self.store_ = ver, {}

    def lookup(self, sql):
        e = self.store_.get(sql)
        if e and e[1] == self.ver.epoch:
            return e[0], True
        return None, False

    def store(self, sql, result):
        self.store_[sql] = (result, self.ver.epoch)


# --------------------------------------------------------------------------
# Random analytical-workload generator (finite literal pools so queries recur
# and caches can actually hit). Mixes soundly-cacheable shapes with hard ones.
# --------------------------------------------------------------------------
IDS = {"customers": list(range(1, 41)),
       "orders": list(range(1, 41)),
       "line_items": list(range(1, 41))}
STATUS = ["new", "paid", "shipped", "refunded"]
SEG = ["smb", "mid", "ent"]
CITY = ["NYC", "LA", "CHI", "HOU", "PHX", "SF"]
BANDS = [(0, 50), (50, 150), (150, 300), (300, 500), (100, 400), (10, 90)]
DAYS = [(1, 90), (90, 180), (180, 365)]
THRESH = [50, 100, 200, 300]
LIMITS = [5, 10, 20]
HAVK = [1, 3, 5]


def g_point():
    t = random.choice(["customers", "orders", "line_items"])
    base = f"SELECT * FROM {t} WHERE id={random.choice(IDS[t])}"
    if t == "orders" and random.random() < 0.5:
        base += f" AND amount>{random.choice(THRESH)}"   # narrowing w/ extra pred
    return base

def g_range():
    lo, hi = random.choice(BANDS)
    s = f"SELECT id,customer_id,amount FROM orders WHERE amount BETWEEN {lo} AND {hi}"
    if random.random() < 0.5:
        s += f" AND status='{random.choice(STATUS)}'"
    return s + f" ORDER BY id LIMIT {random.choice(LIMITS)}"

def g_agg():
    s = "SELECT COUNT(*), ROUND(COALESCE(SUM(amount),0),2), ROUND(COALESCE(AVG(amount),0),2) FROM orders"
    if random.random() < 0.7:
        s += f" WHERE status='{random.choice(STATUS)}'"
    return s

def g_groupby():
    s = "SELECT customer_id, COUNT(*), ROUND(SUM(amount),2) FROM orders GROUP BY customer_id"
    if random.random() < 0.5:
        s += f" HAVING COUNT(*)>{random.choice(HAVK)}"
    return s + f" ORDER BY 2 DESC, customer_id LIMIT {random.choice(LIMITS)}"

def g_groupby2():
    return "SELECT status, COUNT(*), ROUND(SUM(amount),2) FROM orders GROUP BY status ORDER BY status"

def g_join2():
    s = ("SELECT c.segment, COUNT(*), ROUND(COALESCE(SUM(o.amount),0),2) "
         "FROM customers c JOIN orders o ON o.customer_id=c.id")
    if random.random() < 0.5:
        s += f" WHERE c.segment='{random.choice(SEG)}'"
    return s + " GROUP BY c.segment ORDER BY c.segment"

def g_join3():
    return (f"SELECT c.city, COUNT(DISTINCT o.id), COALESCE(SUM(li.qty),0) "
            f"FROM customers c JOIN orders o ON o.customer_id=c.id "
            f"JOIN line_items li ON li.order_id=o.id "
            f"WHERE c.city='{random.choice(CITY)}' GROUP BY c.city")

def g_star():
    s = f"SELECT * FROM customers WHERE city='{random.choice(CITY)}'"
    if random.random() < 0.5:
        s += f" AND segment='{random.choice(SEG)}'"
    return s + " ORDER BY id"

def g_case():
    lo, hi = random.choice(DAYS)
    return (f"SELECT id, CASE WHEN amount>=200 THEN 'big' WHEN amount>=50 "
            f"THEN 'mid' ELSE 'small' END FROM orders "
            f"WHERE order_day BETWEEN {lo} AND {hi} ORDER BY id LIMIT {random.choice(LIMITS)}")

def g_distinct():
    s = "SELECT DISTINCT city, segment FROM customers"
    if random.random() < 0.5:
        s += f" WHERE segment='{random.choice(SEG)}'"
    return s + " ORDER BY city, segment"

def g_in_sub():       # subquery in predicate -> should be CACHEABLE & sound
    return (f"SELECT COUNT(*) FROM orders WHERE customer_id IN "
            f"(SELECT id FROM customers WHERE segment='{random.choice(SEG)}')")

def g_exists_sub():   # correlated EXISTS -> should be CACHEABLE & sound
    return (f"SELECT c.id, c.name FROM customers c WHERE EXISTS "
            f"(SELECT 1 FROM orders o WHERE o.customer_id=c.id "
            f"AND o.amount>{random.choice(THRESH)}) ORDER BY c.id LIMIT {random.choice(LIMITS)}")

def g_scalar_sub():   # scalar subquery in projection -> CACHEABLE & sound
    return (f"SELECT o.id, (SELECT name FROM customers c WHERE c.id=o.customer_id) "
            f"FROM orders o WHERE o.amount>{random.choice(THRESH)} "
            f"ORDER BY o.id LIMIT {random.choice(LIMITS)}")

# ---- hard shapes that MUST be refused ----
def g_from_sub():     # derived table in FROM -> refuse
    return ("SELECT t.seg, COUNT(*) FROM (SELECT segment AS seg FROM customers) t "
            "GROUP BY t.seg ORDER BY t.seg")

def g_cte():          # CTE -> refuse
    return (f"WITH big AS (SELECT id FROM orders WHERE amount>{random.choice(THRESH)}) "
            f"SELECT COUNT(*) FROM big")

def g_udf():          # unknown/UDF function -> refuse
    return f"SELECT my_udf(amount), id FROM orders WHERE id={random.choice(IDS['orders'])}"

def g_nondet():       # nondeterministic -> refuse
    if random.random() < 0.5:
        return "SELECT * FROM orders WHERE amount > ABS(RANDOM() % 100)"
    return f"SELECT id, CURRENT_DATE FROM customers WHERE id={random.choice(IDS['customers'])}"


GENERATORS = (
    [g_point] * 8 + [g_range] * 10 + [g_agg] * 8 + [g_groupby] * 10 +
    [g_groupby2] * 5 + [g_join2] * 12 + [g_join3] * 6 + [g_star] * 6 +
    [g_case] * 6 + [g_distinct] * 4 + [g_in_sub] * 6 + [g_exists_sub] * 5 +
    [g_scalar_sub] * 4 +
    [g_from_sub] * 4 + [g_cte] * 3 + [g_udf] * 2 + [g_nondet] * 2
)


def run_live(conn, sql):
    return tuple(conn.execute(sql).fetchall())


# --------------------------------------------------------------------------
# Differential fuzzer
# --------------------------------------------------------------------------
def main(n_events=240_000, mutation_fraction=0.2):
    random.seed(SEED)
    conn = build_db()
    ver = Versions()
    mut = Mutator(conn, ver)
    fcache = FootprintCache(ver, conn)
    acache = AnyWriteCache(ver)

    memo = {}
    def extract(sql):
        fp = memo.get(sql)
        if fp is None:
            fp = memo[sql] = extract_footprint(sql)
        return fp

    n_q = n_mut = 0
    cacheable = refused = 0
    n_row = n_col = 0
    f_hits = a_hits = 0
    stale = 0
    reasons = Counter()
    counterexamples = []

    for _ in range(n_events):
        if random.random() < mutation_fraction:
            mut.run()
            n_mut += 1
            continue
        n_q += 1
        sql = random.choice(GENERATORS)()
        fp = extract(sql)

        if fp[0] == "refuse":
            refused += 1
            reasons[fp[1]] += 1
            continue                       # pass-through to live DB (correct)

        cacheable += 1
        if fp[0] == "row":
            n_row += 1
        else:
            n_col += 1

        live = run_live(conn, sql)          # GROUND TRUTH (the oracle)

        served, hit = fcache.lookup(sql)
        if hit:
            f_hits += 1
            if served != live:              # <-- soundness violation
                stale += 1
                if len(counterexamples) < 25:
                    counterexamples.append((sql, fp, served, live))
        else:
            fcache.store(sql, fp, live)

        aserved, ahit = acache.lookup(sql)  # baseline on identical population
        if ahit:
            a_hits += 1
        else:
            acache.store(sql, live)

    # ---------------- report ----------------
    print(f"\n{'='*70}\nSPIKE 2 -- automatic extraction + differential fuzzer\n{'='*70}")
    print(f"events={n_events}  (read-heavy, mutation_fraction={mutation_fraction})")
    print(f"queries={n_q}  mutations={n_mut}   distinct query strings={len(memo)}")

    cov = 100 * cacheable / n_q
    f_hr = 100 * f_hits / cacheable if cacheable else 0
    a_hr = 100 * a_hits / cacheable if cacheable else 0
    stale_rate = 100 * stale / f_hits if f_hits else 0

    print(f"\n--- METRIC 1: SOUNDNESS (the whole point) ---")
    print(f"cache HITs checked against live DB : {f_hits}")
    print(f"STALE hits (cached != live)        : {stale}")
    print(f"STALE-HIT RATE                     : {stale_rate:.4f}%")

    print(f"\n--- METRIC 2: COVERAGE ---")
    print(f"cacheable / total queries          : {cacheable}/{n_q} = {cov:.1f}%")
    print(f"  row-level (PK narrowed)          : {n_row}")
    print(f"  column-level                     : {n_col}")
    print(f"refused                            : {refused}")

    print(f"\n--- METRIC 4: WHY refused (widening triggers) ---")
    for r, c in reasons.most_common():
        print(f"  {r:<28}{c:>7}  ({100*c/n_q:.1f}% of queries)")

    print(f"\n--- METRIC 3: HIT RATE among cacheable (read-heavy) ---")
    print(f"footprint (auto)   hit rate        : {f_hr:.1f}%")
    print(f"invalidate-on-any-write hit rate   : {a_hr:.1f}%")

    # ---- example extractions for transparency ----
    print(f"\n--- sample auto-extracted footprints ---")
    samples = ["SELECT * FROM customers WHERE id=5",
               "SELECT c.segment, COUNT(*), ROUND(COALESCE(SUM(o.amount),0),2) FROM customers c JOIN orders o ON o.customer_id=c.id GROUP BY c.segment ORDER BY c.segment",
               "SELECT COUNT(*) FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE segment='ent')",
               "WITH big AS (SELECT id FROM orders WHERE amount>100) SELECT COUNT(*) FROM big",
               "SELECT my_udf(amount), id FROM orders WHERE id=3"]
    for s in samples:
        fp = extract_footprint(s)
        head = fp[0] if fp[0] != "refuse" else f"refuse:{fp[1]}"
        body = fp[1] if fp[0] == "column" else (fp[1:] if fp[0] == "row" else "")
        print(f"  [{head}] {s[:60]}")
        if body:
            print(f"        -> {body}")

    # ---------------- gates ----------------
    print(f"\n=== SUCCESS GATES ===")
    g1 = stale == 0
    print(f"Gate 1  0.000% stale under auto-extraction : "
          f"{'PASS' if g1 else 'FAIL'}  ({stale_rate:.4f}%, {f_hits} hits checked)")
    g2 = cov >= 70.0
    print(f"Gate 2  coverage >= 70% (viability)        : "
          f"{'PASS' if g2 else 'FAIL'}  ({cov:.1f}%)")
    g3 = f_hr > a_hr + 1.0
    print(f"Gate 3  footprint hit% >> on-any-write     : "
          f"{'PASS' if g3 else 'FAIL'}  ({f_hr:.1f}% vs {a_hr:.1f}%)")

    if counterexamples:
        print(f"\n!!! {stale} SOUNDNESS COUNTEREXAMPLES (showing up to 25) !!!")
        for sql, fp, served, live in counterexamples:
            print(f"\n  SQL : {sql}")
            print(f"  FP  : {fp}")
            print(f"  cached: {str(served)[:120]}")
            print(f"  live  : {str(live)[:120]}")

    return dict(stale=stale, stale_rate=stale_rate, cov=cov, f_hr=f_hr,
                a_hr=a_hr, f_hits=f_hits, reasons=reasons, n_row=n_row,
                n_col=n_col, cacheable=cacheable, refused=refused, n_q=n_q,
                g1=g1, g2=g2, g3=g3)


if __name__ == "__main__":
    main()
