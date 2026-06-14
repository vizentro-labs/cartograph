"""
Cartograph demo: never-stale + precise invalidation, visible in the terminal.

Scripted, reproducible sequence against a real Postgres:
  1. expensive analytical query        -> source: live  (cold)
  2. same query again                  -> source: cache (no DB execution)
  3. OUT-OF-BAND write to a row in the query's footprint (separate connection)
  4. same query                        -> source: live  (change caught; NOT stale)
  5. OUT-OF-BAND write to an UNRELATED column
  6. same query                        -> source: cache (precise: unrelated write
                                          did not invalidate)

Run:  python examples/demo.py   (needs the wal_level=logical cluster up)
"""

import psycopg2
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from cartograph import Cartograph
from cartograph import config   # for the DSN of the demo cluster

Q = ("SELECT c.segment, COUNT(*) AS n, ROUND(COALESCE(SUM(o.amount),0),2) AS total "
     "FROM customers c JOIN orders o ON o.customer_id=c.id "
     "WHERE c.segment='ent' GROUP BY c.segment")


def oob(sql, args=None):
    """Fire a write from a SEPARATE connection the cache never sees."""
    c = psycopg2.connect(config.DSN); c.autocommit = True
    c.cursor().execute(sql, args)
    c.close()


def step(n, cg, note):
    r = cg.query(Q)
    tag = "CACHE ✓ (no DB hit)" if r.source == "cache" else "LIVE  (executed on DB)"
    print(f"\n[{n}] {note}")
    print(f"    source = {r.source:5}  -> {tag}")
    print(f"    rows   = {r.rows}   (footprint={r.footprint_mode}, as_of_lsn={r.as_of_lsn})")
    return r


def main():
    print("=" * 74)
    print("CARTOGRAPH DEMO — never serve a stale answer, invalidate precisely")
    print("=" * 74)
    print("query under test:\n   ", Q)

    Cartograph.bootstrap()
    cg = Cartograph()

    r1 = step(1, cg, "Agent asks the expensive query (cold).")
    r2 = step(2, cg, "Agent asks the SAME query again.")

    # find an 'ent' customer's order and bump its amount (in-footprint write)
    c = psycopg2.connect(config.DSN); cur = c.cursor()
    cur.execute("SELECT o.id, o.amount FROM orders o JOIN customers c ON c.id=o.customer_id "
                "WHERE c.segment='ent' ORDER BY o.id LIMIT 1")
    oid, old_amt = cur.fetchone(); c.close()
    print(f"\n[3] OUT-OF-BAND write (separate connection): "
          f"UPDATE orders.amount of order {oid} ({old_amt} -> {old_amt}+1000).")
    oob("UPDATE orders SET amount = amount + 1000 WHERE id=%s", (oid,))

    r4 = step(4, cg, "Agent asks the SAME query (Cartograph saw the WAL change).")

    print(f"\n    BEFORE (cached): {r2.rows[0]}")
    print(f"    AFTER  (live)  : {r4.rows[0]}")
    delta = r4.rows[0][2] - r2.rows[0][2]
    print(f"    -> total moved by {delta:+.2f}; cache returned the NEW value, "
          f"never the stale {r2.rows[0][2]}.")

    print(f"\n[5] OUT-OF-BAND write to an UNRELATED column: "
          f"UPDATE customers.email (not in the query's footprint).")
    oob("UPDATE customers SET email = 'changed@ex.com' WHERE segment='ent'")

    r6 = step(6, cg, "Agent asks the SAME query.")
    verdict = "STAYED CACHED (precise: unrelated write did not invalidate)" \
        if r6.source == "cache" else "re-executed (over-invalidated!)"
    print(f"    -> {verdict}")

    print("\n" + "-" * 74)
    print("stats:", cg.stats())
    print("-" * 74)
    seq = [r1.source, r2.source, r4.source, r6.source]
    print(f"source sequence: {seq}  (expected ['live','cache','live','cache'])")
    print("stale answers served:", cg.stats()["stale_count"])
    cg.close()


if __name__ == "__main__":
    main()
