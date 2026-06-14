"""
Cartograph control-plane API — a thin HTTP layer over the Cartograph core so the
dashboard (and any client) can read live state. No caching logic here.

Run:  cartograph-api      (or: python -m cartograph.api)
Env:  CARTOGRAPH_DSN / _MODE / _SLOT, CARTOGRAPH_API_HOST / _PORT
"""

import os
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .core import Cartograph

app = FastAPI(title="Cartograph control plane", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_cg = None
_lock = threading.Lock()      # the core's psycopg2 connections are single-threaded


def cg():
    global _cg
    if _cg is None:
        _cg = Cartograph(
            dsn=os.environ.get("CARTOGRAPH_DSN"),
            mode=os.environ.get("CARTOGRAPH_MODE"),
            slot=os.environ.get("CARTOGRAPH_SLOT"),
        )
    return _cg


class SQL(BaseModel):
    sql: str


@app.get("/health")
def health():
    return {"ok": True, "mode": cg().mode, "dsn": cg().dsn}


@app.get("/stats")
def stats():
    with _lock:
        return cg().stats()


@app.get("/schema")
def schema():
    with _lock:
        return {"tables": cg().schema_map()}


@app.get("/cached")
def cached():
    with _lock:
        return {"queries": cg().cached_queries()}


@app.get("/feed")
def feed():
    with _lock:
        return {"events": cg().events()}


@app.post("/query")
def query(body: SQL):
    with _lock:
        return cg().query(body.sql).to_dict()


@app.post("/explain")
def explain(body: SQL):
    with _lock:
        return cg().explain(body.sql)


def run():
    import uvicorn
    uvicorn.run(app,
                host=os.environ.get("CARTOGRAPH_API_HOST", "127.0.0.1"),
                port=int(os.environ.get("CARTOGRAPH_API_PORT", "8000")))


if __name__ == "__main__":
    run()
