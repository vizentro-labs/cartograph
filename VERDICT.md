# Cartograph cache spike — VERDICT

**Hypothesis:** you can cache a SQL result and *cheaply* (no re-run, no LLM)
detect when the underlying data changed enough that the cached answer is now
wrong, invalidate precisely, and **never serve a stale answer** while avoiding
most re-executions.

**Result: SUPPORTED — with one honest asterisk.** The correctness promise holds
unconditionally and cheaply. The "avoid *most* re-executions" promise is real
but workload-dependent. Run it yourself: `python3 spike.py`.

## What was built
- In-memory SQLite, `customers / orders / line_items`, seeded 800 / 4000 / 9000.
- A `Versions` tracker: every mutation does O(1) bumps of a global epoch, a
  per-table version, per-`(table,column)` versions, and a per-table `*rowset`
  version (inserts/deletes only). No scanning, no LLM — this is the "cheap
  fingerprint" substrate.
- 5 query shapes (point, filtered range, aggregate, 2-table join, group-by),
  each with a hand-declared dependency footprint.
- Strategies: `no-cache`, `ttl-{5,25,100}`, `on-any-write`, `footprint-table`,
  `footprint-column` (**ours**), `footprint-precise`.
- **Ground truth = re-running every query live at every step.** A hit is scored
  "stale" iff the cached answer ≠ ground truth at that instant. ~8k query
  evaluations per scenario.

## Headline numbers (footprint-column = "ours")

| mut.frac | ours hit% | ours stale% | on-any-write hit% | best TTL w/ 0 stale |
|---------:|----------:|------------:|------------------:|--------------------:|
| 0.50 (write-heavy) | 14.9% | **0.000%** | 5.7% | none (0%) |
| 0.30 (mixed)       | 25.6% | **0.000%** | 10.6% | none (0%) |
| 0.15 (read-heavy)  | 41.3% | **0.000%** | 20.9% | none (0%) |

`footprint-precise` (value-level fingerprint on the point lookup) reaches 30→54%
hit, still at 0.000% stale.

## Gates
- **Gate 1 — footprint stale-hit ≈ 0%: PASS** in all 3 scenarios (exactly 0.000%).
  Zero is *by construction* (conservative invalidation); the experiment's job was
  to confirm the hand-declared footprints were actually correct supersets — a
  too-narrow footprint would have leaked stale hits. None leaked.
- **Gate 2 — beats invalidate-on-any-write: PASS.** ~2× the hit rate everywhere,
  because writes to unreferenced tables/columns (line_items churn, customer
  email edits) don't invalidate order aggregates. Same zero staleness.
- **Gate 3 — no TTL dominates: PASS.** Every TTL we tried carried stale hits
  (ttl-5 read-heavy was the best at 0.50%, still > 0). To get TTL down to zero
  stale you must set TTL→0, i.e. no cache. TTL buys hit rate strictly by gambling
  on correctness; footprint buys it for free.

## What broke / surprised me
- **The cost is over-invalidation, not staleness.** Hit rate is capped by how
  often *footprint columns* get written. On the hot `orders` table under
  write-heavy load, expensive aggregates almost never survive — so caching saves
  little there. That's correct conservatism, not a bug, but it means the savings
  promise is conditional on read-heaviness; the *correctness* promise is not.
- **TTL never reached zero stale.** Even a 5-tick TTL serves wrong answers. There
  is no "safe" TTL short of 0. That alone is the case for footprint invalidation.

## Where footprint extraction got hard (the real risk)
1. **Rows vs values.** `COUNT(*)` depends on which rows *exist*, not on any
   column value; `SUM(amount)` depends on both. Needed a `*rowset` sentinel
   distinct from value-columns, bumped only on insert/delete. Easy to get wrong.
2. **Value/row-level precision is only cheap when the predicate is selective.**
   A point lookup `WHERE id=42` is unaffected by a write to id=99 — but the cheap
   way to know that is to fetch row 42, which is basically *running the query*.
   For an aggregate, a value-level fingerprint ≈ re-executing it. So the queries
   you most want to cache (expensive aggregates) are exactly the ones where you
   **cannot** cheaply get row-level precision and must fall back to column-level
   and eat conservative misses. This is the fundamental ceiling, and it showed up
   cleanly: `footprint-precise` only beat `footprint-column` on the point lookup.
3. **Footprints were declared by hand.** Real extraction needs SQL parsing and
   ideally the planner. `SELECT *`, expressions, functions, subqueries, and views
   make this materially harder — and a footprint that is too *narrow* silently
   breaks the zero-stale guarantee. That is the scariest failure mode and the
   thing a real Cartograph must get provably right.

## Threats to validity
Single in-memory SQLite, synthetic uniform data, logical-time TTL, a stylized
cost model, and hand-declared footprints. The zero-stale result is partly by
construction — its empirical worth is confirming the footprint declarations were
correct supersets. Scaling to real SQL hinges entirely on point (3) above.

## Bottom line
The core idea holds: cheap O(1) version counters keyed by a *conservative*
dependency footprint give you **never-stale** caching that strictly beats both
TTL (which can't reach zero stale) and blunt invalidate-on-any-write (~2× worse
hit rate). The product risk is not correctness-in-principle; it's (a) automatic,
provably-conservative footprint extraction from arbitrary SQL, and (b) selling
savings that evaporate under write-heavy load on hot tables. A clean **yes** on
the hypothesis, conditional on solving footprint extraction safely.

---

# Spike 2 — does zero-stale survive *automatic* footprint extraction?

Spike 1's caveat was that footprints were declared **by hand**. Spike 2 replaces
that with automatic extraction from arbitrary SQL via **sqlglot** (`parse` +
`qualify` against the schema: column resolution, star expansion, validation),
then attacks it with a **differential fuzzer** — random analytical queries
interleaved with random inserts/updates/deletes across every table and column,
comparing every cache HIT against the live DB. Run: `python3 spike2.py`.

**Result: zero-stale SURVIVED. GO on "sound automatic extraction is feasible" —
with a coverage asterisk on CTE/derived-table-heavy SQL.**

## Numbers (240k events, read-heavy, mutation_fraction=0.2)

| metric | value |
|---|---|
| **STALE-HIT RATE** (auto extraction) | **0.0000%** — 0 of **39,259** live-checked hits |
| **Coverage** (cacheable / all queries) | **89.1%** (row-level 15,170 · column-level 156,028) |
| **Hit rate** among cacheable (read-heavy) | **22.9%** vs **5.2%** invalidate-on-any-write (4.4×) |
| Refused | 10.9% — see breakdown |

**Refusal breakdown (the widening triggers):** derived/unresolved column 3.9%
(subquery in FROM), CTE/unknown table 3.0%, unknown/UDF function 2.0%,
nondeterministic function 2.0%.

## Gates
- **Gate 1 — 0.000% stale under auto-extraction: PASS.** 0 stale across 39,259
  hits spanning point / range / aggregate / group-by / 2- & 3-table join / and
  `IN`/`EXISTS`/scalar-subquery shapes, under heavy mutation of every column.
- **Gate 2 — coverage ≥ 70%: PASS at 89.1%** *on this synthetic analytical mix.*
  Read this honestly (below).
- **Gate 3 — beats invalidate-on-any-write: PASS,** 22.9% vs 5.2% (4.4×) on the
  identical cacheable population, at zero stale.

## What worked (and was non-obvious)
- **Soundness only needs the *set* of `(table, column)` a query reads** — not a
  query plan. So parser-level column lineage is sufficient for the guarantee:
  footprint = `{(t,'*rowset') for each source table}` (inserts/deletes) ∪
  `{(t,col) for each referenced column}` (updates). If the result changes, some
  referenced column changed or a row entered/left a source — both bump. We never
  needed EXPLAIN / cardinality for correctness.
- **Subqueries in predicates are cacheable and sound** — `IN (SELECT …)`,
  correlated `EXISTS`, scalar subqueries in the projection. Column lineage flows
  to base tables and the footprint is the union across the whole tree. These are
  common in analytics, so capturing them matters a lot for coverage.
- **Row/key narrowing** stayed sound by restricting it to a single-table,
  single-SELECT query with a top-level PK equality, fingerprinted by reading the
  actual row.

## Coverage killers (honest)
- **CTEs (`WITH`) and derived tables (subquery in `FROM`) are refused outright**,
  because their *output* columns can't be soundly mapped back to base columns
  without recursing through the subquery's projection. Our synthetic mix is only
  ~11% such queries, so coverage looks great (89%) — but **real analytical SQL
  (dbt models, BI tools) is dense with CTEs**, so real-world coverage would be
  materially lower. 89% is "the mechanism works," not "you'll get 89% on a
  warehouse." Closing this is the #1 follow-up: map lineage *through* derived
  tables/CTEs (sqlglot has a `lineage` module that can push this further).
- UDFs / unknown functions and nondeterministic functions (`RANDOM`,
  `CURRENT_DATE`, …) are refused by nature — correctly uncacheable, not a defect.

## The subtle bug the fuzzer's design exposed
`qualify` leaves `ORDER BY` / `GROUP BY` references to *projected outputs*
unqualified (`table=''`, e.g. `ORDER BY c.segment` where `c.segment` is
selected). A naive "refuse on any unqualified column" was still **sound** but
**over-refused** — coverage collapsed to 42%. The fix: recognize output-alias
refs (their base columns are already captured) and skip them. Lesson: making
extraction *sound* is easy; making it *sound AND high-coverage* is where the
fiddly correctness reasoning lives.

## Is sqlglot enough, or do we need the planner?
**For the zero-stale guarantee, sqlglot is enough** — `qualify` gives sound,
validated column lineage at column granularity, which is all soundness requires.
A planner / `EXPLAIN` would only help **coverage and precision**, not
correctness: (a) lineage through derived tables/CTEs to stop refusing them, and
(b) proving additional selective predicates safe for row-level narrowing.

## Threats to validity
Single dialect (SQLite); schema is known and **static** — DDL / schema changes
aren't modeled (they'd need their own invalidation); **views** are refused
unless inlined-then-analyzed; the workload generator is synthetic; row-narrowing
trusts declared PK uniqueness. The 0-stale result is by-construction
(over-approximation) — the fuzzer's job was to catch extraction bugs that break
the *superset* property (under-capture), and none survived 39k checks.

## Go / No-go
**GO.** Sound automatic extraction is feasible: sqlglot column lineage yields a
conservative footprint, zero-stale held under tens of thousands of fuzzed
live-checks, and flat analytical SQL caches at high coverage with a 4.4×
hit-rate edge over blunt invalidation. The remaining risk is **coverage on
CTE/derived-table-heavy real workloads** — an extraction-completeness problem
(lineage through subqueries), not a correctness blocker. The never-stale promise
holds; the open question is now "how much of *real* SQL can we cover without
giving it up," and the path to widen coverage is clear.

---

# Spike 3 — real-world coverage, and does a coarse fallback rescue CTEs?

Spike 2 stayed zero-stale but **refused every CTE / derived table** — and real
analytical SQL is full of them. Spike 3 measures coverage on a **real corpus**
(the 103 TPC-DS queries + 5 real dbt `jaffle_shop` models) and tests whether a
**coarse conservative fallback** makes CTEs cacheable without giving up
zero-stale. Run: `python3 spike3.py` (fetches/caches the corpus).

**Result: GO. Coarse-fallback rescues CTEs — coverage on real SQL jumps from
~43% to 99% at zero stale, with a useful hit rate. The never-stale promise
holds on real analytical SQL.**

## Three modes (all must stay 0-stale)
- **refuse** = spike 2 (refuse any CTE/derived table).
- **coarse** = don't refuse; footprint = ∪ every `(base_table,column)` referenced
  *anywhere* in the AST + `*rowset` per base source table. Sound because every
  base column the result depends on appears base-qualified *somewhere* in the
  tree (inside the CTE/subquery body), so the union is a superset. No
  lineage-through-subquery needed.
- **lineage** = run sqlglot's `pushdown_projections` (semantics-preserving)
  before extraction, dropping CTE columns that don't reach the result → tighter,
  still-sound footprint.

## Coverage on the real corpus (parse-only)

| corpus | CTE / derived | refuse | **coarse** | lineage |
|---|---|---:|---:|---:|
| **TPC-DS** (103 q) | 34 CTE · 42 derived | 42.7% | **99.0%** | 99.0% |
| **dbt jaffle_shop** (5 models) | 5 CTE | 0.0% | **100%** | 100% |

Coarse refuses exactly **1** TPC-DS query (q30) — a `qualify_failed`: a column
not present in our DDL version. That's the honest failure mode: qualify is only
as good as the schema you feed it; **schema drift → refuse (safe, uncached)**.

## Soundness + hit rate (differential fuzzer, 160k events, executable CTE corpus)

| mode | cacheable% | hit% (cacheable) | **stale-hit%** | hits checked |
|---|---:|---:|---:|---:|
| refuse | 20.5% | 18.7% | **0.0000%** | 6,157 |
| **coarse** | 95.0% | 25.6% | **0.0000%** | 38,898 |
| **lineage** | 95.0% | 30.6% | **0.0000%** | 46,548 |
| invalidate-on-any-write | — | 11.3% | 0.0000% | 17,240 |

Zero stale across **85,446** coarse+lineage hits, on executable stacked-CTE,
derived-table, correlated-subquery, window, and multi-join shapes, under random
mutation of every base column.

## Gates
- **Gate 1 — 0.000% stale for coarse AND lineage on real-shaped SQL: PASS.**
- **Gate 2 — coarse coverage ≥ 80% on real TPC-DS: PASS at 99.0%** (vs 42.7%
  refuse). CTEs become cacheable, not refused. **This is the headline.**
- **Gate 3 — coarse hit% ≫ any-write: PASS,** 25.6% vs 11.3% (2.3×), zero stale.

## Is coarse enough for v1, or do we need lineage-through?
Coarse is **enough to ship v1**: 99% coverage, 2.3× the hit rate of blunt
invalidation, zero stale, and the implementation is trivial (union of qualified
columns). **But the over-invalidation cost is real and lands exactly where dbt
lives.** The dominant dbt pattern is a staging model that `SELECT`s every column
and a mart that uses a few. On that shape (our "CTE-selects-unused-columns"
focus metric):

| | coarse | lineage |
|---|---:|---:|
| hit% on staging-`SELECT *` CTEs | 46.5% | **69.7%** |

A ~23-point gap — coarse invalidates whenever *any* selected-but-unused column
(e.g. a frequently-churned `email`/`status`) changes; lineage prunes it. Both
stay 0-stale. And lineage costs almost nothing: it's an existing
semantics-preserving optimizer pass (`pushdown_projections`). **Verdict: ship
coarse, but turn lineage on early** — for a dbt/BI ICP the precision tier matters
more than the 5-point overall delta suggests.

## Is sqlglot enough, or do we need the planner?
Still yes. `qualify` gives sound column lineage (soundness); `pushdown_projections`
gives the precision tier. **No planner / EXPLAIN needed at either tier.** The
only coverage loss was schema-resolution (q30), not a planning gap.

## Threats to validity
Coverage is parse-only on real SQL; soundness/hit-rate run on an **executable**
CTE corpus over the small `customers/orders/line_items` schema (TPC-DS isn't
cheaply executable+mutable), with shapes hand-built to mirror TPC-DS/dbt
(stacked CTEs, derived tables, correlated subqueries, windows, multi-join,
`SELECT *` staging). Corpus parsed in **Spark** dialect (8 queries need it; v0
targets Postgres, but extraction is dialect-independent on the AST). Schema/DDL
changes still unmodeled (they'd need their own invalidation); **views** are
refused unless inlined-then-analyzed.

## Go / No-go
**GO.** Real analytical SQL — the CTE/derived-table-heavy stuff spike 2 refused —
is cacheable at **99% coverage, zero stale, 2.3× hit rate**, with a cheap, sound
precision upgrade (lineage) that matters specifically for dbt. Across spikes 1–3
the never-stale promise has held by construction and survived tens of thousands
of live-checked hits on synthetic, auto-extracted, and now real-world SQL. The
core hypothesis is **supported**; the remaining work is engineering coverage
breadth (views, schema-change invalidation, more dialects), not correctness.

---

# Spike 4 — does zero-stale survive a REAL Postgres (out-of-band writes + DDL)?

Spikes 1–3 assumed a **static known schema** and an **in-process** version
tracker the harness controlled. Production breaks both. Spike 4 runs against a
**real PostgreSQL 16** (`wal_level=logical`) where writes and schema changes
arrive **out-of-band**, and the cache learns about them *only* from Postgres's
own change stream. Run: `python3 spike4.py` (needs the local PG cluster).

**Result: GO. Zero-stale survives a real out-of-band-write Postgres and a DDL
battery — but only with three things the fuzzer forced me to get right (below).
The never-stale promise is true against a real database, not just a harness.**

## Change capture (no in-process interception)
- **Data:** a logical replication slot, plugin **`test_decoding`**. Version-counter
  bumps are derived purely by decoding the WAL: `INSERT`/`DELETE` → bump
  `(table,'*rowset')`; `UPDATE` → diff old vs new tuple (`REPLICA IDENTITY FULL`)
  and bump exactly the changed `(table,column)`s.
- **DDL:** an event trigger (`ddl_command_end` + `sql_drop`) bumps a `cg_meta`
  row; that bump **rides the same WAL stream**, so one synchronous drain captures
  data *and* schema changes. On a schema bump: re-read `information_schema`,
  invalidate every footprint touching a changed table, re-extract against the new
  schema.

## Results

| | |
|---|---|
| iterations | 120,000 |
| out-of-band writes fired (2 txn threads + bulk ETL) | **89,368** |
| WAL bumps applied | 99,371 |
| **clean cache hits checked vs live** | **28,310** |
| **STALE HITS** | **0  (0.0000%)** |
| hit rate (footprint) vs invalidate-on-any-write | 23.6% vs 5.1% (4.6×) |

**DDL battery — all PASS (no stale, no crash):** rename footprint column →
**safe-refuse** (`qualify_failed`); add column → **correct-invalidate**;
change column type → **correct-invalidate** (type-aware); drop referenced table →
**safe-refuse** (`unknown_or_dropped_table`); rename + write old&new name → refuse.

## Gates
- **Gate 1 — 0.000% stale under out-of-band data writes: PASS** (0 / 28,310 clean
  hits, driven only by the WAL stream).
- **Gate 2 — 0.000% stale across the DDL battery: PASS** — every case is
  correct-invalidate or safe-refuse; never a wrong answer.
- **Gate 3 — write→bump LAG window: characterized and closeable.** With **lazy
  polling** there is a window = the poll interval, in which a committed write is
  not yet bumped → a stale hit is possible. We **close it** by draining the slot
  **synchronously to the current WAL LSN before each serve**: the answer is then
  correct *as-of that LSN*. Cost: one `pg_logical_slot_get_changes` round-trip per
  serve (decode cost ∝ undrained WAL), amortizable by batching.

## The verification race, and why it doesn't weaken the result
40.6% of hit-checks were **excluded** because a relevant write committed during
the fuzzer's live-read verification (between the decision drain and the ground-
truth query). This is a **differential-test artifact, not a production stale**:
in production there is no second live read — the cache serves the value as-of the
drained LSN, which is correct at that LSN. Crucially, the exclusion **cannot hide
a real bug**: a genuinely *missed* write would never appear in the verification
drain, so it would be flagged stale, not excluded. The 28,310 clean checks are
real zero-stale evidence.

## Three things the fuzzer caught (it earned its keep)
1. **Unsound store on a race.** Storing a freshly-computed result under a
   fingerprint captured *after* a concurrent write caches a stale value under a
   fresh key → it produced real stale hits until fixed. **General lesson:** a
   result's fingerprint must reflect a WAL position ≤ the result's snapshot, with
   no relevant write slipping in between compute and fingerprint.
2. **Type-aware schema diff.** A column **type** change (e.g. `numeric→text`) can
   alter results but leaves the column *set* unchanged — a set-only diff misses
   it. Invalidation must diff `(column, type)`, even though the footprint itself
   ignores types.
3. **Dropped base table ≠ CTE alias.** Coarse mode treats unknown table refs as
   CTE aliases; after a `DROP`, a real base table becomes "unknown" and would be
   silently dropped from the footprint. Guard: refuse when a referenced table
   isn't a CTE and isn't in the schema.

## Cost / infra of the change-capture mechanism (honest)
- `wal_level=logical` + a **replication slot**, which **retains WAL until
  consumed** — if the cache stops draining, WAL accumulates and can fill the
  disk. Real operational hazard; needs monitoring + a drop-on-failure policy.
- **`REPLICA IDENTITY FULL`** (for per-column update precision) logs the full old
  row on every update — materially more WAL. Without it you fall back to
  bump-all-columns-on-update (sound, coarser hit rate).
- A synchronous decode per serve (or per batch) for the zero-window guarantee.

## Go / No-go
**GO, qualified.** Against a real out-of-band Postgres the never-stale promise
holds: 0 stale across 28k live-checked hits and a DDL battery, using only
Postgres's own WAL + an event trigger. The window is closeable by synchronous
drain-before-serve. The price is real infra (logical slot lifecycle, `REPLICA
IDENTITY FULL` WAL overhead, per-serve decode) and three correctness details the
fuzzer surfaced. Across spikes 1–4 the promise has now survived synthetic,
auto-extracted, real-world, and real-database out-of-band conditions — the core
hypothesis is supported end to end; remaining work is operational hardening, not
correctness.
