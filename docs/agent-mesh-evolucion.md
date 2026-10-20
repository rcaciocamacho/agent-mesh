# Agent-Mesh — Evolución del proyecto origen hasta el sistema actual

> Documento de cambios desde el proyecto origen **codebase_memory** hasta **agent-mesh**.
> Fecha de elaboración: 2026-10-19
> Repo actual: `git@github.com:usuario/agent-mesh.git` · Rama `main`
> Proyecto origen: **codebase-memory-mcp** (MCP server de terceros, DeusData)
> → `https://github.com/DeusData/codebase-memory-mcp`
> (fork comunitario: `win4r/codebase-memory-mcp-pro`).
> Desplegado inicialmente como módulo `codebase/` dentro de `rack-dashboard`
> (`/home/usuario/Repositorios/rack-dashboard/codebase/`). Plan original: `codebase_memory_plan.md`.

---

## 0. Resumen ejecutivo

**agent-mesh** es una **biblioteca de conocimiento viva y compartida** en forma de grafo, a la que acceden todos los agentes del homelab. La idea central: **independientemente de con quién hable el usuario, la información de la que se nutren los agentes es exactamente la misma** — una única fuente de verdad.

La evolución se desarrolló en **2 días de trabajo intensivo (2026-08-17 → 2026-08-18)** y pasó de un esqueleto/diseño a un sistema en producción con **failover real, auth por agente, pgvector y backups**, todo verificado con smoke tests.

| Métrica | Valor |
|---|---|
| Commits totales | 22 (desde `fe6117f` Initial commit) |
| Ventana de desarrollo | 2026-08-17 → 2026-08-18 |
| Fases completadas | F0–F6 (7 fases) |
| Stack final | FastAPI + PostgreSQL (core-server) + Redis + nginx LB |
| Estado | Implementado, desplegado y verificado en el rack |

---

## 1. El proyecto origen: **codebase_memory**

El origen directo de agent-mesh es el proyecto **codebase-memory-mcp** de terceros (DeusData) — un *code intelligence MCP server* que indexa repos de GitHub en un **grafo de conocimiento persistente** (162 lenguajes, binario único, cero dependencias): `https://github.com/DeusData/codebase-memory-mcp`. Se desplegó inicialmente como módulo `codebase/` dentro del Rack Dashboard. La idea semilla era: *un agente que indexa repos de GitHub y responde consultas sobre el código* (estructura de ficheros, clases, funciones, imports, docstrings), con búsqueda y exploración de ficheros.

**Plan original de codebase_memory:**

- **codebase-indexer** (FastAPI worker): conecta a GitHub (API/clone), indexa estructura,
clases, funciones, imports y docstrings; almacena en MySQL (`rack_cicd`) en tablas `codebase_repos`, `codebase_files`, `codebase_symbols`.
- **API REST** (`/api/codebase/*`): `repos`, `reindex`, `search?q=`, `file?path=`,
`context?file=&line=`.
- **UI en el Rack**: lista de repos indexados + buscador + explorer de ficheros.
- **Acceso para agentes**: vía HTTP a la API del Rack + mensajes en Redis (canal `agents`).

**Fases previstas de codebase_memory:**

- **Fase 1 — Core:** tablas MySQL, indexador (clona/pull, parsea Python/JS/TS/HTML),
endpoints básicos, integración en `main.py` del Rack.
- **Fase 2 — Conexión agentes:** helper de consulta vía API, notificaciones Redis al
reindexar, webhook GitHub para reindex automático.
- **Fase 3 — UI:** sección nueva en el dashboard del Rack.

**Punto de inflexión:** la idea de *"indexar un codebase y responder consultas"* (codebase_memory) evoluciona a *"biblioteca de conocimiento de grafo compartida entre agentes"* (agent-mesh) — se pasa de un índice de código de un solo consumidor a **una fuente de verdad semántica, compartida y escribible por todos los agentes**.

---

## 2. Línea temporal de cambios (git log completo)

### Fase 0 — Diseño y esqueleto (2026-08-17)

| Commit | Cambio |
|---|---|
| `fe6117f` | **Initial commit** |
| `915802c` | **diseño: agent-mesh** — biblioteca de conocimiento viva y compartida (esqueleto + spec) |

Decisión clave de diseño: **descartar SQLite** (fichero local, escritor único) en favor de **PostgreSQL** (nativo de red, FTS + JSONB + `WITH RECURSIVE`), con el servicio HTTP como **autoridad única de escritura**.

### Fase 1 — MVP (2026-08-17)

| Commit | Cambio |
|---|---|
| `396a25a` | **MVP agent-mesh**: servicio FastAPI + store PostgreSQL, cliente, seed, skill y docs |
| `cb3cda8` | skill agent-mesh: description concisa (routing) |

### Fase 2 — Auth + similaridad semántica (2026-08-17)

| Commit | Cambio |
|---|---|
| `8105d5e` | **auth Bearer obligatorio** + `POST /similar` (embeddings puros en Python) |
| `8c5749f` | Dockerfile copia `embeddings.py` (fix arranque contenedor) |
| `6572b2f` | docs + skill: auth obligatoria + `kb.similar` (fase 2) |

Detalle técnico: embeddings **deterministas, offline y sin API externa** — *hashing-trick* sobre n-gramas → vector de **192 dimensiones**, similitud coseno. Escala futura prevista: migrar a la extensión **pgvector** nativa (misma dim, operador `<=>`) **sin tocar a los clientes**.

### Fase 2b — Enriquecimiento y coordinación (2026-08-18)

| Commit | Cambio |
|---|---|
| `5f16f8f` | `seed_config.py`: persiste configuraciones no-secretas de los agentes al grafo |
| `02de630` | `enrich.py`: **conectividad por dominios** (anti-nodos-sueltos) — hubs servicios+infra, Rubén `GESTIONA`, skills/memorias → `RELACIONADO_CON` |
| `2fdae62` | **disparo por Redis**: `pub_seeded` + `enrich_listener` (demo/cron) + unidad systemd. Cuando los agentes persisten → `kb:seeded` → re-enrich conectividad |
| `ff006b9` | fix: `python -u` en unidad systemd (stdout sin buffer para journal) |

### Fase Spec-02 — Mejoras F0–F6 (2026-08-18)

| Commit | Cambio |
|---|---|
| `ff92fe9` | **spec02 F0–F5**: service con migraciones, pgvector, TTL+conversaciones, auth por agente+audit, locking |
| `8887829` | **spec02 F6**: `backup_kb.sh` (pg_dump + rotación, `KB_PG_DUMP` configurable) + `smoke_test.sh` (validación F0–F6 con docker) + plan spec-02 |
| `df1f823` | **spec02 F4**: 2º nodo en Dreamfyre + nginx LB con failover real + fix `row_factory` pool |
| `cd76831` | **F6**: `backup_kb.sh` sincroniza los backups a RustFS (S3, bucket `agentmesh-backups`) |
| `a1d71db` | spec-02: **F3 desplegado** — tokens por agente configurados y audit verificado |
| `c94afce` | spec-02: **F3 completo** — agentes del rack integrados con `kb_bridge` + listener arreglado |

### Fase de robustez (2026-08-18)

| Commit | Cambio |
|---|---|
| `e9bba14` | scripts: `failover_live_test.sh` — verificación manual del failover F4 |
| `cab325c` | fix: `enrich_listener` con timeouts explícitos de Redis (redis-py 8.x se colgaba en SUBSCRIBE) |
| `cc577c0` | fix: `enrich_listener` tolerante a timeouts puntuales del pubsub (re-suscribe en vez de morir) |
| `6ccba8f` | **F4**: failover multi-base en `client.py` — `base_url` acepta varias URLs separadas por coma |
| `f7b35b9` | skills: SKILL.md actualizado — client multi-base (failover F4) + rutas y auth al día |

---

## 3. Detalle de cada fase (F0–F6)

### F0 — Migraciones versionadas *(enabler)*
- `service/migrations.py`: tabla `schema_migrations`, migraciones **001..005**
transaccionales e idempotentes; marca `skipped` para entornos sin pgvector.
- `store.py` arranca aplicando migraciones.

### F1 — pgvector HNSW *(prioridad ALTA)* — fail-soft
- Migración 002: `CREATE EXTENSION vector`, columna `embedding_vec vector(192)`,
índice **HNSW** `vector_cosine_ops`.
- Si la extensión no está → `skipped` y el servicio sigue con **coseno Python**
(sin cambios visibles para los clientes).
- `similar()` usa `<=>` en SQL cuando pgvector está activo; fallback a coseno.

### F2 — TTL + resumen de conversaciones *(ALTA)*
- `nodes.expires_at` + **barrendero background** (task asyncio cada `KB_TTL_SWEEP` s)
que purga expirados (edges caen por `CASCADE`).
- Tabla `conversations` (`session_id`, `resumen`, `lang`, `meta`) + endpoints
`PUT/GET/DELETE /conversations/{session_id}`.
- El TTL se aplica **SOLO cuando el cliente lo pide** (`ttl_seconds`); el conocimiento
durable (sin TTL) **nunca expira**.

### F3 — Token por agente + audit *(MEDIA)* — **DESPLEGADO 2026-08-18**
- `KB_TOKENS={"agente":"token",...}` en `.env` de ambos nodos (gana a `KB_TOKEN` legacy).
- Tablas `agents` (`token_hash` → agente) y `audit_log` (agente, acción, detalle, ts).
- **Tokens generados:** uno por agente del homelab + alias `legacy`.
- **Clientes configurados:**
- Agente local (Lenovo): `.env` del repo → su token. - Agente con plugin (Lenovo): `config/api_keys.json` → `kb_base`/`kb_token`; el plugin `agent_mesh` lee de `api_keys.json` antes del env. - Agente del rack (MateBook): `stars-trello/kb_bridge.py` + `hermes_listener.py` (**NUEVO** — el unit `hermes-listener.service` apuntaba a un script inexistente y estaba en auto-restart) con su token desde `~/.openclaw/kb_config.json`. - Agente del MateBook: `stars-trello/listen_agents.py` integra `kb_bridge` con su token. - Los listeners responden por Telegram al supergrupo activo `-1003995338844` (el chat_id viejo `-5427554034` daba 400 Bad Request).
- **Verificado:** `audit_log` identifica a cada agente por su token
en escrituras; `legacy` sigue operativo.

### F5 — Optimistic locking *(BAJA)*
- `nodes.version` (bigint, `++` en cada upsert) + `if_match` en el body del PUT.
- Desajuste → **409 ConflictError**. Clientes sin `if_match` siguen funcionando igual.

### F6 — Backups / schema / Redis Streams *(BAJA)*
- Redis: `XADD` a `kb:changes:stream` (maxlen 1000) **además** del pub/sub actual
(listeners tardíos no pierden eventos). Aditivo, no rompe nada.
- Validación de schema cubierta por F0 (migraciones).
- Backup: `scripts/backup_kb.sh` (pg_dump + rotación) + sync a **RustFS**
(S3, bucket `agentmesh-backups`, retención `KB_RUSTFS_KEEP_DAYS`).

### F4 — SPOF MateBook *(MEDIA)* — **implementado (2º nodo + LB)**
- **Modelo:** primario = **MateBook** (`192.168.0.45:8084`); backup = **Dreamfyre**
(`192.168.0.26`, mismo contenedor en `:18084`).
- **nginx LB** en Dreamfyre `:18085` (upstream primario + backup, health pasivo
`max_fails=3 fail_timeout=15s` → si el MateBook cae, conmuta solo).
- Ambos nodos escriben en el **MISMO PostgreSQL** → cero pérdida, failover
transparente (verificado: escribir vía A y leer vía B tras matar A).
- `deploy/f4/` contiene `docker-compose.yml` + `lb.conf`.
- El nodo backup lleva `KB_TTL_SWEEP=0` (el barrendero solo corre en el primario).
- `Dockerfile` ahora respeta `KB_PORT` (permite variar el puerto por nodo).

---

## 4. Arquitectura final

```
                ┌──────────────────────────┐
 Agente 1 ─────►│        agent-mesh         │
 Agente 2 ─────►│  Servicio HTTP (Docker)   │──► PostgreSQL (core-server)
 Agente 3 ─────►│  FastAPI :8084            │     nodos/edges/jsonb/FTS/pgvector
 Agente N ─────►│  Auth: Bearer KB_TOKEN    │
                └───────────┬──────────────┘
                            │ pub/sub "kb:changes" + XADD kb:changes:stream
                            ▼
                     Redis (bus del homelab)

  Failover (F4):
  nginx LB :18085 ──► [ MateBook :8084 (primario) | Dreamfyre :18084 (backup) ]
                            └────────── misma BD ──────────┘
```

### Schema del grafo (semántico, no de código)
- `nodes(id, label, name, text, properties jsonb, updated_at, expires_at, version, embedding_vec)`
- `edges(id, type, source, target, properties jsonb, created_at)`
- `conversations(session_id, resumen, lang, meta)`
- `agents(token_hash → agente)` + `audit_log(agente, acción, detalle, ts)`
- `schema_migrations` (versionado de migraciones)

**Labels:** `Entidad`, `Concepto`, `Nota`, `Agente`, `Servicio`, `Skill`, `Proyecto`, `Persona`, `Infra`, `Lugar`. **Edges:** `RELACIONADO_CON`, `USADO_POR`, `CONECTADO_A`, `GESTIONA`, `DEPENDE_DE`, `BASADO_EN`, `PROVEEDOR_DE`.

### API REST
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado + conteo nodos/edges |
| GET | `/schema` | Labels + edge types actuales |
| PUT | `/n/{label}/{name}` | Upsert nodo `{text, properties}` (+`if_match`, `ttl_seconds`) |
| DELETE | `/n/{label}/{name}` | Borrar nodo (y edges) |
| PUT | `/e/{type}` | Upsert edge `{source, target, properties}` |
| GET | `/search?q=` | Búsqueda full-text (FTS) |
| POST | `/similar` | Búsqueda por similitud (embeddings / pgvector) |
| POST | `/graph?node=&hops=` | Vecinos / recorrido del grafo |
| PUT/GET/DELETE | `/conversations/{session_id}` | Resumen de conversaciones (F2) |

---

## 5. Estado de implementación (verificado 2026-08-18)

| Fase | Código | Verificado |
|------|--------|-----------|
| F0 migraciones | `service/migrations.py` | smoke test (postgres 16 y pgvector:pg16) |
| F1 pgvector | `store.similar() <=>` + backfill | con y sin extensión (2 runs) |
| F2 TTL + conversaciones | store + endpoints + sweeper | smoke test (TTL purgado en 2 ciclos) |
| F3 tokens + audit | store + main (`KB_TOKENS`) | smoke test (401/200 + audit por agente) |
| F5 optimistic locking | version + `if_match` | smoke test (409) |
| F6 streams + backup | XADD aditivo + `backup_kb.sh` | smoke test (stream XRANGE + dump/rotación) |
| F4 SPOF | 2º nodo Dreamfyre + nginx LB | failover real verificado (matar primario → conmuta) |

**Verificación (`scripts/smoke_test.sh`, 2026-08-18):**
- Sin pgvector (`postgres:16`): migración 002 `skipped`, fail-soft coseno OK.
- Con pgvector (`pgvector/pgvector:pg16`): migración 002 `applied`, `similar()` con `<=>`.
- Auth: 401 sin token / token malo; 200 con token de agente; `audit_log` registra el agente.
- Redis Streams: `XRANGE kb:changes:stream` devuelve el evento.
- Backup: dump con datos + rotación a KEEP backups.

---

## 6. Compatibilidad y garantías de no-regresión

- `client/client.py` mantiene su API pública; ahora `base_url` acepta **UNA o VARIAS URLs**
separadas por coma (failover F4): conmuta solo ante errores de conexión, **nunca** ante respuestas HTTP válidas (401/404/409).
- **Auth legacy** (`KB_TOKEN` único) sigue funcionando si no se define `KB_TOKENS`.
- Sin `ttl_seconds` / `if_match` el comportamiento es **idéntico** al anterior.
- Redis caído → el servicio sigue funcionando (solo pierde avisos).

---

## 7. Estructura del repo

```
agent-mesh/
├── service/          → FastAPI + store PostgreSQL (imagen Docker)
│   ├── main.py, store.py, migrations.py, embeddings.py, Dockerfile
├── client/           → librería Python (stdlib puro) para los agentes
│   └── client.py     → multi-base (failover F4)
├── scripts/          → seed.py, seed_config.py, enrich.py, enrich_listener.py,
│                       pub_seeded.py, backup_kb.sh, smoke_test.sh, failover_live_test.sh
├── skills/agent-mesh → SKILL.md (cómo leer/escribir la KB)
├── deploy/
│   ├── agent-mesh-enrich.service
│   └── f4/           → docker-compose.yml + lb.conf (nginx LB)
├── docs/specs/       → agent-mesh-design.md, spec-02-mejoras.md
├── docker-compose.yml
└── .env.example
```

---

## 8. Decisiones clave y lecciones

1. **codebase_memory (índice de código) → agent-mesh (grafo semántico compartido):** de un
consumidor de datos a una fuente de verdad escribible por todos los agentes.
2. **SQLite descartado → PostgreSQL** (nativo de red, concurrencia, FTS, JSONB,
recorridos recursivos). El servicio HTTP es la **autoridad única de escritura**.
3. **Redis como capa de coordinación, NO como motor**: canal `kb:changes` (pub/sub) +
`kb:changes:stream` (Redis Streams, para listeners tardíos).
4. **Embeddings offline** (hashing-trick, 192d) → sin dependencia de API externa;
pgvector HNSW como camino de escala, **fail-soft** si falta la extensión.
5. **Failover real** (F4) sin replicación de datos: ambos nodos escriben al **mismo Postgres**.
6. **TTL solo bajo demanda** del cliente; el conocimiento durable nunca expira.
7. **Fixes reales de robustez**: `redis-py 8.x` se colgaba en `SUBSCRIBE` (timeouts
explícitos), `python -u` en systemd, `row_factory` del pool, listener apuntando a script inexistente, chat_id de Telegram obsoleto.

---

*Fin del documento*
