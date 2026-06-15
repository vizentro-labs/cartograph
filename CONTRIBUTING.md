# Contributing

Thanks for your interest in Cartograph. The core promise is one thing: **never
serve a stale answer.** Keep that in mind and the rest follows.

## Dev setup

```bash
cd packages/core
pip install -e ".[dev]"            # ruff + pytest + api extras  (use `py -m` on Windows)
python -m cartograph.bootstrap_demo   # needs a wal_level=logical Postgres
```

## Before you open a PR

```bash
cd packages/core
ruff check src                                       # lint (line-length 100, py310)
CARTOGRAPH_DSN=... pytest -q                          # MCP integration test, expect 0 stale
CARTOGRAPH_DSN=... python benchmarks/ci_fuzz.py       # the soundness gate
```

CI runs the same lint + tests against a real Postgres on every push.

## The one invariant that matters

Any change to `extract.py` or `runtime.py` touches the never-stale guarantee:

- **Conservative = correct.** When unsure, a footprint must **widen** (more
  invalidation) or **refuse** — never narrow. A too-narrow footprint silently
  serves stale answers, the single scariest failure mode.
- **Validate with the fuzzer.** `benchmarks/fuzz_postgres.py` is how every
  soundness bug was caught. A change that could make a footprint smaller needs to
  be proven a superset, with a fuzzer run that stays at 0 stale.

If you find a way to make it serve stale, that's a security report — see
[`SECURITY.md`](SECURITY.md).
