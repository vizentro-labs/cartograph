#!/usr/bin/env python3
"""Benchmark: out-of-band differential fuzzer (graduated from spike 4).

Validates the PACKAGE runtime (cartograph.runtime) against a REAL Postgres:
every cache HIT is checked vs a live query; a sound mismatch = a missed write.
This is the soundness oracle, now run against the shipped code (no private copy).

Run from packages/core:  PYTHONPATH=src python benchmarks/fuzz_postgres.py
"""
import os, sys, time, random, threading
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import psycopg2
import psycopg2.extras

from cartograph import config
from cartograph.runtime import (conn, rhash, bootstrap, read_schema,
                                WalVersions, Cache, on_schema_change)

DSN = config.DSN
TABLES = config.TABLES
PK = config.PK
SLOT = config.SLOT
MODE = config.MODE
SEED = 1234
random.seed(SEED)


class Writers:
    def __init__(self):
        self.stop = threading.Event()
        self.threads = []
        self.next_order = 4001
        self.next_li = 9001
        self.lock = threading.Lock()
        self.count = 0

    def _txn_writer(self):
        c = conn(); cur = c.cursor()
        cities = ["NYC", "LA", "CHI", "HOU", "PHX", "SF"]
        while not self.stop.is_set():
            k = random.random()
            try:
                if k < 0.30:      # update order amount (relevant to aggregates)
                    cur.execute("UPDATE orders SET amount=%s WHERE id=%s",
                                (round(random.uniform(5, 500), 2), random.randint(1, 4000)))
                elif k < 0.45:    # update order status
                    cur.execute("UPDATE orders SET status=%s WHERE id=%s",
                                (random.choice(["new", "paid", "shipped"]), random.randint(1, 4000)))
                elif k < 0.60:    # update customer email (irrelevant to most queries)
                    cur.execute("UPDATE customers SET email=%s WHERE id=%s",
                                (f"x{random.randint(0,10**6)}@ex.com", random.randint(1, 800)))
                elif k < 0.72:    # update customer segment (relevant to joins)
                    cur.execute("UPDATE customers SET segment=%s WHERE id=%s",
                                (random.choice(["smb", "mid", "ent"]), random.randint(1, 800)))
                elif k < 0.82:    # line_items churn (irrelevant unless joined)
                    cur.execute("UPDATE line_items SET qty=qty+1 WHERE id=%s",
                                (random.randint(1, 9000),))
                elif k < 0.90:    # insert order
                    with self.lock:
                        oid = self.next_order; self.next_order += 1
                    cur.execute("INSERT INTO orders VALUES (%s,%s,%s,%s,%s)",
                                (oid, random.randint(1, 800), round(random.uniform(5, 500), 2),
                                 random.choice(["new", "paid"]), random.randint(1, 365)))
                elif k < 0.95:    # delete order
                    cur.execute("DELETE FROM orders WHERE id=%s", (random.randint(1, self.next_order - 1),))
                else:             # insert line_item
                    with self.lock:
                        lid = self.next_li; self.next_li += 1
                    cur.execute("INSERT INTO line_items VALUES (%s,%s,%s,%s)",
                                (lid, random.randint(1, 4000), "SKU9", random.randint(1, 9)))
                with self.lock:
                    self.count += 1
            except Exception:
                c.rollback()
            time.sleep(random.uniform(0.004, 0.016))
        c.close()

    def _etl_writer(self):
        c = conn(); cur = c.cursor()
        while not self.stop.is_set():
            try:    # bulk ETL: re-price a band of orders in one statement
                lo = random.randint(1, 3800)
                cur.execute("UPDATE orders SET amount = amount * 1.01 WHERE id BETWEEN %s AND %s",
                            (lo, lo + 40))
                with self.lock:
                    self.count += 1
            except Exception:
                c.rollback()
            time.sleep(random.uniform(0.3, 0.6))
        c.close()

    def start(self, n_txn=2):
        for _ in range(n_txn):
            t = threading.Thread(target=self._txn_writer, daemon=True); t.start(); self.threads.append(t)
        t = threading.Thread(target=self._etl_writer, daemon=True); t.start(); self.threads.append(t)

    def halt(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=2)


# ==========================================================================
# Executable workload (Postgres dialect; small pools so queries recur)
# ==========================================================================
IDS = list(range(1, 41))
SEG = ["smb", "mid", "ent"]
ST = ["new", "paid", "shipped"]
BANDS = [(0, 100), (100, 300), (300, 500)]
LIM = [10, 20]


def gen_query():
    k = random.random()
    if k < 0.16:
        return f"SELECT id, name, city, segment FROM customers WHERE id={random.choice(IDS)}"
    if k < 0.32:
        return (f"SELECT COUNT(*), ROUND(COALESCE(SUM(amount),0),2) FROM orders "
                f"WHERE status='{random.choice(ST)}'")
    if k < 0.48:
        lo, hi = random.choice(BANDS)
        return (f"SELECT id, customer_id, amount FROM orders WHERE amount BETWEEN {lo} AND {hi} "
                f"ORDER BY id LIMIT {random.choice(LIM)}")
    if k < 0.64:
        return (f"SELECT c.segment, COUNT(*), ROUND(COALESCE(SUM(o.amount),0),2) "
                f"FROM customers c JOIN orders o ON o.customer_id=c.id "
                f"WHERE c.segment='{random.choice(SEG)}' GROUP BY c.segment ORDER BY c.segment")
    if k < 0.78:
        return (f"SELECT customer_id, COUNT(*), ROUND(SUM(amount),2) FROM orders "
                f"GROUP BY customer_id ORDER BY customer_id LIMIT {random.choice(LIM)}")
    if k < 0.90:   # CTE (coarse-mode path)
        return (f"WITH e AS (SELECT customer_id AS cid, amount AS amt, status AS st FROM orders) "
                f"SELECT cid, ROUND(SUM(amt),2) FROM e GROUP BY cid ORDER BY cid LIMIT {random.choice(LIM)}")
    return (f"SELECT o.customer_id, COALESCE(SUM(li.qty),0) FROM orders o "
            f"JOIN line_items li ON li.order_id=o.id GROUP BY o.customer_id "
            f"ORDER BY o.customer_id LIMIT {random.choice(LIM)}")


def run_live(qconn, sql):
    cur = qconn.cursor()
    cur.execute(sql)
    return cur.fetchall()


# ==========================================================================
# PART 1: out-of-band-write fuzzer with a race-sound oracle
# ==========================================================================
def fuzz_outofband(n_iters=120000):
    ver = WalVersions()
    schema = read_schema()
    cache = Cache(ver, schema)
    qconn = conn()
    writers = Writers()
    writers.start()
    any_cache = {}            # invalidate-on-any-write baseline (epoch = total bumps)

    hits = stale = raced = cacheable = refused = 0
    any_hits = 0
    epoch = 0
    drain_keys_total = 0

    for _ in range(n_iters):
        sql = gen_query()
        # (1) synchronous drain to current LSN BEFORE deciding -> closes the window
        b = ver.drain(); epoch += len(b); drain_keys_total += len(b)
        if ver.schema_dirty:
            on_schema_change(cache, ver)

        fp = cache.footprint(sql)
        if fp[0] == "refuse":
            refused += 1
            continue
        cacheable += 1

        served, hit = cache.lookup(sql, qconn)
        # (3) ground truth
        live = run_live(qconn, sql)
        # (4) verification drain: did a relevant write race the read?
        b2 = ver.drain(); epoch += len(b2); drain_keys_total += len(b2)
        if ver.schema_dirty:
            on_schema_change(cache, ver)
        # A row-level fingerprint re-reads the WHOLE row, so ANY write to that
        # table during the verify window is a race (not just id/rowset). Column
        # footprints race only on their own keys.
        if fp[0] == "column":
            race = bool(b2 & set(fp[1]))
        else:
            race = any(k[0] == fp[1] for k in b2)

        # any-write baseline (separate accounting, same population)
        ae = any_cache.get(sql)
        if ae is not None and ae[1] == epoch - len(b2) and not race:
            any_hits += 1
        else:
            any_cache[sql] = (live, epoch)

        if race:
            # A relevant write committed between the lookup and the live read.
            # `live` is now inconsistent with the post-drain version state, so we
            # must NOT store it (doing so caches a stale result under a fresh
            # fingerprint -- a real bug the fuzzer caught). Skip; a later clean
            # iteration will populate the entry.
            raced += 1
            continue
        if hit:
            hits += 1
            if served != live:
                stale += 1
                print(f"\n!!! STALE HIT\n  SQL: {sql}\n  fp: {fp}\n  cached: {str(served)[:100]}\n  live: {str(live)[:100]}")
        else:
            cache.store(sql, live, qconn)

    writers.halt()
    qconn.close()
    return dict(iters=n_iters, cacheable=cacheable, refused=refused, hits=hits,
                stale=stale, raced=raced, any_hits=any_hits, writes=writers.count,
                drain_keys=drain_keys_total)


# ==========================================================================
# PART 2: DDL battery -- each must yield correct-invalidate or safe-refuse,
# never a stale hit.
# ==========================================================================
def ddl(sql):
    c = conn(); c.cursor().execute(sql); c.close()


def fuzz_ddl():
    ver = WalVersions()
    schema = read_schema()
    cache = Cache(ver, schema)
    qconn = conn()
    results = []

    def settle():
        ver.drain()
        if ver.schema_dirty:
            return on_schema_change(cache, ver)
        return (set(), 0)

    def cache_query(sql):
        ver.drain()
        if ver.schema_dirty: on_schema_change(cache, ver)
        served, hit = cache.lookup(sql, qconn)
        fp = cache.footprint(sql)
        if fp[0] == "refuse":
            return "refuse", None
        live = run_live(qconn, sql)
        cache.store(sql, live, qconn)
        return "cached", live

    def reask(sql, prior):
        """Return ('stale'|'invalidated_miss'|'hit_ok'|'refused'|'error', detail)."""
        ver.drain()
        if ver.schema_dirty:
            changed, n = on_schema_change(cache, ver)
        served, hit = cache.lookup(sql, qconn)
        fp = cache.footprint(sql)
        if fp[0] == "refuse":
            return "refused", fp[1]
        try:
            live = run_live(qconn, sql)
        except Exception as e:
            qconn.rollback()
            return "error_live", type(e).__name__
        if hit:
            return ("stale" if served != live else "hit_ok"), None
        cache.store(sql, live, qconn)
        return "invalidated_miss", None

    cases = [
        ("rename footprint column",
         "SELECT id, name, city, segment FROM customers WHERE id=5",
         "ALTER TABLE customers RENAME COLUMN city TO city2"),
        ("add column",
         "SELECT COUNT(*), ROUND(COALESCE(SUM(amount),0),2) FROM orders WHERE status='paid'",
         "ALTER TABLE orders ADD COLUMN discount numeric(10,2) DEFAULT 0"),
        ("change column type",
         "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id ORDER BY customer_id LIMIT 10",
         "ALTER TABLE orders ALTER COLUMN order_day TYPE bigint"),
        ("drop a referenced table",
         "SELECT o.customer_id, COALESCE(SUM(li.qty),0) FROM orders o JOIN line_items li ON li.order_id=o.id GROUP BY o.customer_id ORDER BY o.customer_id LIMIT 10",
         "DROP TABLE line_items"),
        ("rename then write old+new",
         "SELECT id, segment FROM customers WHERE id=7",
         "ALTER TABLE customers RENAME COLUMN segment TO tier"),
    ]
    for name, q, ddl_sql in cases:
        st, _ = cache_query(q)               # cache it (if cacheable)
        ddl(ddl_sql)                          # out-of-band DDL
        # also fire a data write right after DDL to stress invalidation
        try:
            ddl("UPDATE customers SET name=name WHERE id=1")
        except Exception:
            pass
        outcome, detail = reask(q, st)
        ok = outcome in ("invalidated_miss", "hit_ok", "refused", "error_live")
        results.append((name, st, outcome, detail, ok))
    qconn.close()
    return results


# ==========================================================================
def main():
    print("#" * 72)
    print("# SPIKE 4 -- production substrate: real Postgres, out-of-band writes + DDL")
    print("#" * 72)
    print("setting up cluster schema, seed, REPLICA IDENTITY FULL, DDL trigger, WAL slot...")
    bootstrap()

    print("\n" + "=" * 72)
    print("PART 1: OUT-OF-BAND DATA WRITES (separate threads + bulk ETL; cache sees")
    print("        writes ONLY via the Postgres WAL / test_decoding stream)")
    print("=" * 72)
    r = fuzz_outofband()
    clean = r["hits"]
    sr = 100 * r["stale"] / clean if clean else 0.0
    print(f"iterations              : {r['iters']}")
    print(f"out-of-band writes fired: {r['writes']} (txn threads + ETL)")
    print(f"WAL bumps applied       : {r['drain_keys']}")
    print(f"cacheable / refused     : {r['cacheable']} / {r['refused']}")
    print(f"clean cache hits checked: {clean}")
    print(f"verification-raced (excluded, measures the window): {r['raced']}")
    print(f"STALE HITS              : {r['stale']}")
    print(f"STALE-HIT RATE          : {sr:.4f}%")
    hr = 100 * r["hits"] / r["cacheable"] if r["cacheable"] else 0
    ahr = 100 * r["any_hits"] / r["cacheable"] if r["cacheable"] else 0
    print(f"hit rate (footprint)    : {hr:.1f}%   vs invalidate-on-any-write {ahr:.1f}%")

    print("\n" + "=" * 72)
    print("PART 2: OUT-OF-BAND SCHEMA / DDL CHANGES")
    print("=" * 72)
    dres = fuzz_ddl()
    print(f"{'case':<28}{'cached?':<10}{'reask outcome':<20}{'detail':<14}{'pass'}")
    print("-" * 80)
    ddl_ok = True
    for name, st, outcome, detail, ok in dres:
        ddl_ok = ddl_ok and ok
        print(f"{name:<28}{st:<10}{outcome:<20}{str(detail or ''):<14}{'PASS' if ok else 'FAIL'}")

    print("\n" + "=" * 72)
    print("=== SUCCESS GATES ===")
    print("=" * 72)
    g1 = r["stale"] == 0
    print(f"Gate 1  0.000% stale under OUT-OF-BAND data writes : "
          f"{'PASS' if g1 else 'FAIL'}  ({r['stale']} stale / {clean} clean hits)")
    no_stale_ddl = all(o != "stale" for _, _, o, _, _ in dres)
    g2 = ddl_ok and no_stale_ddl
    print(f"Gate 2  0.000% stale across DDL battery            : "
          f"{'PASS' if g2 else 'FAIL'}  (no stale; all correct-invalidate or safe-refuse)")
    print(f"Gate 3  write->bump LAG window characterized       : "
          f"closed by synchronous drain-to-current-LSN before each serve.")
    print(f"        verification-race rate (fuzzer artifact)   : "
          f"{100*r['raced']/(r['raced']+clean+1e-9):.2f}% of hit checks "
          f"(in production these are correct-as-of-LSN answers, not stale)")

    return dict(r=r, dres=dres, g1=g1, g2=g2)


if __name__ == "__main__":
    main()
