"""
Sound footprint extraction from arbitrary SQL (graduated from spike 3, unchanged
algorithm).

extract(sql, schema, pk, dialect, mode) -> one of:
    ("refuse", reason)
    ("column", sorted_tuple_of_keys)     # keys: (table, col) and (table, "*rowset")
    ("row", table, pk_col, literal)      # provably-selective PK lookup

Soundness rests on a single property: the returned key set is a *superset* of the
(table, column) pairs the answer depends on, plus a "*rowset" sentinel per source
table (for inserts/deletes). When uncertain, it widens or refuses — never narrows.
"""

from sqlglot import exp, parse_one
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.pushdown_projections import pushdown_projections

# functions that make a query nondeterministic / unanalyzable -> never cacheable
NONDET_NAMES = {"RAND", "RANDOM", "NOW", "CURRENT_TIMESTAMP", "CURRENT_DATE",
                "CURRENT_TIME", "CURRENT_DATETIME", "UNIXEPOCH", "UUID",
                "RANDOMBLOB", "CURRENT_USER", "LAST_INSERT_ROWID"}
NONDET_CLASSES = (exp.Rand, exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentTime)


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

    if mode == "refuse":                  # strict baseline (refuse CTE/derived)
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
