"""Cartograph: never-stale SQL result caching over a real Postgres.

Public API (thin facade over the proven spike3/spike4 core):
    from cartograph import Cartograph
    cg = Cartograph(dsn=...)
    cg.query(sql)    -> QueryResult(rows, source, footprint_mode, as_of_lsn)
    cg.explain(sql)  -> {cacheable, refusal_reason?, footprint}
    cg.stats()       -> {hit_rate, refusals_by_reason, stale_count, ...}
"""

from .core import Cartograph, QueryResult

__all__ = ["Cartograph", "QueryResult"]
