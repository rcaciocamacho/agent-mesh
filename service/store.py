"""
agent-mesh — store PostgreSQL (grafo + FTS) para el knowledge graph compartido.

PostgreSQL en TrueNAS (nativo de red, gestiona la concurrencia). El servicio HTTP
es la autoridad única de escritura; los agentes solo hablan con la API.

MVP:
  - nodes / edges con properties jsonb
  - full-text: índice GIN sobre to_tsvector('spanish', ...)
  - recorrido de grafo con WITH RECURSIVE (vecinos / N saltos)
Fase 2: pgvector para similaridad semántica.

Config: KB_DSN (postgres://...) — ver .env.example.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS nodes(
      id          bigserial PRIMARY KEY,
      label       text NOT NULL,
      name        text NOT NULL,
      text        text NOT NULL DEFAULT '',
      properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
      updated_at  timestamptz NOT NULL DEFAULT now(),
      UNIQUE(label, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edges(
      id          bigserial PRIMARY KEY,
      type        text NOT NULL,
      source      bigint NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      target      bigint NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at  timestamptz NOT NULL DEFAULT now(),
      UNIQUE(type, source, target)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source)",
    "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target)",
    """
    CREATE INDEX IF NOT EXISTS idx_nodes_fts ON nodes
      USING GIN (to_tsvector('spanish',
        coalesce(label,'')||' '||coalesce(name,'')||' '||coalesce(text,'')))
    """,
]

def _fts_expr() -> str:
    return ("to_tsvector('spanish', "
            "coalesce(label,'')||' '||coalesce(name,'')||' '||coalesce(text,''))")

class Store:
    """Graph + full-text store sobre PostgreSQL (pool de conexiones)."""

    def __init__(self, dsn: str):
        self.pool = ConnectionPool(dsn, min_size=1, max_size=8, open=False)
        self.pool.open()
        with self.pool.connection() as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            conn.commit()

    @contextmanager
    def _c(self):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            yield cur
            conn.commit()

    # ── nodos ──────────────────────────────────────────────────────────────
    def upsert_node(self, label: str, name: str, text: str = "",
                    properties: dict | None = None) -> int:
        props = json.dumps(properties or {}, ensure_ascii=False)
        with self._c() as cur:
            cur.execute(
                "INSERT INTO nodes(label, name, text, properties, updated_at)"
                " VALUES (%s,%s,%s,%s::jsonb, now())"
                " ON CONFLICT (label, name) DO UPDATE SET"
                "   text=EXCLUDED.text, properties=EXCLUDED.properties, updated_at=now()"
                " RETURNING id",
                (label, name, text, props))
            return cur.fetchone()["id"]

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

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _reindex_query(q: str) -> str:
        """plainto_tsquery maneja el query; solo normalizamos espacios."""
        return " ".join(q.split())

    @staticmethod
    def _node(row: dict) -> dict:
        props = row.get("properties") or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                props = {}
        return {"id": row["id"], "label": row["label"], "name": row["name"],
                "text": row.get("text", ""), "properties": props}

    def close(self):
        self.pool.close()
