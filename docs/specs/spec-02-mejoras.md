# Spec 02 — Mejoras propuestas para agent-mesh (plan de ejecución)

Fecha: 2026-08-18 · Estado: **IMPLEMENTADO, DESPLEGADO Y VERIFICADO** en el rack (MateBook primario + Dreamfyre backup/LB + TrueNAS + pi.hole)
Autor: spec vía Redis (canal agents) + análisis/implementación Hermes

> El archivo original referenciado (`log-chat-agent/specs/02-mejoras-agent-mesh.md`)
> no existe en el Lenovo; el plan se ancló al resumen del mensaje + el diseño real.

## Puntos del spec y prioridades

| # | Mejora | Prioridad |
|---|--------|-----------|
| 1 | TTL + resumen de conversaciones | ALTA |
| 2 | SPOF MateBook | MEDIA |
| 3 | Token por agente + audit | MEDIA |
| 4 | Optimistic locking | BAJA |
| 5 | Índice vectorial pgvector HNSW | ALTA |
| 6 | Backups / validación schema / Redis Streams | BAJA |

## Orden lógico de ejecución (dependencias primero)

1. **F0 — Migraciones versionadas** (enabler: casi todo toca schema)
   - `service/migrations.py`: tabla `schema_migrations`, migraciones 001..005
     transaccionales e idempotentes; `skipped` para entornos sin pgvector.
   - `store.py` arranca aplicando migraciones.

2. **F1 — pgvector HNSW** (ALTA, punto 5) — fail-soft
   - Migración 002: `CREATE EXTENSION vector`, columna `embedding_vec vector(192)`,
     índice HNSW `vector_cosine_ops`. Si la extensión no está en TrueNAS,
     se marca `skipped` y el servicio sigue con el coseno Python (sin cambios
     visibles para los clientes).
   - `similar()` usa `<=>` en SQL cuando pgvector está activo; fallback a coseno.

3. **F2 — TTL + resumen de conversaciones** (ALTA, punto 1)
   - `nodes.expires_at` + barrendero background (task asyncio cada `KB_TTL_SWEEP` s)
     que purga expirados (edges caen por CASCADE).
   - Tabla `conversations` (session_id, resumen, lang, meta) + endpoints
     PUT/GET/DELETE `/conversations/{session_id}`.
   - El TTL se aplica SOLO cuando el cliente lo pide (`ttl_seconds`); el
     conocimiento durable (sin ttl) nunca expira.

4. **F3 — Token por agente + audit** (MEDIA, punto 3) — **DESPLEGADO 2026-08-18**
   - `KB_TOKENS={"agente":"token",...}` en .env de ambos nodos (gana a KB_TOKEN legacy).
   - Tablas `agents` (token_hash → agente) y `audit_log` (agente, acción, detalle, ts).
   - Tokens generados: `hermes-local`, `hermes-rack`, `mako`, `jarvis` + alias `legacy`
     (token histórico, mantenido para no romper clientes sin migrar).
   - Clientes configurados:
     - Hermes local (Lenovo): `.env` del repo → KB_TOKEN=hermes-local.
     - JARVIS (Lenovo): `jarvisbrain/config/api_keys.json` → kb_base/kb_token;
       plugin `agent_mesh` lee de api_keys.json (patrón llm_client.py) antes del env.
     - Hermes rack (MateBook): `stars-trello/kb_bridge.py` + hermes_listener.py
       (NUEVO — el unit hermes-listener.service apuntaba a un script inexistente
       y estaba en auto-restart) con token hermes-rack desde ~/.openclaw/kb_config.json.
     - Mako (MateBook): `stars-trello/listen_agents.py` integra kb_bridge con token mako.
     - Ambos listeners responden por Telegram al supergrupo activo
       (-1003995338844; el chat_id viejo -5427554034 daba 400 Bad Request).
   - Verificado: audit_log identifica `jarvis`, `hermes-local`, `mako` y
     `hermes-rack` en escrituras; legacy sigue operativo (compatibilidad).
     kb_state vía Redis: ambos agentes responden 230/384.

5. **F5 — Optimistic locking** (BAJA, punto 4)
   - `nodes.version` (bigint, ++ en cada upsert) + `if_match` en el body del PUT.
   - Desajuste → 409 ConflictError. Los clientes que no manden `if_match`
     siguen funcionando igual (compatibilidad).

6. **F6 — Backups / schema / Redis Streams** (BAJA, punto 6)
   - Redis: XADD a `kb:changes:stream` (maxlen 1000) además del pub/sub actual
     (los listeners tardíos no pierden eventos). Aditivo, no rompe nada.
   - Validación de schema cubierta por F0 (migraciones).
   - Backup: `scripts/backup_kb.sh` (pg_dump + rotación, creado y verificado).

7. **F4 — SPOF MateBook** (MEDIA, punto 2) — **implementado (2º nodo + LB)**
   - Modelo: **primario = MateBook** (192.168.0.45:8084, contenedor existente);
     **backup = Dreamfyre** (192.168.0.26) con el mismo contenedor en :18084.
     nginx LB en Dreamfyre :18085 (upstream primario + backup, health pasivo
     `max_fails=3 fail_timeout=15s`: si el MateBook cae, conmuta solo).
   - Ambos nodos escriben en el MISMO PostgreSQL (TrueNAS) → cero pérdida,
     failover transparente (verificado: escribir vía A y leer vía B tras
     matar A).
   - `deploy/f4/` contiene docker-compose.yml + lb.conf para Dreamfyre.
   - El nodo backup lleva `KB_TTL_SWEEP=0` (el barrendero solo corre en el
     primario, evita purgas duplicadas).
   - Dockerfile ahora respeta `KB_PORT` (permite variar el puerto por nodo).

## Estado de implementación

| Fase | Código | Verificado |
|------|--------|-----------|
| F0 migraciones | ✅ `service/migrations.py` | smoke test (docker postgres 16 y pgvector:pg16) |
| F1 pgvector | ✅ store.similar() <=> + backfill embedding_vec | con y sin extensión (2 runs) |
| F2 TTL+conversaciones | ✅ store + endpoints + sweeper | smoke test (TTL purgado en 2 ciclos) |
| F3 tokens+audit | ✅ store + main (KB_TOKENS) | smoke test (401/200 + audit por agente) |
| F5 optimistic locking | ✅ version + if_match | smoke test (409) |
| F6 streams + backup | ✅ XADD aditivo + `scripts/backup_kb.sh` | smoke test (stream XRANGE + dump/rotación) |
| F4 SPOF | ✅ 2º nodo Dreamfyre :18084 + nginx LB :18085 (`deploy/f4/`) | failover real verificado (matar primario → conmuta a backup) |

Verificación realizada el 2026-08-18 (scripts/smoke_test.sh):
- Sin pgvector (postgres:16): migración 002 `skipped`, fail-soft coseno OK.
- Con pgvector (pgvector/pgvector:pg16): migración 002 `applied`, similar() con <=>.
- Auth: 401 sin token / token malo; 200 con token de agente; audit_log registra el agente.
- Redis Streams: XRANGE kb:changes:stream devuelve el evento.
- Backup: dump con datos + rotación a KEEP backups.

## Compatibilidad

- `client/client.py` mantiene su API pública; ahora `base_url` acepta UNA url
  o VARIAS separadas por coma (failover F4): conmuta solo ante errores de
  conexión, nunca ante respuestas HTTP válidas (401/404/409).
- Auth legacy (KB_TOKEN único) sigue funcionando si no se define KB_TOKENS.
- Sin `ttl_seconds`/`if_match` el comportamiento es idéntico al anterior.
