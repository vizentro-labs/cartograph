<!-- Thanks for contributing! Keep the one promise: never serve a stale answer. -->

## What & why

<!-- What does this change, and why? Link any related issue (e.g. Closes #123). -->

## Soundness checklist

- [ ] My change does **not narrow** any footprint — it can only widen or refuse. If it *could* narrow one, I've explained why the new footprint is still a provable superset.
- [ ] `ruff check src` passes.
- [ ] Tested against a real Postgres: `pytest -q` and `python benchmarks/ci_fuzz.py` both **0 stale**.
- [ ] If I touched `extract.py` or `runtime.py`, I ran the full `benchmarks/fuzz_postgres.py` and it stayed at **0 stale**.

## Notes

<!-- Anything reviewers should know: tradeoffs, follow-ups, screenshots. -->
