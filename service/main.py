"""
agent-mesh — servicio HTTP del knowledge graph compartido.

Autoridad única de escritura. Los agentes (Hermes local/rack, Mako, JARVIS)
leen y escriben aquí; todos comparten la misma BD.

Diseño: docs/specs/agent-mesh-design.md · Mejoras: docs/specs/spec-02-mejoras.md
F0 migraciones versionadas · F1 pgvector (fail-soft) · F2 TTL+conversaciones
F3 tokens por agente + audit · F5 optimistic locking (If-Match).
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from store import Store, ConflictError

DB_PATH = os.environ.get("KB_DSN", "postgresql://kb:***@localhost:5432/agentmesh")
KB_TOKEN = os.environ.get("KB_TOKEN", "")          # legacy: token único ("" = auth off)
KB_TOKENS = os.environ.get("KB_TOKENS", "")        # F3: JSON {"agente": "token", ...}
REDIS_URL = os.environ.get("KB_REDIS_URL", "")     # "" = sin notificaciones
KB_CHANNEL = os.environ.get("KB_CHANNEL", "kb:changes")
TTL_SWEEP_SECONDS = int(os.environ.get("KB_TTL_SWEEP", "300"))  # F2: barrendero

_store: Store | None = None

# ── Redis (avisos best-effort; nunca tumbar un write) ─────────────────────
def _redis_pub(msg: str):
    if not REDIS_URL:
        return
    try:
        import redis
        r = redis.Redis.from_url(REDIS_URL)
        # F6: además de pub/sub, XADD a un stream (los listeners tardíos no pierden nada)
        try:
            r.xadd(KB_CHANNEL + ":stream", {"msg": msg}, maxlen=1000)
        except Exception:
            pass
        r.publish(KB_CHANNEL, msg)
        r.close()
    except Exception as e:
        print(f"[kb] ⚠️ redis pub falló: {e}")

# ── auth: token único (legacy) o tokens por agente (F3) ───────────────────
_AGENTS: dict[str, str] = {}  # token -> agente

def _load_agents() -> None:
    _AGENTS.clear()
    if KB_TOKENS:
        try:
            for agent, token in json.loads(KB_TOKENS).items():
                _AGENTS[str(token)] = str(agent)
                _store.register_agent(str(token), str(agent))
            return
        except Exception as e:
            print(f"[kb] ⚠️ KB_TOKENS inválido ({e}); uso KB_TOKEN legacy")
    if KB_TOKEN:
        _AGENTS[KB_TOKEN] = "legacy"

def _check_auth(authorization: str | None) -> str | None:
    """Devuelve el agente autenticado (o None si auth desactivada)."""
    if not _AGENTS:
        return None
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "token inválido o ausente")
    token = authorization.removeprefix("Bearer ").strip()
    agent = _AGENTS.get(token)
    if agent is None:
        raise HTTPException(401, "token inválido o ausente")
    return agent

# ── modelos ────────────────────────────────────────────────────────────────
class NodeIn(BaseModel):
    label: str
    name: str
    text: str = ""
    properties: dict | None = None
    ttl_seconds: int | None = None      # F2: efímero (0/None = durable)
    if_match: int | None = None         # F5: optimistic locking (version esperada)

class EdgeIn(BaseModel):
    type: str
    source: int
    target: int
    properties: dict | None = None

class ConversationIn(BaseModel):
    resumen: str
    lang: str = "es"
    meta: dict | None = None

# ── barrendero TTL (F2) ────────────────────────────────────────────────────
async def _ttl_sweeper():
    while True:
        try:
            n = _store.cleanup_expired()
            if n:
                print(f"[kb] 🧹 barrendero TTL: {n} nodo(s) expirado(s) purgado(s)")
        except Exception as e:
            print(f"[kb] ⚠️ barrendero TTL falló: {e}")
        await asyncio.sleep(TTL_SWEEP_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store
    _store = Store(DB_PATH)
    try:
        _store.backfill_embeddings()
    except Exception as e:
        print(f"[kb] ⚠️ backfill embeddings falló: {e}")
    _load_agents()
    task = asyncio.create_task(_ttl_sweeper())
    yield
    task.cancel()
    _store.close()

app = FastAPI(title="agent-mesh", version="0.2.0", lifespan=lifespan)

def store() -> Store:
    if _store is None:
        raise HTTPException(503, "store no inicializado")
    return _store

def _audit(agent: str | None, action: str, detail: dict | None = None):
    try:
        store().audit(agent or "anonymous", action, detail)
    except Exception as e:
        print(f"[kb] ⚠️ audit falló: {e}")

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
def put_node(label: str, name: str, body: NodeIn,
             authorization: str | None = Header(None)):
    agent = _check_auth(authorization)
    try:
        res = store().upsert_node(body.label or label, body.name or name,
                                  body.text, body.properties,
                                  ttl_seconds=body.ttl_seconds,
                                  if_match=body.if_match)
    except ConflictError as e:
        raise HTTPException(409, str(e))
    _redis_pub(json_change("node", label, name))
    _audit(agent, "node.upsert", {"label": label, "name": name,
                                  "id": res["id"], "version": res["version"]})
    return res

@app.delete("/n/{label}/{name}")
def del_node(label: str, name: str, authorization: str | None = Header(None)):
    agent = _check_auth(authorization)
    ok = store().delete_node(label, name)
    if ok:
        _redis_pub(json_change("node_delete", label, name))
        _audit(agent, "node.delete", {"label": label, "name": name})
    return {"deleted": ok}

@app.put("/e/{etype}")
def put_edge(etype: str, body: EdgeIn, authorization: str | None = Header(None)):
    agent = _check_auth(authorization)
    eid = store().upsert_edge(body.type or etype,
                              body.source, body.target, body.properties)
    _redis_pub(json_change("edge", body.type or etype, f"{body.source}->{body.target}"))
    _audit(agent, "edge.upsert", {"type": body.type or etype,
                                  "source": body.source, "target": body.target,
                                  "id": eid})
    return {"id": eid, "type": body.type or etype}

# ── conversaciones (F2) ────────────────────────────────────────────────────
@app.put("/conversations/{session_id}")
def put_conversation(session_id: str, body: ConversationIn,
                     authorization: str | None = Header(None)):
    agent = _check_auth(authorization)
    cid = store().upsert_conversation(session_id, body.resumen,
                                      body.lang, body.meta)
    _audit(agent, "conversation.upsert", {"session_id": session_id, "id": cid})
    return {"id": cid, "session_id": session_id}

@app.get("/conversations/{session_id}")
def get_conversation(session_id: str, authorization: str | None = Header(None)):
    _check_auth(authorization)
    c = store().get_conversation(session_id)
    if c is None:
        raise HTTPException(404, "conversación no encontrada")
    return c

@app.delete("/conversations/{session_id}")
def del_conversation(session_id: str, authorization: str | None = Header(None)):
    agent = _check_auth(authorization)
    ok = store().delete_conversation(session_id)
    if ok:
        _audit(agent, "conversation.delete", {"session_id": session_id})
    return {"deleted": ok}

# ── búsqueda / similaridad / grafo ─────────────────────────────────────────
@app.get("/search")
def search(q: str = Query(...), limit: int = Query(10, ge=1, le=100),
           authorization: str | None = Header(None)):
    _check_auth(authorization)
    return {"query": q, "results": store().search(q, limit)}

@app.post("/similar")
def similar(body: dict, limit: int = Query(10, ge=1, le=100),
            authorization: str | None = Header(None)):
    _check_auth(authorization)
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(422, "campo 'text' requerido")
    return {"query": text, "results": store().similar(text, limit)}

@app.post("/graph")
def graph(node: int, hops: int = Query(1, ge=1, le=5), limit: int = Query(50, le=200),
          authorization: str | None = Header(None)):
    _check_auth(authorization)
    return store().neighbors(node, hops, limit)

def json_change(kind: str, label: str, name: str) -> str:
    return json.dumps({"kind": kind, "label": label, "name": name})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("KB_PORT", "8084")))
