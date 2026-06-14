"""
Cartograph MCP server -- a THIN pass-through that exposes the Cartograph public
API (cartograph.core.Cartograph) to an AI agent as MCP tools. There is NO caching,
extraction, or capture logic here; every tool just calls the M1 core and returns
its result, surfacing `source` / `as_of_lsn` so correctness is observable.

Tools:
  query(sql)   -> {rows, source: "cache"|"live", footprint_mode, as_of_lsn}
  explain(sql) -> {cacheable, refusal_reason?, footprint}   (no execution)
  stats()      -> {hit_rate, refusals_by_reason, stale_count (==0), ...}

Config via env:
  CARTOGRAPH_DSN    libpq DSN (default: host=/tmp/pgsock port=55432 dbname=postgres user=postgres)
  CARTOGRAPH_MODE   footprint mode: "coarse" (default, CTE-safe) | "column" | "lineage"
  CARTOGRAPH_SLOT   logical replication slot name (default: "cg")

Setup note (same trade-off as M1): the server attaches to an existing logical
replication slot (plugin=test_decoding) on a `wal_level=logical` Postgres.
`REPLICA IDENTITY FULL` on cached tables buys per-column update precision (more
WAL); without it the core falls back to coarser, still-sound invalidation.
Run `python -m cartograph.bootstrap_demo` (or Cartograph.bootstrap()) to create
the demo schema + slot + DDL trigger.
"""

import os
import sys
import json
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .core import Cartograph

_CG = None


def get_cartograph():
    """Lazily build the single Cartograph instance from env."""
    global _CG
    if _CG is None:
        _CG = Cartograph(
            dsn=os.environ.get("CARTOGRAPH_DSN"),
            mode=os.environ.get("CARTOGRAPH_MODE"),
            slot=os.environ.get("CARTOGRAPH_SLOT"),
        )
    return _CG


server = Server("cartograph")

TOOLS = [
    Tool(
        name="query",
        description=("Run a read-only SQL query through Cartograph's never-stale "
                     "cache. Returns rows plus `source` ('cache' or 'live') and "
                     "`as_of_lsn` so you can see whether the DB was actually hit. "
                     "Cached answers are guaranteed equal to a live run at as_of_lsn."),
        inputSchema={"type": "object",
                     "properties": {"sql": {"type": "string"}},
                     "required": ["sql"]},
    ),
    Tool(
        name="explain",
        description=("Dry-run: report whether a SQL query is cacheable, at what "
                     "footprint precision (row/column/coarse), or the refusal "
                     "reason. Does NOT execute the query."),
        inputSchema={"type": "object",
                     "properties": {"sql": {"type": "string"}},
                     "required": ["sql"]},
    ),
    Tool(
        name="stats",
        description=("Cache statistics: hit rate, refusals by reason, and the "
                     "stale-hit count (0 by construction)."),
        inputSchema={"type": "object", "properties": {}},
    ),
]


@server.list_tools()
async def list_tools():
    return TOOLS


async def dispatch(name: str, arguments: dict):
    """The tool logic the server registers. Exposed as a function so tests can
    drive the exact same handlers. Runs the (blocking) core off the event loop."""
    cg = get_cartograph()
    if name == "query":
        res = await asyncio.to_thread(cg.query, arguments["sql"])
        return res.to_dict()
    if name == "explain":
        return await asyncio.to_thread(cg.explain, arguments["sql"])
    if name == "stats":
        return await asyncio.to_thread(cg.stats)
    raise ValueError(f"unknown tool: {name}")


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    result = await dispatch(name, arguments or {})
    return [TextContent(type="text", text=json.dumps(result, default=str))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run():
    """Sync entry point (console_scripts: cartograph-mcp)."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
