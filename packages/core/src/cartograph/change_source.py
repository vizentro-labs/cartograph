"""
ChangeSource: the per-engine seam (design: docs/change-source-design.md).

Everything engine-specific about *change capture* lives behind this interface:
the connection, schema introspection, the version tracker fed by the change
stream, the current position token, and demo provisioning. The rest of Cartograph
(extract, Cache, the serve path in core.py) is engine-agnostic.

This is a BEHAVIOR-PRESERVING extraction: PostgresChangeSource delegates to the
proven `runtime` functions unchanged, so the executed code path is identical to
before the refactor. Adding a new engine (e.g. MySQL via binlog) means writing a
new ChangeSource; nothing in the core changes.

NOT YET LOCKED by the differential fuzzer in this environment. Before trusting or
merging to main, run `benchmarks/fuzz_postgres.py` against a real Postgres and
confirm 0 stale — that is the only proof that this refactor preserved the
guarantee.
"""

from abc import ABC, abstractmethod

from . import runtime


class ChangeSource(ABC):
    """One adapter per database engine. The capability flags select the
    consistency mode the core can offer (see docs/change-source-design.md)."""

    #: sqlglot dialect used for footprint extraction
    dialect = "postgres"
    #: per-column update precision available? (else bump-all-on-update, still sound)
    column_level = True
    #: can we cheaply drain-to-now before each serve? False => bounded freshness
    synchronous = True

    @abstractmethod
    def query_connection(self):
        """A DB-API connection used to execute user queries."""

    @abstractmethod
    def discover_tables_pk(self):
        """-> (tables: list[str], pk: dict[table, pk_col])."""

    @abstractmethod
    def read_schema(self):
        """-> {table: {column: type}} for tracked tables. Types are included so a
        column TYPE change can invalidate (footprint ignores types; this doesn't)."""

    @abstractmethod
    def new_version_tracker(self):
        """-> a fresh version tracker bound to the change stream. Duck-typed to
        runtime.WalVersions: attrs `v` (dict) and `schema_dirty` (bool); methods
        bump(table, col) and drain() -> set of bumped (table, col) keys."""

    @abstractmethod
    def current_position(self, qconn):
        """-> opaque, monotonic position token the served answer is correct as of
        (Postgres: WAL LSN; MySQL: GTID / binlog coords)."""

    @abstractmethod
    def on_schema_change(self, cache, ver):
        """Re-read schema, invalidate footprints on changed tables. -> (changed, n)."""

    def provision_demo(self):
        """(Re)create the throwaway demo schema + change-capture infra (admin)."""
        raise NotImplementedError


class PostgresChangeSource(ChangeSource):
    """Postgres via WAL logical decoding (plugin=test_decoding).

    Behavior-preserving wrapper over the proven `runtime` module: every method
    just forwards to the existing function, so this changes structure, not
    behavior."""

    dialect = "postgres"
    column_level = True     # requires REPLICA IDENTITY FULL (set by bootstrap / doctor)
    synchronous = True      # drain to the current LSN before each serve

    def query_connection(self):
        return runtime.conn()

    def discover_tables_pk(self):
        return runtime.discover_tables_pk()

    def read_schema(self):
        return runtime.read_schema()

    def new_version_tracker(self):
        return runtime.WalVersions()

    def current_position(self, qconn):
        cur = qconn.cursor()
        cur.execute("SELECT pg_current_wal_lsn()::text")
        return cur.fetchone()[0]

    def on_schema_change(self, cache, ver):
        return runtime.on_schema_change(cache, ver)

    def provision_demo(self):
        runtime.bootstrap()
