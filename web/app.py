"""
agent-mesh web — explorador gráfico del knowledge graph compartido.

UI de consulta (solo lectura): búsqueda semántica + grafo navegable.
No escribe en la KB. El servicio agent-mesh sigue siendo la autoridad única.

Arranque:
    uvicorn app:app --host 0.0.0.0 --port ${MESH_WEB_PORT:-8096}

Variables de entorno:
    KB_BASE       URL del servicio agent-mesh (def. http://127.0.0.1:8084)
    KB_TOKEN      token Bearer (si falta, se intenta leer del .env del repo)
    MESH_WEB_PORT puerto de escucha (solo documental)
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
REPO = BASE.parent

KB_BASE = os.environ.get("KB_BASE", "http://127.0.0.1:8084").rstrip("/")
TIMEOUT = float(os.environ.get("MESH_WEB_TIMEOUT", "60"))


def _token() -> str:
    tok = os.environ.get("KB_TOKEN", "").strip()
    if tok:
        return tok
    env_file = REPO / ".env"
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith("KB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


TOKEN = _token()
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

app = FastAPI(title="agent-mesh web", version="0.1.0", docs_url="/api/docs")


def _client() -> httpx.AsyncClient:
    # Un cliente por petición: httpx.AsyncClient no es obligatorio reutilizarlo
    # y así evitamos estados raros tras reinicios del servicio KB.
    return httpx.AsyncClient(base_url=KB_BASE, headers=HEADERS, timeout=TIMEOUT)


def _upstream_error(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        detail = exc.response.text[:400]
        return HTTPException(exc.response.status_code, f"KB respondió {exc.response.status_code}: {detail}")
    return HTTPException(502, f"KB no disponible: {exc}")


async def _get(path: str, **params):
    async with _client() as c:
        try:
            r = await c.get(path, params=params or None)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _upstream_error(exc)


async def _post(path: str, **params):
    async with _client() as c:
        try:
            r = await c.post(path, params=params or None)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _upstream_error(exc)


# ── proxy de solo lectura ────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    try:
        data = await _get("/health")
    except HTTPException as exc:
        return {"status": "down", "error": exc.detail, "kb_base": KB_BASE}
    data["kb_base"] = KB_BASE
    return data


@app.get("/api/schema")
async def schema():
    return await _get("/schema")


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1),
    mode: str = Query("semantic", pattern="^(semantic|fts)$"),
    limit: int = Query(12, ge=1, le=100),
):
    """mode=semantic -> /similar (embeddings); mode=fts -> /search (full-text)."""
    if mode == "fts":
        data = await _get("/search", q=q, limit=limit)
        return {"query": q, "mode": "fts", "results": data.get("results", [])}

    async with _client() as c:
        try:
            r = await c.post("/similar", params={"limit": limit}, json={"text": q})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _upstream_error(exc)
    return {"query": q, "mode": "semantic", "results": data.get("results", [])}


@app.get("/api/expand")
async def expand(
    node: int = Query(..., ge=1),
    hops: int = Query(1, ge=1, le=5),
    limit: int = Query(40, ge=1, le=200),
):
    """Vecinos de un nodo, con tipos de relación ya agregados por el servicio."""
    data = await _post("/graph", node=node, hops=hops, limit=limit)
    center = data.get("node")
    # El servicio devuelve el id del centro; para el nombre lo resolvemos aparte
    # si el frontend no lo tiene todavía.
    return {
        "center": center,
        "neighbors": data.get("neighbors", []),
    }


@app.get("/api/subgraph")
async def subgraph(
    nodes: str = Query(..., description="ids separados por coma"),
    hops: int = Query(1, ge=1, le=2),
    limit: int = Query(25, ge=1, le=100),
):
    """Subgrafo agregado: une los vecinos de varios nodos (para 'graficar resultados')."""
    try:
        ids = [int(x) for x in nodes.split(",") if x.strip().isdigit()]
    except ValueError:
        raise HTTPException(400, "lista de ids inválida")
    ids = ids[:20]
    if not ids:
        raise HTTPException(400, "sin ids válidos")

    out_nodes: dict[int, dict] = {}
    out_edges: dict[tuple[int, int, str], dict] = {}

    async with _client() as c:
        for nid in ids:
            try:
                r = await c.post("/graph", params={"node": nid, "hops": hops, "limit": limit})
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise _upstream_error(exc)

            for nb in data.get("neighbors", []):
                out_nodes[nb["id"]] = {"id": nb["id"], "label": nb["label"], "name": nb["name"]}
                for rel in nb.get("relations") or ["RELACIONADO_CON"]:
                    key = (min(nid, nb["id"]), max(nid, nb["id"]), rel)
                    out_edges[key] = {"source": nid, "target": nb["id"], "type": rel}

    return {
        "roots": ids,
        "nodes": list(out_nodes.values()),
        "edges": list(out_edges.values()),
    }


# ── búsqueda de nodos (por nombre / contenido) ───────────────────────────


def _connect():
    dsn = _dsn()
    if not dsn:
        raise HTTPException(503, "sin KB_DSN: solo disponible el proxy al servicio")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise HTTPException(503, "psycopg no instalado")
    try:
        return psycopg.connect(dsn, connect_timeout=8), dict_row
    except Exception as exc:
        raise HTTPException(502, f"no se pudo conectar a la KB: {exc}")


@app.get("/api/nodes")
async def search_nodes(
    q: str = Query("", max_length=200),
    label: str = Query("", max_length=60),
    mode: str = Query("name", pattern="^(name|content|any)$"),
    limit: int = Query(30, ge=1, le=200),
):
    """Busca nodos por nombre (mode=name), por el texto del contenido
    (mode=content) o ambos (mode=any). Ordena por relevancia de nombre y luego
    por grado de conexión, que es lo que separa el nodo bueno del homónimo.
    """
    q = q.strip()
    label = label.strip()
    if not q and not label:
        raise HTTPException(400, "indica al menos q o label")

    patterns = []
    args: list = []

    if q:
        pat = f"%{q}%"
        if mode == "name":
            patterns.append("(n.name ILIKE %s)")
            args.append(pat)
        elif mode == "content":
            patterns.append("(n.text ILIKE %s)")
            args.append(pat)
        else:
            patterns.append("(n.name ILIKE %s OR n.text ILIKE %s)")
            args.extend([pat, pat])
    if label:
        patterns.append("(n.label = %s)")
        args.append(label)

    where_sql = " AND ".join(patterns) or "TRUE"
    # ranking: nombre exacto (3) > empieza por (2) > contiene (1)
    rank_args = [q.lower(), q.lower() + "%"] if q else []
    rank_sql = """
      CASE WHEN LOWER(n.name) = %s THEN 3
           WHEN LOWER(n.name) LIKE %s THEN 2
           ELSE 1 END
    """ if q else "1"

    sql = f"""
        SELECT n.id, n.label, n.name, n.updated_at, n.version,
               LEFT(COALESCE(n.text, ''), 400) AS excerpt,
               LENGTH(COALESCE(n.text, '')) AS text_len,
               (SELECT COUNT(*) FROM edges e
                 WHERE e.source = n.id OR e.target = n.id) AS degree,
               {rank_sql} AS rank
        FROM nodes n
        WHERE {where_sql}
        ORDER BY rank DESC, degree DESC, n.name
        LIMIT %s
    """
    full_args = list(rank_args) + list(args) + [limit]

    conn, dict_row = _connect()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, full_args)
            rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(500, f"error de consulta: {exc}")
    finally:
        conn.close()

    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
    return {"query": q, "label": label, "mode": mode, "total": len(rows), "results": rows}


@app.get("/api/ego")
async def ego_graph(
    id: int = Query(..., ge=1),
    hops: int = Query(1, ge=1, le=3),
    limit: int = Query(120, ge=1, le=400),
):
    """Ego-grafo con aristas REALES.

    A diferencia de /api/expand (que solo dice qué nodos son vecinos y con qué
    tipos), aquí se devuelven las aristas concretas, incluidas las que existen
    entre los propios vecinos. Es lo que hace legible el vecindario.
    """
    conn, dict_row = _connect()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, label, name FROM nodes WHERE id = %s", (id,))
            center = cur.fetchone()
            if not center:
                raise HTTPException(404, f"nodo {id} no existe")

            walk = """
                WITH RECURSIVE walk(hop, id, path) AS (
                  SELECT 0, %s::bigint, ARRAY[%s::bigint]
                  UNION ALL
                  SELECT w.hop + 1,
                         CASE WHEN e.source = w.id THEN e.target ELSE e.source END,
                         w.path || (CASE WHEN e.source = w.id THEN e.target
                                         ELSE e.source END)
                  FROM walk w
                  JOIN edges e ON e.source = w.id OR e.target = w.id
                  WHERE w.hop < %s
                    AND NOT (CASE WHEN e.source = w.id THEN e.target
                                  ELSE e.source END) = ANY(w.path)
                ), picked AS (
                  SELECT DISTINCT id FROM walk LIMIT %s
                )
            """
            cur.execute(
                walk + """
                SELECT n.id, n.label, n.name,
                       (SELECT COUNT(*) FROM edges e2
                         WHERE e2.source = n.id OR e2.target = n.id) AS degree
                FROM nodes n JOIN picked p ON p.id = n.id
                """,
                (id, id, hops, limit))
            nodes = cur.fetchall()

            cur.execute(
                walk + """
                SELECT e.id, e.type, e.source, e.target
                FROM edges e
                JOIN picked a ON a.id = e.source
                JOIN picked b ON b.id = e.target
                WHERE e.source <> e.target
                """,
                (id, id, hops, limit))
            edges = cur.fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"error de consulta: {exc}")
    finally:
        conn.close()

    return {
        "center": center,
        "hops": hops,
        "nodes": nodes,
        "edges": edges,
        "truncated": len(nodes) >= limit,
    }


@app.get("/api/node")
async def node_detail(id: int = Query(..., ge=1), name: str = Query("")):
    """Detalle de un nodo por id.

    El servicio no expone GET /n/{label}/{name} para lectura, así que se lee
    directamente de la BD (solo lectura, misma máquina) y se cae a /search si
    el driver no está disponible.
    """
    row = _node_from_db(id)
    if row is not None:
        return row

    if not name:
        raise HTTPException(404, "nodo no encontrado")
    data = await _get("/search", q=name, limit=50)
    for item in data.get("results", []):
        if item.get("id") == id:
            return item
    results = data.get("results", [])
    return results[0] if results else {"id": id, "name": name, "label": "", "text": "", "properties": {}}


def _dsn() -> str:
    dsn = os.environ.get("KB_DSN", "").strip()
    if dsn:
        return dsn
    try:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("KB_DSN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _node_from_db(node_id: int):
    dsn = _dsn()
    if not dsn:
        return None
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id, label, name, text, properties, version, updated_at"
                    " FROM nodes WHERE id = %s",
                    (node_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cur.execute(
                    "SELECT e.type, e.source, e.target,"
                    "  s.label AS s_label, s.name AS s_name,"
                    "  t.label AS t_label, t.name AS t_name"
                    " FROM edges e"
                    " JOIN nodes s ON s.id = e.source"
                    " JOIN nodes t ON t.id = e.target"
                    " WHERE e.source = %s OR e.target = %s"
                    " ORDER BY e.type, t.name"
                    " LIMIT 200",
                    (node_id, node_id),
                )
                rels = []
                for e in cur.fetchall():
                    outgoing = e["source"] == node_id
                    other_id = e["target"] if outgoing else e["source"]
                    other_label = e["t_label"] if outgoing else e["s_label"]
                    other_name = e["t_name"] if outgoing else e["s_name"]
                    rels.append({
                        "type": e["type"],
                        "direction": "out" if outgoing else "in",
                        "id": other_id,
                        "label": other_label,
                        "name": other_name,
                    })
    except Exception:
        return None

    row["updated_at"] = row["updated_at"].isoformat() if row.get("updated_at") else None
    if isinstance(row.get("properties"), str):
        try:
            import json as _json
            row["properties"] = _json.loads(row["properties"])
        except ValueError:
            pass
    row["relations"] = rels
    return row


# ── estáticos ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")
