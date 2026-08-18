"""
agent-mesh — store PostgreSQL (grafo + FTS + vectores) para el knowledge graph.

PostgreSQL en TrueNAS (nativo de red, gestiona la concurrencia). El servicio HTTP
es la autoridad única de escritura; los agentes solo hablan con la API.

Spec 02 (mejoras):
  F0  migraciones versionadas → service/migrations.py
  F1  pgvector HNSW: similar() usa `<=>` en SQL cuando la extensión está
      activa; si no, cae al coseno Python (fail-soft, comportamiento previo).
  F2  TTL: expires_at por nodo + cleanup_expired(); conversaciones con resumen.
  F3  tokens por agente + audit_log.
  F5  optimistic locking: version + if_match (409 si desactualizado).

Config: KB_DSN (postgres://...) — ver .env.example.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from embeddings import embed, cosine
from migrations import apply_migrations, pgvector_active

def _fts_expr() -> str:
    return ("to_tsvector('spanish', "
            "coalesce(label,'')||' '||coalesce(name,'')||' '||coalesce(text,''))")

class ConflictError(Exception):
    """Versión desactualizada en escritura (optimistic locking, HTTP 409)."""

class Store:
    """Graph + full-text + vectores sobre PostgreSQL (pool de conexiones)."""

    def __init__(self, dsn: str):
        # row_factory=dict_row desde el pool: todas las conexiones (incluidas
        # las de apply_migrations) devuelven dicts con claves por nombre.
        self.pool = ConnectionPool(dsn, min_size=1, max_size=8, open=False,
                                   kwargs={"row_factory": dict_row})
        self.pool.open()
        with self.pool.connection() as conn:
            # F0: aplicar migraciones versionadas (idempotente)
            apply_migrations(self.pool)
            self.pgvector = pgvector_active(self.pool)
        print(f"[kb] pgvector activo: {self.pgvector}")

    @contextmanager
    def _c(self):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            yield cur
            conn.commit()

    # ── nodos ──────────────────────────────────────────────────────────────
    def upsert_node(self, label: str, name: str, text: str = "",
                    properties: dict | None = None, ttl_seconds: int | None = None,
                    if_match: int | None = None) -> dict:
        """Upsert nodo. ttl_seconds>0 → expires_at = now()+ttl (nodo efímero).

        if_match: optimistic locking — si la versión actual no coincide,
        lanza ConflictError (HTTP 409) en vez de pisar la escritura.
        Devuelve {"id", "label", "name", "version"}.
        """
        props = json.dumps(properties or {}, ensure_ascii=False)
        emb = json.dumps(embed(f"{label} {name} {text}"))
        vec_cols = ", embedding_vec" if self.pgvector else ""
        vec_vals = ", %s::vector" if self.pgvector else ""
        vec_set = (", embedding_vec=EXCLUDED.embedding_vec"
                   if self.pgvector else "")
        with self._c() as cur:
            if if_match is not None:
                cur.execute("SELECT version FROM nodes WHERE label=%s AND name=%s",
                            (label, name))
                row = cur.fetchone()
                if row is None:
                    # no existe: crear es seguro (nadie lo ha pisado aún)
                    pass
                elif row["version"] != if_match:
                    raise ConflictError(
                        f"versión {if_match} obsoleta (actual {row['version']})")
            params = [label, name, text, props, emb]
            if self.pgvector:
                params.append(emb)
            if ttl_seconds:
                params.append(ttl_seconds)
                cur.execute(
                    f"INSERT INTO nodes(label, name, text, properties, embedding"
                    f"{vec_cols}, updated_at, expires_at)"
                    f" VALUES (%s,%s,%s,%s::jsonb,%s::jsonb{vec_vals}, now(),"
                    f" now() + make_interval(secs => %s))"
                    f" ON CONFLICT (label, name) DO UPDATE SET"
                    f"   text=EXCLUDED.text, properties=EXCLUDED.properties,"
                    f"   embedding=EXCLUDED.embedding{vec_set},"
                    f"   updated_at=now(), expires_at=EXCLUDED.expires_at,"
                    f"   version=nodes.version+1"
                    f" RETURNING id, version",
                    params)
            else:
                cur.execute(
                    f"INSERT INTO nodes(label, name, text, properties, embedding"
                    f"{vec_cols}, updated_at, expires_at)"
                    f" VALUES (%s,%s,%s,%s::jsonb,%s::jsonb{vec_vals}, now(), NULL)"
                    f" ON CONFLICT (label, name) DO UPDATE SET"
                    f"   text=EXCLUDED.text, properties=EXCLUDED.properties,"
                    f"   embedding=EXCLUDED.embedding{vec_set},"
                    f"   updated_at=now(), expires_at=NULL,"
                    f"   version=nodes.version+1"
                    f" RETURNING id, version",
                    params)
            row = cur.fetchone()
        return {"id": row["id"], "label": label, "name": name,
                "version": row["version"]}

    def get_node(self, label: str, name: str) -> dict | None:
        with self._c() as cur:
            cur.execute("SELECT * FROM nodes WHERE label=%s AND name=%s", (label, name))
            row = cur.fetchone()
        return self._node(row) if row else None

    def delete_node(self, label: str, name: str) -> bool:
        with self._c() as cur:
            cur.execute("DELETE FROM nodes WHERE label=%s AND name=%s", (label, name))
            return cur.rowcount > 0

    # ── edges ──────────────────────────────────────────────────────────────
    def upsert_edge(self, etype: str, source: int, target: int,
                    properties: dict | None = None) -> int:
        props = json.dumps(properties or {}, ensure_ascii=False)
        with self._c() as cur:
            cur.execute(
                "INSERT INTO edges(type, source, target, properties, created_at)"
                " VALUES (%s,%s,%s,%s::jsonb, now())"
                " ON CONFLICT (type, source, target) DO UPDATE"
                "   SET properties=EXCLUDED.properties"
                " RETURNING id",
                (etype, source, target, props))
            return cur.fetchone()["id"]

    def delete_edge(self, etype: str, source: int, target: int) -> bool:
        with self._c() as cur:
            cur.execute("DELETE FROM edges WHERE type=%s AND source=%s AND target=%s",
                        (etype, source, target))
            return cur.rowcount > 0

    # ── TTL (F2) ───────────────────────────────────────────────────────────
    def cleanup_expired(self) -> int:
        """Borra nodos con expires_at vencido (los edges caen por CASCADE)."""
        with self._c() as cur:
            cur.execute("DELETE FROM nodes WHERE expires_at IS NOT NULL"
                        " AND expires_at < now()")
            return cur.rowcount

    # ── conversaciones (F2) ────────────────────────────────────────────────
    def upsert_conversation(self, session_id: str, resumen: str,
                            lang: str = "es", meta: dict | None = None) -> int:
        m = json.dumps(meta or {}, ensure_ascii=False)
        with self._c() as cur:
            cur.execute(
                "INSERT INTO conversations(session_id, resumen, lang, meta, updated_at)"
                " VALUES (%s,%s,%s,%s::jsonb, now())"
                " ON CONFLICT (session_id) DO UPDATE SET"
                "   resumen=EXCLUDED.resumen, lang=EXCLUDED.lang,"
                "   meta=EXCLUDED.meta, updated_at=now()"
                " RETURNING id",
                (session_id, resumen, lang, m))
            return cur.fetchone()["id"]

    def get_conversation(self, session_id: str) -> dict | None:
        with self._c() as cur:
            cur.execute("SELECT * FROM conversations WHERE session_id=%s",
                        (session_id,))
            row = cur.fetchone()
        if not row:
            return None
        return {"id": row["id"], "session_id": row["session_id"],
                "resumen": row["resumen"], "lang": row["lang"],
                "meta": row["meta"] if isinstance(row["meta"], dict) else
                        json.loads(row["meta"] or "{}"),
                "updated_at": str(row["updated_at"])}

    def delete_conversation(self, session_id: str) -> bool:
        with self._c() as cur:
            cur.execute("DELETE FROM conversations WHERE session_id=%s",
                        (session_id,))
            return cur.rowcount > 0

    # ── tokens por agente + audit (F3) ─────────────────────────────────────
    def register_agent(self, token: str, agent: str) -> None:
        import hashlib
        th = hashlib.sha256(token.encode()).hexdigest()
        with self._c() as cur:
            cur.execute(
                "INSERT INTO agents(token_hash, agent) VALUES (%s,%s)"
                " ON CONFLICT (agent) DO UPDATE SET token_hash=EXCLUDED.token_hash",
                (th, agent))

    def agent_for_token(self, token: str) -> str | None:
        import hashlib
        th = hashlib.sha256(token.encode()).hexdigest()
        with self._c() as cur:
            cur.execute("SELECT agent FROM agents WHERE token_hash=%s", (th,))
            row = cur.fetchone()
        return row["agent"] if row else None

    def audit(self, agent: str, action: str, detail: dict | None = None) -> None:
        d = json.dumps(detail or {}, ensure_ascii=False)
        with self._c() as cur:
            cur.execute(
                "INSERT INTO audit_log(agent, action, detail) VALUES (%s,%s,%s::jsonb)",
                (agent, action, d))

    # ── búsqueda / recorrido ───────────────────────────────────────────────
    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = self._reindex_query(query)
        sql = (f"SELECT id, label, name, text, properties,"
               f" ts_rank({_fts_expr()}, plainto_tsquery('spanish', %s)) AS rank"
               f" FROM nodes WHERE {_fts_expr()} @@ plainto_tsquery('spanish', %s)"
               f" ORDER BY rank DESC, updated_at DESC LIMIT %s")
        with self._c() as cur:
            cur.execute(sql, (q, q, limit))
            return [self._node(r) for r in cur.fetchall()]

    def neighbors(self, node_id: int, hops: int = 1, limit: int = 50) -> dict:
        sql = """
        WITH RECURSIVE walk(hop, id, path) AS (
          SELECT 0, %s::bigint, ARRAY[%s::bigint]
          UNION ALL
          SELECT w.hop+1,
                 (CASE WHEN e.source=w.id THEN e.target ELSE e.source END),
                 w.path || (CASE WHEN e.source=w.id THEN e.target ELSE e.source END)
          FROM walk w
          JOIN edges e ON e.source=w.id OR e.target=w.id
          WHERE w.hop < %s
            AND NOT (CASE WHEN e.source=w.id THEN e.target ELSE e.source END) = ANY(w.path)
        ),
        rels AS (
          SELECT n.id, n.label, n.name,
                 coalesce(array_agg(DISTINCT e.type)
                          FILTER (WHERE e.type IS NOT NULL), '{}') AS rtypes
          FROM walk w
          JOIN nodes n ON n.id = w.id
          LEFT JOIN edges e ON e.source = w.id OR e.target = w.id
          WHERE w.id <> %s
          GROUP BY n.id, n.label, n.name
        )
        SELECT id, label, name, rtypes FROM rels ORDER BY id LIMIT %s
        """
        with self._c() as cur:
            cur.execute(sql, (node_id, node_id, hops, node_id, limit))
            rows = cur.fetchall()
        return {"node": node_id, "neighbors": [
            {"id": r["id"], "label": r["label"], "name": r["name"],
             "relations": list(r["rtypes"])} for r in rows]}

    def schema(self) -> dict:
        with self._c() as cur:
            cur.execute("SELECT DISTINCT label FROM nodes ORDER BY label")
            labels = [r["label"] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT type FROM edges ORDER BY type")
            etypes = [r["type"] for r in cur.fetchall()]
        return {"labels": labels, "edge_types": etypes}

    def counts(self) -> dict:
        with self._c() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM nodes")
            n = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM edges")
            e = cur.fetchone()["c"]
        return {"nodes": n, "edges": e}

    # ── similaridad semántica (F1: pgvector si está, coseno Python si no) ──
    def backfill_embeddings(self) -> int:
        """Recalcula embeddings para nodos que aún no los tienen (idempotente).

        Con pgvector activo rellena `embedding_vec` (los nodos pre-pgvector solo
        tienen el jsonb); sin pgvector rellena el jsonb. Cubre ambos huecos.
        """
        with self._c() as cur:
            if self.pgvector:
                cur.execute("SELECT id, label, name, text FROM nodes"
                            " WHERE embedding_vec IS NULL")
            else:
                cur.execute("SELECT id, label, name, text FROM nodes"
                            " WHERE embedding IS NULL")
            rows = cur.fetchall()
            for r in rows:
                emb = json.dumps(embed(f"{r['label']} {r['name']} {r['text']}"))
                if self.pgvector:
                    cur.execute(
                        "UPDATE nodes SET embedding=%s::jsonb, embedding_vec=%s::vector"
                        " WHERE id=%s", (emb, emb, r["id"]))
                else:
                    cur.execute("UPDATE nodes SET embedding=%s::jsonb WHERE id=%s",
                                (emb, r["id"]))
        return len(rows)

    def similar(self, text: str, limit: int = 10) -> list[dict]:
        """Top-N nodos más parecidos. pgvector HNSW si está activo; si no,
        coseno Python (fail-soft, comportamiento previo)."""
        q = embed(text)
        if self.pgvector:
            with self._c() as cur:
                cur.execute(
                    "SELECT id, label, name, text, properties,"
                    " 1 - (embedding_vec <=> %s::vector) AS score"
                    " FROM nodes WHERE embedding_vec IS NOT NULL"
                    " ORDER BY embedding_vec <=> %s::vector LIMIT %s",
                    (json.dumps(q), json.dumps(q), limit))
                out = []
                for r in cur.fetchall():
                    row = self._node(r)
                    row["score"] = round(float(r["score"]), 4)
                    out.append(row)
                return out
        # fallback: coseno en Python (como antes de pgvector)
        with self._c() as cur:
            cur.execute("SELECT id, label, name, text, properties, embedding"
                        " FROM nodes WHERE embedding IS NOT NULL")
            rows = cur.fetchall()
        scored = []
        for r in rows:
            ev = r["embedding"]
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    continue
            if not isinstance(ev, list) or not ev:
                continue
            scored.append((cosine(q, ev), r))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for sim, r in scored[:limit]:
            row = self._node(r)
            row["score"] = round(sim, 4)
            out.append(row)
        return out

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _reindex_query(q: str) -> str:
        return " ".join(q.split())

    @staticmethod
    def _node(row: dict) -> dict:
        props = row.get("properties") or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                props = {}
        out = {"id": row["id"], "label": row["label"], "name": row["name"],
               "text": row.get("text", ""), "properties": props}
        if "version" in row:
            out["version"] = row["version"]
        return out

    def close(self):
        self.pool.close()
