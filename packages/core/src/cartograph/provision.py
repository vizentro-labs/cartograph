"""
Provisioner — "connect a DSN and go".

`provision(dsn)` idempotently sets up Cartograph's change-capture infra on an
EXISTING database without touching your data:
  - REPLICA IDENTITY FULL on the tracked tables (per-column update precision)
  - a cg_meta table + DDL event trigger (schema-change signal on the WAL stream)
  - a logical replication slot (plugin=test_decoding)

The one thing it cannot do on a database it doesn't own is flip `wal_level`
(needs a restart) — it detects that and tells you exactly what to run, tailored
to your provider (RDS/Aurora, Cloud SQL, self-hosted).

`cartograph-doctor [DSN]` runs it and prints a readiness report.
"""

import os
import sys
import psycopg2

from . import config


def _conn(dsn):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    return c


def _discover(cur):
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE'
                     AND table_name <> 'cg_meta' ORDER BY table_name""")
    return [r[0] for r in cur.fetchall()]


def _provider(cur):
    try:
        cur.execute("SELECT current_setting('rds.logical_replication', true)")
        if cur.fetchone()[0] is not None:
            return "rds"
    except Exception:
        pass
    cur.execute("SELECT version()")
    v = cur.fetchone()[0].lower()
    if "google" in v or "cloud sql" in v:
        return "cloudsql"
    return "self-hosted"


def _wal_instruction(provider):
    return {
        "rds": "RDS/Aurora: set rds.logical_replication=1 in the parameter group, then reboot once.",
        "cloudsql": "Cloud SQL: set the cloudsql.logical_decoding flag to on, then restart the instance.",
    }.get(provider,
          "Self-hosted: ALTER SYSTEM SET wal_level='logical';  (or edit postgresql.conf), then restart Postgres.")


def preflight(dsn, slot=None):
    """Read-only status of everything Cartograph needs."""
    slot = slot or config.SLOT
    c = _conn(dsn); cur = c.cursor()
    cur.execute("SHOW wal_level"); wal = cur.fetchone()[0]
    cur.execute("SELECT current_setting('is_superuser'), "
                "(SELECT rolreplication FROM pg_roles WHERE rolname=current_user)")
    su, repl = cur.fetchone()
    cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name=%s", (slot,))
    slot_ok = cur.fetchone() is not None
    cur.execute("SELECT 1 FROM pg_event_trigger WHERE evtname='cg_ddl_end'")
    trig_ok = cur.fetchone() is not None
    tables = _discover(cur)
    ri = {}
    for t in tables:
        cur.execute("SELECT relreplident FROM pg_class WHERE oid=%s::regclass", (t,))
        row = cur.fetchone()
        ri[t] = bool(row and row[0] == "f")
    provider = _provider(cur)
    c.close()
    return {
        "dsn": dsn, "slot": slot, "provider": provider, "tables": tables,
        "wal_level": wal, "wal_ok": wal == "logical",
        "can_replicate": (su == "on") or bool(repl),
        "slot_ok": slot_ok, "trigger_ok": trig_ok,
        "replica_identity": ri,
        "ready": wal == "logical" and slot_ok and trig_ok and (all(ri.values()) if ri else False),
    }


def provision(dsn, slot=None, tables=None):
    """Idempotently create Cartograph's infra on an existing DB. Non-destructive."""
    slot = slot or config.SLOT
    c = _conn(dsn); cur = c.cursor()
    report = {"slot": slot, "created": [], "already": [], "needs_action": [], "errors": []}

    cur.execute("SHOW wal_level")
    if cur.fetchone()[0] != "logical":
        report["needs_action"].append(_wal_instruction(_provider(cur)))
        report["ready"] = False
        c.close()
        return report

    tables = tables if tables is not None else _discover(cur)
    report["tables"] = tables

    for t in tables:
        try:
            cur.execute("SELECT relreplident FROM pg_class WHERE oid=%s::regclass", (t,))
            if cur.fetchone()[0] != "f":
                cur.execute(f'ALTER TABLE "{t}" REPLICA IDENTITY FULL')
                report["created"].append(f"replica identity full: {t}")
            else:
                report["already"].append(f"replica identity full: {t}")
        except Exception as e:
            report["errors"].append(f"replica identity {t}: {e}")

    try:
        cur.execute("CREATE TABLE IF NOT EXISTS cg_meta(id int PRIMARY KEY, schema_version int)")
        cur.execute("INSERT INTO cg_meta VALUES (1,0) ON CONFLICT (id) DO NOTHING")
        cur.execute("ALTER TABLE cg_meta REPLICA IDENTITY FULL")
        cur.execute("""CREATE OR REPLACE FUNCTION cg_on_ddl() RETURNS event_trigger AS $$
            BEGIN UPDATE cg_meta SET schema_version = schema_version + 1 WHERE id=1; END
            $$ LANGUAGE plpgsql;""")
        for ev, kind in (("cg_ddl_end", "ddl_command_end"), ("cg_ddl_drop", "sql_drop")):
            cur.execute("SELECT 1 FROM pg_event_trigger WHERE evtname=%s", (ev,))
            if not cur.fetchone():
                cur.execute(f"CREATE EVENT TRIGGER {ev} ON {kind} EXECUTE FUNCTION cg_on_ddl()")
                report["created"].append(f"event trigger: {ev}")
            else:
                report["already"].append(f"event trigger: {ev}")
    except Exception as e:
        report["errors"].append(f"ddl trigger: {e}")

    try:
        cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name=%s", (slot,))
        if not cur.fetchone():
            cur.execute("SELECT pg_create_logical_replication_slot(%s,'test_decoding')", (slot,))
            report["created"].append(f"replication slot: {slot}")
        else:
            report["already"].append(f"replication slot: {slot}")
    except Exception as e:
        report["errors"].append(f"slot: {e}")

    report["ready"] = not report["errors"] and not report["needs_action"]
    c.close()
    return report


# --------------------------------------------------------------------------
def doctor(dsn=None, slot=None):
    """Provision (idempotent) + print a readiness report. Returns ready bool."""
    dsn = dsn or os.environ.get("CARTOGRAPH_DSN") or config.DSN
    slot = slot or config.SLOT
    print(f"\n  cartograph doctor — {dsn}\n  {'-'*52}")
    prov = provision(dsn, slot=slot)
    pre = preflight(dsn, slot=slot)

    def mark(ok):
        return "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"

    print(f"  {mark(pre['wal_ok'])} wal_level = {pre['wal_level']}"
          + ("" if pre["wal_ok"] else "   <-- the one manual step"))
    print(f"  {mark(pre['can_replicate'])} replication privilege")
    print(f"  {mark(pre['slot_ok'])} replication slot '{pre['slot']}' (test_decoding)")
    print(f"  {mark(pre['trigger_ok'])} DDL event trigger")
    ri = pre["replica_identity"]
    print(f"  {mark(bool(ri) and all(ri.values()))} replica identity full "
          f"({sum(ri.values())}/{len(ri)} tables)")
    print(f"  · provider: {pre['provider']} · tracking {len(pre['tables'])} tables")

    if prov["created"]:
        print("\n  provisioned:")
        for x in prov["created"]:
            print(f"    + {x}")
    if prov["needs_action"]:
        print("\n  \033[33maction needed (we can't do this on a DB we don't own):\033[0m")
        for x in prov["needs_action"]:
            print(f"    ! {x}")
    if prov["errors"]:
        print("\n  \033[31merrors:\033[0m")
        for x in prov["errors"]:
            print(f"    x {x}")

    print(f"\n  {'READY — connect an agent and go.' if pre['ready'] else 'NOT READY — see action above.'}\n")
    return pre["ready"]


USAGE = """\
cartograph-doctor: set up an EXISTING Postgres for Cartograph.
Idempotent and non-destructive: it only ever creates a slot, a DDL trigger, and a
metadata table, and sets REPLICA IDENTITY. It never writes your data tables.

Usage:
  cartograph-doctor [DSN]
  cartograph-doctor --help

Arguments:
  DSN    libpq connection string, e.g. "postgres://user@host:5432/dbname".
         If omitted, falls back to $CARTOGRAPH_DSN.

Checks / provisions:
  - wal_level = logical          (the one thing we can't set on a DB we don't own)
  - a logical replication slot   (plugin: test_decoding)
  - a DDL event trigger
  - REPLICA IDENTITY FULL         (per-column invalidation precision)

Environment:
  CARTOGRAPH_DSN    default DSN if none is given
  CARTOGRAPH_SLOT   replication slot name (default: cg)
"""


def cli():
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(USAGE)
        sys.exit(0)
    dsn = args[0] if args else None
    if dsn is None and not os.environ.get("CARTOGRAPH_DSN"):
        print(USAGE)
        print("error: no DSN given and $CARTOGRAPH_DSN is not set")
        sys.exit(2)
    ok = doctor(dsn)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    cli()
