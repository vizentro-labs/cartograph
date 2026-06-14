"""
End-to-end integration test: drives the Cartograph MCP tools THROUGH the real
MCP server interface (in-memory client<->server transport) against a real
Postgres, asserting the demo's source transitions (live/cache/live/cache) and
that the stale count stays 0.

Run:  python tests/test_mcp_integration.py   (or: pytest tests/test_mcp_integration.py)
Needs the wal_level=logical demo cluster up.
"""

import os
import sys
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import psycopg2
from mcp.shared.memory import create_connected_server_and_client_session

from cartograph import config
from cartograph import Cartograph
import cartograph.mcp_server as srv

Q = ("SELECT c.segment, COUNT(*) AS n, ROUND(COALESCE(SUM(o.amount),0),2) AS total "
     "FROM customers c JOIN orders o ON o.customer_id=c.id "
     "WHERE c.segment='ent' GROUP BY c.segment")


def _oob(sql, args=None):
    c = psycopg2.connect(config.DSN); c.autocommit = True
    c.cursor().execute(sql, args); c.close()


def _payload(call_result):
    """Unwrap the JSON a tool returned (first TextContent)."""
    return json.loads(call_result.content[0].text)


async def _run():
    # fresh DB + slot, and reset the server's lazy singleton so it attaches to it
    Cartograph.bootstrap()
    srv._CG = None

    async with create_connected_server_and_client_session(srv.server) as client:
        # tools are discoverable
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"query", "explain", "stats"} <= tools, tools

        # explain: cacheable + a refusal, no execution
        ex = _payload(await client.call_tool("explain", {"sql": Q}))
        assert ex["cacheable"] is True, ex
        ex_no = _payload(await client.call_tool("explain",
                                                {"sql": "SELECT * FROM orders WHERE amount > random()"}))
        assert ex_no["cacheable"] is False and ex_no["refusal_reason"], ex_no

        sources = []

        # 1) cold -> live
        r1 = _payload(await client.call_tool("query", {"sql": Q}))
        sources.append(r1["source"])

        # 2) again -> cache
        r2 = _payload(await client.call_tool("query", {"sql": Q}))
        sources.append(r2["source"])

        # 3) out-of-band write inside the footprint
        cur = psycopg2.connect(config.DSN).cursor()
        cur.execute("SELECT o.id FROM orders o JOIN customers c ON c.id=o.customer_id "
                    "WHERE c.segment='ent' ORDER BY o.id LIMIT 1")
        oid = cur.fetchone()[0]
        _oob("UPDATE orders SET amount = amount + 1000 WHERE id=%s", (oid,))

        # 4) same query -> live again (change caught, NOT stale)
        r4 = _payload(await client.call_tool("query", {"sql": Q}))
        sources.append(r4["source"])

        # 5) out-of-band write to an UNRELATED column -> stays cache
        _oob("UPDATE customers SET email='changed@ex.com' WHERE segment='ent'")
        r6 = _payload(await client.call_tool("query", {"sql": Q}))
        sources.append(r6["source"])

        st = _payload(await client.call_tool("stats", {}))

    # ---- assertions ----
    assert sources == ["live", "cache", "live", "cache"], sources
    assert r4["rows"] != r2["rows"], (r2["rows"], r4["rows"])          # change was caught
    assert r4["rows"][0][2] == r2["rows"][0][2] + 1000.0, (r2["rows"], r4["rows"])
    assert r6["rows"] == r4["rows"], (r4["rows"], r6["rows"])          # unrelated write ignored
    assert st["stale_count"] == 0, st
    assert all(r["as_of_lsn"] for r in (r1, r2, r4, r6))               # LSN surfaced
    print("PASS  sources =", sources, " stale_count =", st["stale_count"],
          " hit_rate =", st["hit_rate"])


def test_demo_transitions_through_mcp():
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
