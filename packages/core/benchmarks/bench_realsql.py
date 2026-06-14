#!/usr/bin/env python3
"""
Cartograph spike 3 -- REAL-WORLD coverage, and does a cheap conservative
fallback make CTEs cacheable without giving up zero-stale?

Spikes 1-2 established: footprint invalidation is 0% stale, and automatic
extraction via sqlglot column lineage is sound -- but spike 2 REFUSED any query
with a CTE or derived table, and real analytical SQL is full of them. This spike:

  1. Measures coverage on a REAL corpus -- the 103 TPC-DS queries (the standard
     warehouse benchmark, CTE/subquery-heavy) + real dbt (jaffle_shop) models.
  2. Adds a 'coarse-fallback' mode: instead of refusing CTEs, build a SOUND
     over-approximate footprint = union of every (base_table, column) referenced
     anywhere in the AST + a '*rowset' sentinel for every base source table.
     No lineage-through-subquery needed.
  3. Adds a 'lineage-through' mode: run sqlglot's projection-pushdown optimizer
     (semantics-preserving) before extraction, dropping CTE columns that don't
     affect the result -> a tighter, still-sound footprint.

The differential fuzzer (spike 2's oracle) re-validates: coarse-fallback AND
lineage-through must stay 0.000% stale across tens of thousands of live-checked
hits on executable CTE/derived/window queries, under random mutation of every
base table/column.

Corpus is fetched/cached under ./corpus_cache (gitignored; not committed --
TPC-DS SQL carries the TPC license). Coverage is a parse-only metric on the real
corpus. Soundness + hit-rate need a live DB, so they run on an executable
CTE-heavy corpus over spike.py's customers/orders/line_items schema (reusing its
version counters + mutator). That split is a threat to validity, noted in VERDICT.
"""

import os
import re
import glob
import random
import urllib.request
from collections import Counter, defaultdict

import sqlglot
from sqlglot import exp, parse_one
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.pushdown_projections import pushdown_projections

from spike import build_db, Versions, Mutator, result_hash, SEED

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus_cache")

NONDET_NAMES = {"RAND", "RANDOM", "NOW", "CURRENT_TIMESTAMP", "CURRENT_DATE",
                "CURRENT_TIME", "CURRENT_DATETIME", "UNIXEPOCH", "UUID",
                "RANDOMBLOB", "CURRENT_USER", "LAST_INSERT_ROWID"}
NONDET_CLASSES = (exp.Rand, exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentTime)


# ==========================================================================
# Corpus fetch / load
# ==========================================================================
def _fetch(url, dest):
    if os.path.exists(dest):
        return True
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with urllib.request.urlopen(url, timeout=20) as r:
            open(dest, "wb").write(r.read())
        return True
    except Exception:
        return False


def ensure_corpus():
    base = "https://raw.githubusercontent.com/apache/spark/master/sql/core/src/test/resources/tpcds/"
    names = [f"q{i}" for i in range(1, 100)] + ["q14a", "q14b", "q23a", "q23b",
                                                "q24a", "q24b", "q39a", "q39b"]
    for n in names:
        _fetch(base + n + ".sql", os.path.join(CACHE, "tpcds", n + ".sql"))
    _fetch("https://raw.githubusercontent.com/gregrahn/tpcds-kit/master/tools/tpcds.sql",
           os.path.join(CACHE, "tpcds_ddl.sql"))


def schema_from_ddl(path, dialect="postgres"):
    schema = {}
    for stmt in sqlglot.parse(open(path).read(), dialect=dialect):
        if isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.Schema):
            cols = {c.name: (c.kind.sql() if c.kind else "?")
                    for c in stmt.this.expressions if isinstance(c, exp.ColumnDef)}
            if cols:
                schema[stmt.this.this.name] = cols
    return schema


def load_tpcds():
    files = sorted(glob.glob(os.path.join(CACHE, "tpcds", "q*.sql")),
                   key=lambda p: os.path.basename(p))
    return [(os.path.basename(f)[:-4], open(f).read().strip().rstrip(";")) for f in files]


# ----- jaffle_shop (real dbt) -----
JAFFLE_SCHEMA = {
    "raw_customers": {"id": "INT", "first_name": "TEXT", "last_name": "TEXT"},
    "raw_orders":    {"id": "INT", "user_id": "INT", "order_date": "DATE", "status": "TEXT"},
    "raw_payments":  {"id": "INT", "order_id": "INT", "payment_method": "TEXT", "amount": "INT"},
    "stg_customers": {"customer_id": "INT", "first_name": "TEXT", "last_name": "TEXT"},
    "stg_orders":    {"order_id": "INT", "customer_id": "INT", "order_date": "DATE", "status": "TEXT"},
    "stg_payments":  {"payment_id": "INT", "order_id": "INT", "payment_method": "TEXT", "amount": "REAL"},
}
JAFFLE_ORDERS_COMPILED = """
with orders as (select * from stg_orders),
order_payments as (
    select order_id,
        sum(case when payment_method = 'credit_card' then amount else 0 end) as credit_card_amount,
        sum(case when payment_method = 'coupon' then amount else 0 end) as coupon_amount,
        sum(case when payment_method = 'bank_transfer' then amount else 0 end) as bank_transfer_amount,
        sum(case when payment_method = 'gift_card' then amount else 0 end) as gift_card_amount,
        sum(amount) as total_amount
    from stg_payments group by order_id
),
final as (
    select orders.order_id, orders.customer_id, orders.order_date, orders.status,
        order_payments.credit_card_amount, order_payments.coupon_amount,
        order_payments.bank_transfer_amount, order_payments.gift_card_amount,
        order_payments.total_amount as amount
    from orders left join order_payments on orders.order_id = order_payments.order_id
)
select * from final
"""


def _compile_jinja(sql):
    sql = re.sub(r"\{#.*?#\}", "", sql, flags=re.S)
    sql = re.sub(r"\{\{\s*ref\(\s*'([^']+)'\s*\)\s*\}\}", r"\1", sql)
    sql = re.sub(r"\{\{\s*source\(\s*'[^']+'\s*,\s*'([^']+)'\s*\)\s*\}\}", r"\1", sql)
    return sql


def load_jaffle():
    out = []
    for name in ["customers", "stg_customers", "stg_orders", "stg_payments"]:
        p = os.path.join(CACHE, "dbt", name + ".sql")
        if os.path.exists(p):
            out.append((name, _compile_jinja(open(p).read())))
    out.append(("orders", JAFFLE_ORDERS_COMPILED))
    return out


# ==========================================================================
# Extraction -- three modes. Returns:
#   ("refuse", reason) | ("column", keys_tuple) | ("row", table, pk, literal)
# ==========================================================================
def _func_refusal(tree):
    for f in tree.find_all(exp.Func):
        if isinstance(f, exp.Anonymous):
            nm = (f.name or "").upper()
            return "nondeterministic_func" if nm in NONDET_NAMES else "unknown_func"
        if isinstance(f, NONDET_CLASSES):
            return "nondeterministic_func"
        if (f.sql_name() or "").upper() in NONDET_NAMES:
            return "nondeterministic_func"
    return None


def _flatten_and(e, out):
    if isinstance(e, exp.And):
        _flatten_and(e.this, out); _flatten_and(e.expression, out)
    else:
        out.append(e)


def _lit_to_py(lit):
    s = lit.this
    if lit.is_string:
        return s
    return float(s) if ("." in s or "e" in s.lower()) else int(s)


def _try_row(q, base_tables, pk):
    if len(base_tables) != 1 or len(list(q.find_all(exp.Select))) != 1:
        return None
    tbl = next(iter(base_tables))
    pkcol = pk.get(tbl)
    where = q.find(exp.Where)
    if not pkcol or where is None:
        return None
    conds = []
    _flatten_and(where.this, conds)
    for c in conds:
        if isinstance(c, exp.EQ):
            for a, b in ((c.this, c.expression), (c.expression, c.this)):
                if isinstance(a, exp.Column) and a.name == pkcol and isinstance(b, exp.Literal):
                    return ("row", tbl, pkcol, _lit_to_py(b))
    return None


def _base_cols(q, schema):
    """(base_alias_map, base_cols, nonbase_col?, unresolved?) from a qualified tree."""
    base_alias = {}
    for t in q.find_all(exp.Table):
        if t.name in schema:
            base_alias[t.alias_or_name] = t.name
    out_names = set()
    for sel in q.find_all(exp.Select):
        for p in sel.expressions:
            if p.alias_or_name:
                out_names.add(p.alias_or_name)
    cols = set()
    nonbase = unresolved = False
    for c in q.find_all(exp.Column):
        ref = c.table
        if not ref:
            if c.name in out_names:
                continue
            unresolved = True
            continue
        if ref in base_alias:
            cols.add((base_alias[ref], c.name))
        else:
            nonbase = True
    return base_alias, cols, nonbase, unresolved


def extract(sql, schema, pk, dialect, mode):
    try:
        tree = parse_one(sql, dialect=dialect)
    except Exception:
        return ("refuse", "parse_error")

    r = _func_refusal(tree)               # legitimately-uncacheable in ALL modes
    if r:
        return ("refuse", r)

    try:
        q = qualify(tree.copy(), schema=schema, dialect=dialect,
                    qualify_columns=True, validate_qualify_columns=True, expand_stars=True)
    except Exception:
        return ("refuse", "qualify_failed")

    has_cte = q.find(exp.With) is not None
    has_deriv = any(isinstance(s.parent, (exp.From, exp.Join))
                    for s in q.find_all(exp.Subquery))
    base_alias, base_cols, nonbase, unresolved = _base_cols(q, schema)
    base_tables = set(base_alias.values())
    if not base_tables:
        return ("refuse", "no_base_tables")

    if mode == "refuse":                  # spike 2 behavior (baseline)
        if has_cte or has_deriv or nonbase or unresolved:
            return ("refuse", "cte_or_derived")
        rn = _try_row(q, base_tables, pk)
        if rn:
            return rn
        keys = {(t, "*rowset") for t in base_tables} | base_cols
        return ("column", tuple(sorted(keys)))

    # ---- coarse / lineage (never refuse on CTE/derived) ----
    if unresolved:
        return ("refuse", "unresolved_column")     # safety; very rare

    if mode == "lineage" and (has_cte or has_deriv):
        try:
            q2 = qualify(tree.copy(), schema=schema, dialect=dialect,
                         qualify_columns=True, validate_qualify_columns=True,
                         expand_stars=True)
            q2 = pushdown_projections(q2, schema=schema)   # drop unused projections
            ba2 = {t.alias_or_name: t.name for t in q2.find_all(exp.Table)
                   if t.name in schema}
            pruned = {(ba2[c.table], c.name) for c in q2.find_all(exp.Column)
                      if c.table in ba2}
            if pruned:
                base_cols = pruned
                base_tables = set(ba2.values()) or base_tables
        except Exception:
            pass                          # fall back to coarse cols (still sound)

    keys = {(t, "*rowset") for t in base_tables} | base_cols
    if not has_cte and not has_deriv and len(base_tables) == 1:
        rn = _try_row(q, base_tables, pk)
        if rn:
            return rn
    return ("column", tuple(sorted(keys)))


# ==========================================================================
# Caches (keyed by SQL text) -- reused by the fuzzer
# ==========================================================================
class FootprintCache:
    def __init__(self, ver, conn):
        self.ver, self.conn, self.store_ = ver, conn, {}

    def _fpval(self, fp):
        if fp[0] == "column":
            return tuple(self.ver.v[k] for k in fp[1])
        _, tbl, pkc, lit = fp
        return result_hash(tuple(self.conn.execute(
            f"SELECT * FROM {tbl} WHERE {pkc}=?", (lit,)).fetchall()))

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
        return (e[0], True) if e and e[1] == self.ver.epoch else (None, False)

    def store(self, sql, result):
        self.store_[sql] = (result, self.ver.epoch)


# ==========================================================================
# Executable CTE/derived/window corpus over spike's schema (for the fuzzer)
# ==========================================================================
EXEC_SCHEMA = {
    "customers":  {"id": "INT", "name": "TEXT", "email": "TEXT", "city": "TEXT", "segment": "TEXT"},
    "orders":     {"id": "INT", "customer_id": "INT", "amount": "REAL", "status": "TEXT", "order_day": "INT"},
    "line_items": {"id": "INT", "order_id": "INT", "sku": "TEXT", "qty": "INT"},
}
EXEC_PK = {"customers": "id", "orders": "id", "line_items": "id"}

TH = [50, 100, 200]
ST = ["new", "paid", "shipped", "refunded"]
SEG = ["smb", "mid", "ent"]
LIM = [10, 20]
BANDS = [(0, 100), (100, 300), (50, 500)]
KK = [1, 3]


def gx_stacked():
    th = random.choice(TH)
    return (f"WITH c AS (SELECT id, segment FROM customers), "
            f"o AS (SELECT customer_id, amount FROM orders WHERE amount>{th}), "
            f"j AS (SELECT c.segment AS seg, o.amount AS amt FROM c JOIN o ON o.customer_id=c.id) "
            f"SELECT seg, COUNT(*), ROUND(SUM(amt),2) FROM j GROUP BY seg ORDER BY seg")

def gx_derived_agg():
    return (f"SELECT band, COUNT(*) FROM (SELECT CASE WHEN amount>=200 THEN 'big' ELSE 'small' END AS band "
            f"FROM orders WHERE status='{random.choice(ST)}') t GROUP BY band ORDER BY band")

def gx_unused_orders():     # CTE selects status,order_day that never reach result
    return (f"WITH e AS (SELECT customer_id AS cid, amount AS amt, status AS st, order_day AS od FROM orders) "
            f"SELECT cid, ROUND(SUM(amt),2) FROM e GROUP BY cid ORDER BY cid LIMIT {random.choice(LIM)}")

def gx_unused_customers():  # CTE selects name,email,city,id; only segment used downstream
    return ("WITH e AS (SELECT id, name, email, city, segment FROM customers) "
            "SELECT segment, COUNT(*) FROM e GROUP BY segment ORDER BY segment")

def gx_corr_cte():
    return (f"WITH ctr AS (SELECT customer_id AS cid, SUM(amount) AS tot FROM orders GROUP BY customer_id) "
            f"SELECT cid FROM ctr c1 WHERE tot > (SELECT AVG(tot)*1.2 FROM ctr c2) "
            f"ORDER BY cid LIMIT {random.choice(LIM)}")

def gx_window():
    th = random.choice(TH)
    return (f"SELECT id, customer_id, amount, "
            f"ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY amount DESC, id) AS rn "
            f"FROM orders WHERE amount>{th} ORDER BY customer_id, amount DESC, id LIMIT {random.choice(LIM)}")

def gx_multi_cte_li():
    return (f"WITH oi AS (SELECT id AS oid, customer_id AS cid, amount AS amt FROM orders), "
            f"liq AS (SELECT order_id, SUM(qty) AS q FROM line_items GROUP BY order_id) "
            f"SELECT oi.cid, ROUND(SUM(oi.amt),2), COALESCE(SUM(liq.q),0) "
            f"FROM oi LEFT JOIN liq ON liq.order_id=oi.oid GROUP BY oi.cid ORDER BY oi.cid LIMIT {random.choice(LIM)}")

def gx_nested_cte():
    th2 = random.choice([100, 300])
    return (f"WITH a AS (SELECT customer_id AS cid, amount AS amt FROM orders WHERE status='{random.choice(ST)}'), "
            f"b AS (SELECT cid, SUM(amt) AS s FROM a GROUP BY cid) "
            f"SELECT cid, ROUND(s,2) FROM b WHERE s>{th2} ORDER BY cid LIMIT {random.choice(LIM)}")

def gx_derived_join():
    lo, hi = random.choice(BANDS)
    return (f"SELECT seg, ROUND(SUM(amt),2) FROM (SELECT c.segment AS seg, o.amount AS amt "
            f"FROM customers c JOIN orders o ON o.customer_id=c.id WHERE o.amount BETWEEN {lo} AND {hi}) t "
            f"GROUP BY seg ORDER BY seg")

def gx_having_cte():
    return (f"WITH o AS (SELECT customer_id AS cid, amount AS amt FROM orders) "
            f"SELECT cid, COUNT(*), ROUND(SUM(amt),2) FROM o GROUP BY cid "
            f"HAVING COUNT(*)>{random.choice(KK)} ORDER BY cid LIMIT {random.choice(LIM)}")

def gx_in_sub():            # no CTE -> cacheable in ALL modes (incl refuse)
    return (f"SELECT COUNT(*) FROM orders WHERE customer_id IN "
            f"(SELECT id FROM customers WHERE segment='{random.choice(SEG)}')")

def gx_exists():           # correlated, no CTE -> cacheable in all modes
    return (f"SELECT c.id, c.name FROM customers c WHERE EXISTS "
            f"(SELECT 1 FROM orders o WHERE o.customer_id=c.id AND o.amount>{random.choice(TH)}) "
            f"ORDER BY c.id LIMIT {random.choice(LIM)}")

def gx_udf():
    return f"SELECT my_udf(amount) FROM orders WHERE id={random.randint(1,40)}"

def gx_nondet():
    return "SELECT * FROM orders WHERE amount > ABS(RANDOM() % 100)"


EXEC_GENERATORS = (
    [gx_stacked] * 7 + [gx_derived_agg] * 6 + [gx_unused_orders] * 8 +
    [gx_unused_customers] * 8 + [gx_corr_cte] * 5 + [gx_window] * 6 +
    [gx_multi_cte_li] * 6 + [gx_nested_cte] * 6 + [gx_derived_join] * 6 +
    [gx_having_cte] * 6 + [gx_in_sub] * 5 + [gx_exists] * 5 +
    [gx_udf] * 2 + [gx_nondet] * 2
)


# ==========================================================================
# PART A: coverage on the real corpus (parse-only)
# ==========================================================================
def coverage(name, queries, schema, pk, dialect):
    print(f"\n{'='*72}\nCOVERAGE -- {name}  ({len(queries)} queries, dialect={dialect})\n{'='*72}")
    cte = deriv = 0
    for _, sql in queries:
        try:
            t = parse_one(sql, dialect=dialect)
            if t.find(exp.With):
                cte += 1
            if any(isinstance(s.parent, (exp.From, exp.Join)) for s in t.find_all(exp.Subquery)):
                deriv += 1
        except Exception:
            pass
    print(f"contain CTE: {cte}   contain derived-table-in-FROM: {deriv}")

    rows = {}
    for mode in ("refuse", "coarse", "lineage"):
        cacheable = 0
        reasons = Counter()
        for _, sql in queries:
            d = extract(sql, schema, pk, dialect, mode)
            if d[0] == "refuse":
                reasons[d[1]] += 1
            else:
                cacheable += 1
        cov = 100 * cacheable / len(queries)
        rows[mode] = (cacheable, cov, reasons)
        print(f"\n  mode={mode:8}  cacheable={cacheable}/{len(queries)} = {cov:.1f}%")
        for r, c in reasons.most_common():
            print(f"      refused: {r:<24}{c}")
    return rows


# ==========================================================================
# PART B: differential fuzzer (soundness + hit rate) on executable CTE corpus
# ==========================================================================
def fuzz(modes=("refuse", "coarse", "lineage"), n_events=200_000, mutation_fraction=0.2):
    random.seed(SEED)
    conn = build_db()
    ver = Versions()
    mut = Mutator(conn, ver)

    fcaches = {m: FootprintCache(ver, conn) for m in modes}
    acache = AnyWriteCache(ver)
    memo = {m: {} for m in modes}

    def ext(sql, m):
        d = memo[m].get(sql)
        if d is None:
            d = memo[m][sql] = extract(sql, EXEC_SCHEMA, EXEC_PK, "sqlite", m)
        return d

    stats = {m: dict(cacheable=0, hits=0, stale=0, refused=0) for m in modes}
    a_stats = dict(hits=0, q=0)
    counterexamples = {m: [] for m in modes}
    # focused: hit accounting on the two "unused-column" CTE shapes
    unused_sqls = set()
    focus = {m: dict(hits=0, q=0) for m in modes}

    n_q = 0
    for _ in range(n_events):
        if random.random() < mutation_fraction:
            mut.run()
            continue
        gen = random.choice(EXEC_GENERATORS)
        sql = gen()
        is_focus = gen in (gx_unused_orders, gx_unused_customers)
        n_q += 1

        live = None
        for m in modes:
            d = ext(sql, m)
            st = stats[m]
            if d[0] == "refuse":
                st["refused"] += 1
                continue
            st["cacheable"] += 1
            if is_focus:
                focus[m]["q"] += 1
            if live is None:
                live = tuple(conn.execute(sql).fetchall())     # GROUND TRUTH (once)
            served, hit = fcaches[m].lookup(sql)
            if hit:
                st["hits"] += 1
                if is_focus:
                    focus[m]["hits"] += 1
                if served != live:
                    st["stale"] += 1
                    if len(counterexamples[m]) < 20:
                        counterexamples[m].append((sql, d, served, live))
            else:
                fcaches[m].store(sql, d, live)

        # any-write baseline on the same population (any cacheable-by-coarse query)
        if any(ext(sql, m)[0] != "refuse" for m in modes):
            if live is None:
                live = tuple(conn.execute(sql).fetchall())
            a_stats["q"] += 1
            aserved, ahit = acache.lookup(sql)
            if ahit:
                a_stats["hits"] += 1
            else:
                acache.store(sql, live)

    return dict(stats=stats, a_stats=a_stats, focus=focus, n_q=n_q,
                counterexamples=counterexamples, modes=modes)


# ==========================================================================
def main():
    ensure_corpus()
    tpcds_schema = schema_from_ddl(os.path.join(CACHE, "tpcds_ddl.sql"))
    tpcds_pk = {}   # narrowing not relevant for the coverage metric
    tpcds = load_tpcds()
    jaffle = load_jaffle()

    print("#" * 72)
    print("# SPIKE 3 -- real-world coverage + coarse-fallback for CTEs")
    print("#" * 72)

    cov_tpcds = coverage("TPC-DS", tpcds, tpcds_schema, tpcds_pk, "spark")
    cov_jaffle = coverage("dbt jaffle_shop", jaffle, JAFFLE_SCHEMA, {}, "duckdb")

    print(f"\n{'#'*72}\n# PART B: differential fuzzer (soundness + hit rate, executable CTE corpus)\n{'#'*72}")
    fz = fuzz()
    modes = fz["modes"]
    st = fz["stats"]
    nq = fz["n_q"]

    print(f"\nexecutable CTE/derived/window workload: {nq} query events, read-heavy")
    hdr = f"{'mode':<10}{'cacheable%':>12}{'hit%(cacheable)':>17}{'stale-hit%':>13}{'hits checked':>14}"
    print(hdr); print("-" * len(hdr))
    for m in modes:
        s = st[m]
        cov = 100 * s["cacheable"] / nq
        hr = 100 * s["hits"] / s["cacheable"] if s["cacheable"] else 0
        sr = 100 * s["stale"] / s["hits"] if s["hits"] else 0
        print(f"{m:<10}{cov:>11.1f}%{hr:>16.1f}%{sr:>12.4f}%{s['hits']:>14}")
    a = fz["a_stats"]
    a_hr = 100 * a["hits"] / a["q"] if a["q"] else 0
    print(f"{'any-write':<10}{'-':>12}{a_hr:>16.1f}%{0.0:>12.4f}%{a['hits']:>14}")

    # precision penalty of coarse vs lineage on the unused-column CTE shapes
    print(f"\n--- precision penalty: coarse vs lineage on 'CTE selects unused columns' ---")
    for m in modes:
        f = fz["focus"][m]
        hr = 100 * f["hits"] / f["q"] if f["q"] else 0
        print(f"  {m:<10} hit% on unused-col CTEs = {hr:.1f}%   ({f['hits']}/{f['q']})")

    # ---------- gates ----------
    print(f"\n{'='*72}\n=== SUCCESS GATES ===\n{'='*72}")
    coarse_stale = st["coarse"]["stale"]
    lin_stale = st["lineage"]["stale"]
    g1 = coarse_stale == 0 and lin_stale == 0
    print(f"Gate 1  0.000% stale for coarse AND lineage (real-shape fuzz) : "
          f"{'PASS' if g1 else 'FAIL'}  (coarse {coarse_stale}, lineage {lin_stale} stale; "
          f"{st['coarse']['hits']}+{st['lineage']['hits']} hits checked)")

    coarse_cov_tpcds = cov_tpcds["coarse"][1]
    g2 = coarse_cov_tpcds >= 80.0
    print(f"Gate 2  coarse coverage on real TPC-DS >= 80%               : "
          f"{'PASS' if g2 else 'FAIL'}  ({coarse_cov_tpcds:.1f}%  vs refuse-mode {cov_tpcds['refuse'][1]:.1f}%)")

    coarse_hr = 100 * st["coarse"]["hits"] / st["coarse"]["cacheable"]
    g3 = coarse_hr > a_hr + 1.0
    print(f"Gate 3  coarse hit% >> invalidate-on-any-write              : "
          f"{'PASS' if g3 else 'FAIL'}  ({coarse_hr:.1f}% vs {a_hr:.1f}%)")

    for m in modes:
        for sql, d, served, live in fz["counterexamples"][m]:
            print(f"\n  !!! STALE [{m}] {sql}\n      fp={d}\n      cached={str(served)[:90]}\n      live={str(live)[:90]}")

    return dict(cov_tpcds=cov_tpcds, cov_jaffle=cov_jaffle, fz=fz,
                g1=g1, g2=g2, g3=g3, a_hr=a_hr)


if __name__ == "__main__":
    main()
