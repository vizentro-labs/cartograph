#!/usr/bin/env python3
"""
Cartograph cache-invalidation spike.

Hypothesis under test:
  You can cache a SQL query result and CHEAPLY (no re-run, no LLM) detect when
  the underlying data changed enough that the cached result is now wrong, and
  invalidate precisely -- NEVER serving a stale answer while avoiding most
  re-executions.

This is throwaway scratch code. It is intentionally explicit, not a product.

We run ONE interleaved workload (queries + mutations) and, at every query step,
compute GROUND TRUTH by executing the query against the live DB. We then ask
each strategy what it WOULD have served, and score it:
  - a cache HIT is "stale" iff served_result != ground_truth at that moment.

Strategies compared on the identical workload:
  no-cache, ttl-{N}, on-any-write, footprint-table, footprint-column,
  footprint-precise.
"""

import sqlite3
import random
import hashlib
from collections import defaultdict

SEED = 1234
N_CUSTOMERS = 800
N_ORDERS = 4000
N_LINEITEMS = 9000
N_EVENTS = 4000
# We sweep several read/write mixes: the zero-stale invariant should hold in
# all of them, but the HIT-RATE payoff is workload-dependent and we want to be
# honest about that rather than cherry-pick one favorable mix.
MUTATION_FRACTIONS = [0.5, 0.3, 0.15]
TTLS = [5, 25, 100]             # measured in "event ticks" (logical time)

# Cost model: a re-execution is EXPENSIVE, a fingerprint check is CHEAP.
# Per-shape re-exec cost loosely models how much work the query scans.
EXEC_COST = {
    "point":     2,
    "range":     40,
    "aggregate": 100,
    "join":      60,
    "groupby":   90,
}
CHECK_COST_CHEAP = 1     # O(1)-ish version/epoch lookups
CHECK_COST_PRECISE_POINT = 2   # one indexed single-row fetch

random.seed(SEED)


# --------------------------------------------------------------------------
# DB setup
# --------------------------------------------------------------------------
def build_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE customers(
            id INTEGER PRIMARY KEY, name TEXT, email TEXT,
            city TEXT, segment TEXT
        );
        CREATE TABLE orders(
            id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL,
            status TEXT, order_day INTEGER
        );
        CREATE TABLE line_items(
            id INTEGER PRIMARY KEY, order_id INTEGER, sku TEXT, qty INTEGER
        );
        CREATE INDEX idx_orders_cust ON orders(customer_id);
        CREATE INDEX idx_li_order ON line_items(order_id);
        """
    )
    cities = ["NYC", "LA", "CHI", "HOU", "PHX", "SF"]
    segments = ["smb", "mid", "ent"]
    statuses = ["new", "paid", "shipped", "refunded"]

    c.executemany(
        "INSERT INTO customers VALUES(?,?,?,?,?)",
        [(i, f"cust{i}", f"cust{i}@ex.com",
          random.choice(cities), random.choice(segments))
         for i in range(1, N_CUSTOMERS + 1)],
    )
    c.executemany(
        "INSERT INTO orders VALUES(?,?,?,?,?)",
        [(i, random.randint(1, N_CUSTOMERS), round(random.uniform(5, 500), 2),
          random.choice(statuses), random.randint(1, 365))
         for i in range(1, N_ORDERS + 1)],
    )
    c.executemany(
        "INSERT INTO line_items VALUES(?,?,?,?)",
        [(i, random.randint(1, N_ORDERS), f"SKU{random.randint(1,200)}",
          random.randint(1, 9))
         for i in range(1, N_LINEITEMS + 1)],
    )
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Version tracker -- the cheap signal source.
# Every mutation bumps:
#   - a global write epoch          (for on-any-write)
#   - a per-table version           (for footprint-table)
#   - per-(table,column) versions   (for footprint-column)
#   - a per-table '*rowset' version  on insert/delete only
# All bumps are O(1). No scanning. This is the "cheap fingerprint" substrate.
# --------------------------------------------------------------------------
class Versions:
    def __init__(self):
        self.epoch = 0
        self.v = defaultdict(int)   # keyed by (table,'*table'), (table,'*rowset'), (table,col)

    def bump(self, table, cols=(), rowset=False):
        self.epoch += 1
        self.v[(table, "*table")] += 1
        if rowset:
            self.v[(table, "*rowset")] += 1
        for col in cols:
            self.v[(table, col)] += 1


# --------------------------------------------------------------------------
# Query shapes. Each declares its dependency FOOTPRINT explicitly.
# (In a real tool this comes from parsing/planning the SQL; here we declare it
#  by hand and note in VERDICT.md where that extraction gets hard.)
#
# Footprint columns use the sentinel '*rowset' to mean "result depends on which
# rows exist in this table" (i.e. inserts/deletes), distinct from value columns.
# --------------------------------------------------------------------------
POINT_IDS = list(range(1, 21))
RANGE_BANDS = [(0, 50), (50, 150), (150, 300), (300, 500), (100, 400), (10, 90)]
AGG_STATUS = ["new", "paid", "shipped"]
JOIN_IDS = list(range(1, 16))


def q_point(c, cid):
    return tuple(c.execute(
        "SELECT id,name,city,segment FROM customers WHERE id=? ORDER BY id",
        (cid,)).fetchall())


def q_range(c, band):
    lo, hi = band
    return tuple(c.execute(
        "SELECT id,customer_id,amount FROM orders "
        "WHERE amount BETWEEN ? AND ? ORDER BY id", (lo, hi)).fetchall())


def q_aggregate(c, status):
    return tuple(c.execute(
        "SELECT COUNT(*), ROUND(COALESCE(SUM(amount),0),2), "
        "ROUND(COALESCE(AVG(amount),0),2) FROM orders WHERE status=?",
        (status,)).fetchall())


def q_join(c, cid):
    return tuple(c.execute(
        "SELECT c.id,c.name,COUNT(o.id),ROUND(COALESCE(SUM(o.amount),0),2) "
        "FROM customers c JOIN orders o ON o.customer_id=c.id "
        "WHERE c.id=? GROUP BY c.id,c.name", (cid,)).fetchall())


def q_groupby(c, _):
    return tuple(c.execute(
        "SELECT customer_id, COUNT(*), ROUND(SUM(amount),2) FROM orders "
        "GROUP BY customer_id ORDER BY customer_id LIMIT 20").fetchall())


# shape -> (fn, param_pool, footprint_columns)
SHAPES = {
    "point": (
        q_point, POINT_IDS,
        {("customers", "*rowset"), ("customers", "id"),
         ("customers", "name"), ("customers", "city"), ("customers", "segment")},
    ),
    "range": (
        q_range, RANGE_BANDS,
        {("orders", "*rowset"), ("orders", "amount"), ("orders", "customer_id")},
    ),
    "aggregate": (
        q_aggregate, AGG_STATUS,
        {("orders", "*rowset"), ("orders", "amount"), ("orders", "status")},
    ),
    "join": (
        q_join, JOIN_IDS,
        {("customers", "*rowset"), ("customers", "id"), ("customers", "name"),
         ("orders", "*rowset"), ("orders", "customer_id"), ("orders", "amount")},
    ),
    "groupby": (
        q_groupby, [None],
        {("orders", "*rowset"), ("orders", "customer_id"), ("orders", "amount")},
    ),
}

SHAPE_TABLES = {
    "point": {"customers"},
    "range": {"orders"},
    "aggregate": {"orders"},
    "join": {"customers", "orders"},
    "groupby": {"orders"},
}


def result_hash(result):
    return hashlib.blake2b(repr(result).encode(), digest_size=16).hexdigest()


# --------------------------------------------------------------------------
# Mutations. Each performs a real DB write AND bumps the matching versions.
# Weighted so that MANY mutations are irrelevant to the expensive order
# aggregates (line_items churn, customer email edits) -- the regime where a
# precise footprint should beat blunt invalidation.
# --------------------------------------------------------------------------
class Mutator:
    def __init__(self, conn, ver):
        self.conn = conn
        self.ver = ver
        self.next_order = N_ORDERS + 1
        self.next_li = N_LINEITEMS + 1
        self.next_cust = N_CUSTOMERS + 1

    def run(self):
        kind = random.choices(
            ["li_qty", "li_insert", "cust_email",      # irrelevant to order queries
             "order_amount", "order_status",           # column-level relevant
             "order_insert", "order_delete",           # rowset relevant
             "cust_name", "cust_city"],                 # relevant to point/join
            weights=[14, 10, 12, 12, 8, 8, 6, 5, 5],
        )[0]
        c = self.conn.cursor()
        if kind == "li_qty":
            c.execute("UPDATE line_items SET qty=qty+1 WHERE id=?",
                      (random.randint(1, self.next_li - 1),))
            self.ver.bump("line_items", cols=["qty"])
        elif kind == "li_insert":
            c.execute("INSERT INTO line_items VALUES(?,?,?,?)",
                      (self.next_li, random.randint(1, self.next_order - 1),
                       f"SKU{random.randint(1,200)}", random.randint(1, 9)))
            self.next_li += 1
            self.ver.bump("line_items", cols=["order_id", "sku", "qty"], rowset=True)
        elif kind == "cust_email":
            c.execute("UPDATE customers SET email=? WHERE id=?",
                      (f"x{random.randint(0,10**6)}@ex.com",
                       random.randint(1, self.next_cust - 1)))
            self.ver.bump("customers", cols=["email"])
        elif kind == "order_amount":
            c.execute("UPDATE orders SET amount=? WHERE id=?",
                      (round(random.uniform(5, 500), 2),
                       random.randint(1, self.next_order - 1)))
            self.ver.bump("orders", cols=["amount"])
        elif kind == "order_status":
            c.execute("UPDATE orders SET status=? WHERE id=?",
                      (random.choice(["new", "paid", "shipped", "refunded"]),
                       random.randint(1, self.next_order - 1)))
            self.ver.bump("orders", cols=["status"])
        elif kind == "order_insert":
            c.execute("INSERT INTO orders VALUES(?,?,?,?,?)",
                      (self.next_order, random.randint(1, self.next_cust - 1),
                       round(random.uniform(5, 500), 2),
                       random.choice(["new", "paid", "shipped", "refunded"]),
                       random.randint(1, 365)))
            self.next_order += 1
            self.ver.bump("orders",
                          cols=["customer_id", "amount", "status", "order_day"],
                          rowset=True)
        elif kind == "order_delete":
            oid = random.randint(1, self.next_order - 1)
            c.execute("DELETE FROM orders WHERE id=?", (oid,))
            if c.rowcount:
                self.ver.bump("orders", rowset=True)
        elif kind == "cust_name":
            c.execute("UPDATE customers SET name=? WHERE id=?",
                      (f"renamed{random.randint(0,10**6)}",
                       random.randint(1, self.next_cust - 1)))
            self.ver.bump("customers", cols=["name"])
        elif kind == "cust_city":
            c.execute("UPDATE customers SET city=? WHERE id=?",
                      (random.choice(["NYC", "LA", "CHI", "HOU", "PHX", "SF"]),
                       random.randint(1, self.next_cust - 1)))
            self.ver.bump("customers", cols=["city"])
        self.conn.commit()


# --------------------------------------------------------------------------
# Strategies. Each exposes lookup(key, shape, params, now) -> (served, is_hit,
# check_cost). On a miss the harness re-executes and calls store(...).
# Served result on a hit is whatever the cache holds; the harness scores it
# against ground truth.
# --------------------------------------------------------------------------
class NoCache:
    name = "no-cache"
    def lookup(self, key, shape, params, now): return (None, False, 0)
    def store(self, key, shape, result, now): pass


class TTLCache:
    def __init__(self, ttl):
        self.ttl = ttl
        self.name = f"ttl-{ttl}"
        self.store_ = {}
    def lookup(self, key, shape, params, now):
        e = self.store_.get(key)
        if e and (now - e[1]) <= self.ttl:
            return (e[0], True, CHECK_COST_CHEAP)
        return (None, False, CHECK_COST_CHEAP if e else 0)
    def store(self, key, shape, result, now):
        self.store_[key] = (result, now)


class AnyWriteCache:
    name = "on-any-write"
    def __init__(self, ver):
        self.ver = ver
        self.store_ = {}
    def lookup(self, key, shape, params, now):
        e = self.store_.get(key)
        if e and e[1] == self.ver.epoch:
            return (e[0], True, CHECK_COST_CHEAP)
        return (None, False, CHECK_COST_CHEAP if e else 0)
    def store(self, key, shape, result, now):
        self.store_[key] = (result, self.ver.epoch)


class FootprintTable:
    name = "footprint-table"
    def __init__(self, ver):
        self.ver = ver
        self.store_ = {}
    def _fp(self, shape):
        return tuple(sorted((t, self.ver.v[(t, "*table")])
                            for t in SHAPE_TABLES[shape]))
    def lookup(self, key, shape, params, now):
        e = self.store_.get(key)
        if e and e[1] == self._fp(shape):
            return (e[0], True, CHECK_COST_CHEAP)
        return (None, False, CHECK_COST_CHEAP if e else 0)
    def store(self, key, shape, result, now):
        self.store_[key] = (result, self._fp(shape))


class FootprintColumn:
    name = "footprint-column"
    def __init__(self, ver):
        self.ver = ver
        self.store_ = {}
    def _fp(self, shape):
        cols = SHAPES[shape][2]
        return tuple(sorted((k, self.ver.v[k]) for k in cols))
    def lookup(self, key, shape, params, now):
        e = self.store_.get(key)
        if e and e[1] == self._fp(shape):
            return (e[0], True, CHECK_COST_CHEAP)
        return (None, False, CHECK_COST_CHEAP if e else 0)
    def store(self, key, shape, result, now):
        self.store_[key] = (result, self._fp(shape))


class FootprintPrecise:
    """Column-level for everything, but for the point lookup it fingerprints the
    ACTUAL matching row cheaply (indexed single-row fetch). Demonstrates that
    value-level precision is reachable only when the predicate is selective."""
    name = "footprint-precise"
    def __init__(self, ver, conn):
        self.ver = ver
        self.conn = conn
        self.store_ = {}
    def _fp(self, shape, params):
        if shape == "point":
            row = self.conn.execute(
                "SELECT id,name,city,segment FROM customers WHERE id=?",
                (params,)).fetchall()
            return ("precise-point", result_hash(tuple(row)))
        cols = SHAPES[shape][2]
        return tuple(sorted((k, self.ver.v[k]) for k in cols))
    def _cost(self, shape):
        return CHECK_COST_PRECISE_POINT if shape == "point" else CHECK_COST_CHEAP
    def lookup(self, key, shape, params, now):
        e = self.store_.get(key)
        cost = self._cost(shape)
        if e and e[1] == self._fp(shape, params):
            return (e[0], True, cost)
        return (None, False, cost if e else 0)
    def store_p(self, key, shape, params, result, now):
        self.store_[key] = (result, self._fp(shape, params))


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
def run_scenario(mutation_fraction):
    # Reseed so every scenario starts from an identical DB; only the
    # query/mutation mix differs.
    random.seed(SEED)
    conn = build_db()
    ver = Versions()
    mut = Mutator(conn, ver)

    strategies = [NoCache(), AnyWriteCache(ver),
                  FootprintTable(ver), FootprintColumn(ver),
                  FootprintPrecise(ver, conn)]
    strategies[1:1] = [TTLCache(t) for t in TTLS]

    stats = {s.name: dict(queries=0, hits=0, stale=0, cost=0) for s in strategies}

    shape_names = list(SHAPES.keys())
    now = 0
    n_queries = 0
    n_mutations = 0

    for _ in range(N_EVENTS):
        now += 1
        if random.random() < mutation_fraction:
            mut.run()
            n_mutations += 1
            continue

        n_queries += 1
        shape = random.choice(shape_names)
        fn, pool, _ = SHAPES[shape]
        params = random.choice(pool)
        key = (shape, params)

        truth = fn(conn, params)          # GROUND TRUTH at this instant
        exec_cost = EXEC_COST[shape]

        for s in strategies:
            st = stats[s.name]
            st["queries"] += 1
            served, is_hit, check_cost = s.lookup(key, shape, params, now)
            st["cost"] += check_cost
            if is_hit:
                st["hits"] += 1
                if served != truth:
                    st["stale"] += 1
                # NOTE: we do NOT correct the cache on a stale hit -- a real
                # cache would have shipped the wrong answer. That's the point.
            else:
                st["cost"] += exec_cost     # re-execution
                if isinstance(s, FootprintPrecise):
                    s.store_p(key, shape, params, truth, now)
                else:
                    s.store(key, shape, truth, now)

    # ---- report ----
    print(f"\n{'='*72}")
    print(f"SCENARIO: mutation_fraction={mutation_fraction}  "
          f"({'write-heavy' if mutation_fraction>=0.5 else 'read-heavy' if mutation_fraction<=0.2 else 'mixed'})")
    print(f"{'='*72}")
    print(f"Workload: {N_EVENTS} events  ->  {n_queries} queries, "
          f"{n_mutations} mutations")
    print(f"DB seed rows: {N_CUSTOMERS} customers, {N_ORDERS} orders, "
          f"{N_LINEITEMS} line_items   (seed={SEED})\n")

    hdr = f"{'strategy':<18}{'hit%':>8}{'stale-hit%':>12}{'avg cost/q':>12}{'re-execs':>10}"
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for s in strategies:
        st = stats[s.name]
        q = st["queries"]
        hit_pct = 100 * st["hits"] / q
        stale_pct = (100 * st["stale"] / st["hits"]) if st["hits"] else 0.0
        avg_cost = st["cost"] / q
        reexec = q - st["hits"]
        rows.append((s.name, hit_pct, stale_pct, avg_cost, reexec))
        print(f"{s.name:<18}{hit_pct:>7.1f}%{stale_pct:>11.2f}%"
              f"{avg_cost:>12.2f}{reexec:>10}")

    # ---- gate evaluation (ours = footprint-column, headline cheap signal) ----
    by = {r[0]: r for r in rows}
    ours = by["footprint-column"]
    anyw = by["on-any-write"]
    print("\n=== SUCCESS GATES ===")
    g1 = ours[2] <= 0.0001
    print(f"Gate 1  footprint-column stale-hit ~0%        : "
          f"{'PASS' if g1 else 'FAIL'}  ({ours[2]:.3f}%)")
    g2 = g1 and ours[1] > anyw[1] + 1.0
    print(f"Gate 2  footprint hit% >> on-any-write hit%   : "
          f"{'PASS' if g2 else 'FAIL'}  "
          f"({ours[1]:.1f}% vs {anyw[1]:.1f}%)")
    # Gate 3: no TTL dominates ours -- each TTL either has stale hits OR a
    # lower hit rate (at equal-or-worse correctness it cannot beat us).
    ttl_rows = [by[f"ttl-{t}"] for t in TTLS]
    ttl_verdicts = []
    g3 = True
    for t, tr in zip(TTLS, ttl_rows):
        dominates = (tr[2] <= ours[2] + 1e-9) and (tr[1] > ours[1] + 1e-9)
        if dominates:
            g3 = False
        ttl_verdicts.append((t, tr[2], tr[1], dominates))
    print(f"Gate 3  no TTL dominates footprint            : "
          f"{'PASS' if g3 else 'FAIL'}")
    for t, stale, hit, dom in ttl_verdicts:
        tag = "DOMINATES" if dom else ("stale>0" if stale > 0.0001 else "lower hit%")
        print(f"          ttl-{t:<4} stale={stale:5.2f}%  hit={hit:5.1f}%  -> {tag}")

    print()
    return dict(mf=mutation_fraction, rows=rows, g1=g1, g2=g2, g3=g3,
                ttl=ttl_verdicts, nq=n_queries, nm=n_mutations,
                ours=ours, anyw=anyw)


def main():
    results = [run_scenario(mf) for mf in MUTATION_FRACTIONS]

    print("\n" + "#" * 72)
    print("# CROSS-SCENARIO SUMMARY  (footprint-column = 'ours')")
    print("#" * 72)
    print(f"{'mut.frac':>9}{'ours hit%':>11}{'ours stale%':>13}"
          f"{'anywrite hit%':>15}{'best-ttl-no-stale hit%':>24}")
    for r in results:
        ours = r["ours"]
        # best TTL that had ~0 stale hits (apples-to-apples on correctness)
        clean_ttls = [t for t in r["ttl"] if t[1] <= 0.0001]
        best_clean = max((t[2] for t in clean_ttls), default=0.0)
        print(f"{r['mf']:>9}{ours[1]:>10.1f}%{ours[2]:>12.3f}%"
              f"{r['anyw'][1]:>14.1f}%{best_clean:>23.1f}%")

    all_g1 = all(r["g1"] for r in results)
    all_g2 = all(r["g2"] for r in results)
    all_g3 = all(r["g3"] for r in results)
    print(f"\nGate 1 (zero stale)         : {'PASS' if all_g1 else 'FAIL'} "
          f"in all {len(results)} scenarios")
    print(f"Gate 2 (beats on-any-write) : {'PASS' if all_g2 else 'FAIL'} "
          f"in all {len(results)} scenarios")
    print(f"Gate 3 (no TTL dominates)   : {'PASS' if all_g3 else 'FAIL'} "
          f"in all {len(results)} scenarios")
    return results


if __name__ == "__main__":
    main()
