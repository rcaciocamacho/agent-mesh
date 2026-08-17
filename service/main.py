"""
agent-mesh — servicio HTTP del knowledge graph compartido.

Autoridad única de escritura. Los agentes (Hermes local/rack, Mako, JARVIS)
leen y escriben aquí; todos comparten la misma BD.

Diseño: docs/specs/agent-mesh-design.md
MVP: FTS5 (búsqueda texto). Embeddings/vectores → fase 2.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from store import Store

DB_PATH = os.environ.get("KB_DSN", "postgresql://kb:kb@localhost:5432/agentmesh")
KB_TOKEN = os.environ.get("KB_TOKEN", "")          # "" = auth desactivada
REDIS_URL = os.environ.get("KB_REDIS_URL", "")     # "" = sin notificaciones
KB_CHANNEL = os.environ.get("KB_CHANNEL", "kb:changes")

_store: Store | None = None

def _redis_pub(msg: str):
    """Avisa a los agentes por Redis (best-effort; no debe tumbar el write)."""
    if not REDIS_URL:
        return
    try:
        import redis
        r = redis.Redis.from_url(REDIS_URL)
        r.publish(KB_CHANNEL, msg)
        r.close()
    except Exception as e:
        print(f"[kb] ⚠️ redis pub falló: {e}")

def _check_auth(authorization: str | None):
    if KB_TOKEN:
        if not authorization or authorization != f"Bearer {KB_TOKEN}":
            raise HTTPException(401, "token inválido o ausente")

class NodeIn(BaseModel):
    label: str
    name: str
    text: str = ""
    properties: dict | None = None

class EdgeIn(BaseModel):
    type: str
    source: int
    target: int
    properties: dict | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store
    _store = Store(DB_PATH)
    yield
    _store.close()

app = FastAPI(title="agent-mesh", version="0.1.0", lifespan=lifespan)

def store() -> Store:
    if _store is None:
        raise HTTPException(503, "store no inicializado")
    return _store

@app.get("/health")
def health():
    try:
        c = store().counts()
        return {"status": "ok", **c}
    except Exception as e:
        raise HTTPException(503, str(e))

@app.get("/schema")
def schema(authorization: str | None = Header(None)):
    _check_auth(authorization)
    return store().schema()

@app.put("/n/{label}/{name}")
def put_node(label: str, name: str, body: NodeIn, authorization: str | None = Header(None)):
    _check_auth(authorization)
    nid = store().upsert_node(body.label or label, body.name or name,
                              body.text, body.properties)
    _redis_pub(json_change("node", label, name))
    return {"id": nid, "label": label, "name": name}

@app.delete("/n/{label}/{name}")
def del_node(label: str, name: str, authorization: str | None = Header(None)):
    _check_auth(authorization)
    ok = store().delete_node(label, name)
    if ok:
        _redis_pub(json_change("node_delete", label, name))
    return {"deleted": ok}

@app.put("/e/{etype}")
def put_edge(etype: str, body: EdgeIn, authorization: str | None = Header(None)):
    _check_auth(authorization)
    eid = store().upsert_edge(body.type or etype,
                              body.source, body.target, body.properties)
    _redis_pub(json_change("edge", body.type or etype, f"{body.source}->{body.target}"))
    return {"id": eid, "type": body.type or etype}

@app.get("/search")
def search(q: str = Query(...), limit: int = Query(10, ge=1, le=100),
           authorization: str | None = Header(None)):
    _check_auth(authorization)
    return {"query": q, "results": store().search(q, limit)}

@app.post("/graph")
def graph(node: int, hops: int = Query(1, ge=1, le=5), limit: int = Query(50, le=200),
          authorization: str | None = Header(None)):
    _check_auth(authorization)
    return store().neighbors(node, hops, limit)

def json_change(kind: str, label: str, name: str) -> str:
    import json
    return json.dumps({"kind": kind, "label": label, "name": name})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("KB_PORT", "8084")))
