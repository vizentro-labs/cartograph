"""
Cartograph zero-config quickstart — the never-stale guarantee, proven live.

This is the onboarding/demo LAYER. It does not implement any caching logic; it
drives the proven engine (`cartograph.Cartograph`) through a scripted, dramatic
sequence against the pre-seeded sample Postgres that Docker Compose spun up:

  1. ask an expensive analytical query        -> LIVE  (cold, executed on the DB)
  2. ask the SAME query again                  -> CACHE (served, no DB execution)
  3. change a row IN the query's footprint     (out-of-band write, separate conn)
  4. ask the SAME query again                  -> LIVE  (change caught; NOT stale)
  5. change an UNRELATED column                (out-of-band write)
  6. ask the SAME query again                  -> CACHE (precise: no over-invalidation)

The point: the cache *cannot lie to you*. The instant the data it depends on
moves, it refuses to serve the old answer — and it proves it in your terminal.

Run inside the container; it is self-contained (waits for the DB, bootstraps the
sample schema + replication slot, then runs). Honors NO_COLOR.
"""

import os
import sys
import time

import psycopg2

sys.path.insert(0, "/app/core/src")  # the installed package is also importable; this is belt-and-suspenders
from cartograph import Cartograph, config


# ----------------------------------------------------------------------------
# terminal theater — bold but instant-readable
# ----------------------------------------------------------------------------
_NO_COLOR = bool(os.environ.get("NO_COLOR"))


def _c(code):
    return "" if _NO_COLOR else code


RESET = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
GREEN = _c("\033[32m")
RED = _c("\033[31m")
AMBER = _c("\033[33m")
CYAN = _c("\033[36m")
MAGENTA = _c("\033[35m")
GREY = _c("\033[90m")

# pacing: a guided reveal, not a slideshow. Override with CARTOGRAPH_DEMO_PACE.
PACE = float(os.environ.get("CARTOGRAPH_DEMO_PACE", "0.6"))


def pause(mult=1.0):
    if PACE > 0:
        time.sleep(PACE * mult)


def line(s=""):
    print(s, flush=True)


def rule(char="─", color=GREY):
    line(f"{color}{char * 74}{RESET}")


def banner(title, subtitle=None):
    rule("═", CYAN)
    line(f"{BOLD}{CYAN}  {title}{RESET}")
    if subtitle:
        line(f"{DIM}  {subtitle}{RESET}")
    rule("═", CYAN)


def step_header(n, total, note):
    line()
    line(f"{BOLD}{MAGENTA}  STEP {n}/{total}{RESET}  {BOLD}{note}{RESET}")


# ----------------------------------------------------------------------------
# the query under test + the engine harness
# ----------------------------------------------------------------------------
Q = ("SELECT c.segment, COUNT(*) AS n, ROUND(COALESCE(SUM(o.amount),0),2) AS total "
     "FROM customers c JOIN orders o ON o.customer_id=c.id "
     "WHERE c.segment='ent' GROUP BY c.segment")


def wait_for_db(dsn, attempts=60):
    """Block until the sample Postgres accepts connections (compose healthcheck
    usually beats us here; this is the safety net)."""
    last = None
    for i in range(attempts):
        try:
            psycopg2.connect(dsn).close()
            return
        except Exception as e:  # noqa: BLE001 — any connection error means "not ready yet"
            last = e
            if i == 0:
                line(f"{DIM}  waiting for the sample database…{RESET}")
            time.sleep(1)
    raise SystemExit(f"database never came up: {last}")


def oob(sql, args=None):
    """Fire a write from a SEPARATE connection the cache never observes — exactly
    the way a real app, a teammate, or a cron job would change the data."""
    c = psycopg2.connect(config.DSN)
    c.autocommit = True
    c.cursor().execute(sql, args)
    c.close()


def ask(cg, n, total, note, expect):
    """Issue the query through Cartograph and narrate the result dramatically."""
    step_header(n, total, note)
    pause(0.4)
    r = cg.query(Q)
    seg, count, total_amt = r.rows[0]

    if r.source == "cache":
        tag = f"{BOLD}{GREEN}● CACHE HIT{RESET}  {GREEN}served from memory — the DB was NOT touched{RESET}"
    else:
        tag = f"{BOLD}{AMBER}▲ LIVE{RESET}      {AMBER}executed against Postgres right now{RESET}"

    surprise = "" if r.source == expect else f"   {RED}(unexpected!){RESET}"
    line(f"    {tag}{surprise}")
    line(f"    {DIM}footprint={r.footprint_mode}   as_of_lsn={r.as_of_lsn}{RESET}")
    line(f"    result → segment={CYAN}{seg}{RESET}  orders={CYAN}{count}{RESET}  "
         f"total=${BOLD}{CYAN}{total_amt:,.2f}{RESET}")
    pause()
    return r


def main():
    dsn = os.environ.get("CARTOGRAPH_DSN", config.DSN)
    config.DSN = dsn

    line()
    banner("CARTOGRAPH — the never-stale cache for AI agents on Postgres",
           "It can't serve you a stale answer. Watch it refuse to.")
    line()
    line(f"{DIM}  Sample database: {dsn}{RESET}")
    line(f"{DIM}  Query under test (an expensive JOIN + GROUP BY a query agent would cache):{RESET}")
    line(f"    {CYAN}{Q}{RESET}")
    pause(1.5)

    # ---- self-contained setup: wait, then seed the sample + slot + DDL trigger
    wait_for_db(dsn)
    line()
    line(f"{DIM}  Seeding the sample (customers/orders/line_items), REPLICA IDENTITY FULL,{RESET}")
    line(f"{DIM}  logical replication slot, DDL trigger… {RESET}")
    Cartograph.bootstrap()
    line(f"{GREEN}  ✓ sample ready.{RESET}")
    cg = Cartograph()
    pause(1.0)

    TOTAL = 6
    r1 = ask(cg, 1, TOTAL, "An agent asks the expensive query for the first time.",
             expect="live")
    line(f"    {DIM}→ Nothing cached yet, so Cartograph runs it live and remembers it.{RESET}")
    pause()

    r2 = ask(cg, 2, TOTAL, "The agent asks the EXACT same query again.",
             expect="cache")
    line(f"    {DIM}→ Same question, unchanged data → instant cache hit. No DB work, no LLM round-trip.{RESET}")
    pause(1.2)

    # ---- the moment: change the data underneath, out of band ----
    c = psycopg2.connect(config.DSN)
    cur = c.cursor()
    cur.execute("SELECT o.id, o.amount FROM orders o JOIN customers c ON c.id=o.customer_id "
                "WHERE c.segment='ent' ORDER BY o.id LIMIT 1")
    oid, old_amt = cur.fetchone()
    c.close()

    step_header(3, TOTAL, "Meanwhile, someone changes the data — out of band.")
    rule("·", AMBER)
    line(f"{BOLD}{AMBER}  ⚡ A separate connection the cache never sees writes to the orders table.{RESET}")
    line(f"     {AMBER}UPDATE orders SET amount = amount + 1000  WHERE id={oid}   "
         f"({old_amt} → {float(old_amt) + 1000}){RESET}")
    rule("·", AMBER)
    oob("UPDATE orders SET amount = amount + 1000 WHERE id=%s", (oid,))
    pause(1.4)

    r4 = ask(cg, 4, TOTAL, "The agent asks the same query — would a normal cache lie here?",
             expect="live")
    line(f"    {DIM}→ Cartograph saw the WAL change touch this query's footprint. It REFUSED the "
         f"cached value and recomputed.{RESET}")
    pause(1.0)

    # ---- the undeniable before/after ----
    before = r2.rows[0][2]
    after = r4.rows[0][2]
    delta = after - before
    line()
    rule("━", BOLD + CYAN)
    line(f"{BOLD}  THE NEVER-STALE MOMENT{RESET}")
    line(f"    {DIM}BEFORE{RESET}  (what the cache held) : total = ${RED}{before:,.2f}{RESET}")
    line(f"    {DIM}AFTER {RESET}  (what you just got)   : total = ${GREEN}{BOLD}{after:,.2f}{RESET}")
    line(f"    {BOLD}→ the number moved by {GREEN}{delta:+,.2f}{RESET}{BOLD}. "
         f"Cartograph returned the NEW truth — never the stale ${before:,.2f}.{RESET}")
    rule("━", BOLD + CYAN)
    pause(1.6)

    # ---- precision finale: it isn't paranoid either ----
    step_header(5, TOTAL, "Now change an UNRELATED column — out of band.")
    rule("·", GREY)
    line(f"{DIM}  And it doesn't over-react. customers.email is not in the query's footprint:{RESET}")
    line(f"     {GREY}UPDATE customers SET email='changed@ex.com' WHERE segment='ent'{RESET}")
    rule("·", GREY)
    oob("UPDATE customers SET email = 'changed@ex.com' WHERE segment='ent'")
    pause(1.0)

    r6 = ask(cg, 6, TOTAL, "The agent asks the same query once more.",
             expect="cache")
    verdict = (f"{GREEN}stayed CACHED — precise: an unrelated write did NOT invalidate.{RESET}"
               if r6.source == "cache"
               else f"{RED}re-executed — over-invalidated!{RESET}")
    line(f"    {DIM}→ {verdict}{RESET}")
    pause(1.2)

    # ---- the verdict ----
    seq = [r1.source, r2.source, r4.source, r6.source]
    expected = ["live", "cache", "live", "cache"]
    stats = cg.stats()
    line()
    banner("VERDICT")
    line(f"  source sequence : {seq}")
    line(f"  expected        : {expected}   "
         + (f"{GREEN}{BOLD}✓ exact match{RESET}" if seq == expected
            else f"{RED}✗ mismatch{RESET}"))
    line(f"  hit rate        : {stats['hit_rate'] * 100:.0f}%   "
         f"({stats['hits']} hits / {stats['hits'] + stats['misses']} cacheable)")
    line(f"  {BOLD}stale answers served : {GREEN}{stats['stale_count']}{RESET}{BOLD}  "
         f"— zero, by construction.{RESET}")
    rule("═", CYAN)
    line()
    line(f"  {DIM}This is the whole guarantee: cached or live, every answer matches your data.{RESET}")
    line(f"  {DIM}Point your own agent at it over MCP — see the repo README.{RESET}")
    line()

    cg.close()
    sys.exit(0 if seq == expected and stats["stale_count"] == 0 else 1)


if __name__ == "__main__":
    main()
