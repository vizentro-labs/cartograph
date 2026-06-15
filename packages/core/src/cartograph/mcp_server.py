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
    """Sync entry point (console_scripts: cartograph-mcp). Stdio transport."""
    asyncio.run(main())


def http_app(sse_path="/sse", message_path="/messages/"):
    """Build a Starlette ASGI app serving this MCP server over HTTP (SSE transport),
    so an agent can reach it at a URL instead of spawning a local stdio process.

    Intentionally NO auth or multi-tenant routing here — that belongs in the
    managed control plane. Self-host this behind your own gateway, or bind it to
    localhost. The exposed tools (query/explain/stats) are the same as stdio."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route, Mount

    sse = SseServerTransport(message_path)

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as (r, w):
            await server.run(r, w, server.create_initialization_options())
        return Response()        # SSE stream already sent; satisfy Starlette's Route

    return Starlette(routes=[
        Route(sse_path, endpoint=handle_sse),
        Mount(message_path, app=sse.handle_post_message),
    ])


def run_http():
    """Console entry (cartograph-mcp-http): serve the MCP tools over HTTP/SSE.
    Env: CARTOGRAPH_MCP_HOST (default 127.0.0.1), CARTOGRAPH_MCP_PORT (default 8765)."""
    try:
        import uvicorn
    except ImportError:
        sys.exit("cartograph-mcp-http needs uvicorn — install with: pip install 'cartograph-cache[api]'")
    host = os.environ.get("CARTOGRAPH_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("CARTOGRAPH_MCP_PORT", "8765"))
    print(f"Cartograph MCP (SSE) on http://{host}:{port}/sse", file=sys.stderr)
    uvicorn.run(http_app(), host=host, port=port)


if __name__ == "__main__":
    run()
