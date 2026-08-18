"""
agent-mesh — migraciones versionadas del schema.

F0 del spec 02. Sin esto, cada mejora posterior (pgvector, TTL, versionado,
audit) tendría que tocar el schema a ciegas en el arranque. Con esto los
cambios se aplican en orden, una sola vez, y quedan registrados en la tabla
`schema_migrations` (idempotente, transaccional por migración).

Cada migración es {"id": int, "name": str, "run": callable(conn)}.
`run` recibe una conexión psycopg con la transacción ya abierta; si lanza
excepción, esa migración se revierte y el arranque falla (no se corrompe
nada). Las migraciones con comportamiento condicional (ej. pgvector cuando
la extensión no está instalada) marcan `skipped` en vez de fallar.
"""

from __future__ import annotations

import json

# ── migración 001: baseline (schema original del MVP) ─────────────────────
def _baseline(conn) -> None:
    for stmt in [
        """
        CREATE TABLE IF NOT EXISTS nodes(
          id          bigserial PRIMARY KEY,
          label       text NOT NULL,
          name        text NOT NULL,
          text        text NOT NULL DEFAULT '',
          properties  jsonb NOT NULL DEFAULT '{}'::jsonb,
          embedding   jsonb,
          updated_at  timestamptz NOT NULL DEFAULT now(),
          UNIQUE(label, name)
        )
        """,
        "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS embedding jsonb",
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
    ]:
        conn.execute(stmt)

# ── migración 002: pgvector (HNSW) — fail-soft ─────────────────────────────
# Si la extensión `vector` no está disponible (TrueNAS sin postgresql-pgvector),
# la migración se marca como skipped y el servicio sigue con el coseno Python.
def _pgvector(conn) -> None:
    row = conn.execute(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
    ).fetchone()
    if not row:
        raise _SkipMigration("extensión pgvector no disponible — coseno Python sigue activo")
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # columna nativa + índice HNSW (misma dimensión 192 que los embeddings actuales)
    conn.execute(
        "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS embedding_vec vector(192)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_embedding_vec "
        "ON nodes USING hnsw (embedding_vec vector_cosine_ops)"
    )

# ── migración 003: TTL + resumen de conversaciones (F2) ────────────────────
def _ttl_conversations(conn) -> None:
    conn.execute(
        "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS expires_at timestamptz"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_expires_at ON nodes(expires_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations(
          id          bigserial PRIMARY KEY,
          session_id  text NOT NULL UNIQUE,
          resumen     text NOT NULL DEFAULT '',
          lang        text NOT NULL DEFAULT 'es',
          meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
          updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

# ── migración 004: tokens por agente + audit (F3) ──────────────────────────
def _tokens_audit(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agents(
          token_hash  text PRIMARY KEY,
          agent       text NOT NULL UNIQUE,
          created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log(
          id          bigserial PRIMARY KEY,
          agent       text NOT NULL,
          action      text NOT NULL,
          detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)")

# ── migración 005: optimistic locking (F5) ─────────────────────────────────
def _optimistic_locking(conn) -> None:
    conn.execute(
        "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS version bigint NOT NULL DEFAULT 1"
    )

class _SkipMigration(Exception):
    """Migración no aplicable en este entorno (se registra como skipped)."""

MIGRATIONS = [
    {"id": 1, "name": "baseline", "run": _baseline},
    {"id": 2, "name": "pgvector_hnsw", "run": _pgvector},
    {"id": 3, "name": "ttl_conversations", "run": _ttl_conversations},
    {"id": 4, "name": "tokens_audit", "run": _tokens_audit},
    {"id": 5, "name": "optimistic_locking", "run": _optimistic_locking},
]

def apply_migrations(pool) -> dict:
    """Aplica las migraciones pendientes en orden. Idempotente.

    Devuelve {"applied": [nombres], "skipped": [nombres]} del arranque.
    """
    applied, skipped = [], []
    with pool.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations(
              id          int PRIMARY KEY,
              name        text NOT NULL,
              status      text NOT NULL DEFAULT 'applied',  -- applied|skipped
              applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        done = {r["id"]: r for r in conn.execute(
            "SELECT id, name, status FROM schema_migrations").fetchall()}
        for m in sorted(MIGRATIONS, key=lambda x: x["id"]):
            if m["id"] in done:
                continue
            try:
                m["run"](conn)
                status = "applied"
                applied.append(m["name"])
            except _SkipMigration as e:
                status = "skipped"
                skipped.append(m["name"])
                print(f"[kb] ⚠️ migración {m['id']} ({m['name']}) skipped: {e}")
            conn.execute(
                "INSERT INTO schema_migrations(id, name, status) VALUES (%s, %s, %s)",
                (m["id"], m["name"], status),
            )
            conn.commit()
    return {"applied": applied, "skipped": skipped}

def pgvector_active(pool) -> bool:
    """True si la columna vector(192) nativa existe (migración 002 aplicada)."""
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'nodes' AND column_name = 'embedding_vec'
            """
        ).fetchone()
    return bool(row)

def json_dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
