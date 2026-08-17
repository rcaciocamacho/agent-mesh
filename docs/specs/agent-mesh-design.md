# Agent-Mesh — Biblioteca de conocimiento viva y compartida entre agentes

Fecha: 2026-08-17 · Estado: **Diseño (pendiente de aprobación)** · Autor: Rubén + Hermes

## Objetivo

Una **base de conocimiento de grafo compartida** a la que acceden todos los agentes
(Hermes local, Hermes del rack, **Mako** y JARVIS) de modo que, **independientemente de
con quién hable el usuario, la información de la que se nutren sea exactamente la misma**.

No es un indexador de código: es una **biblioteca viva semántica** donde los agentes
leen y **escriben** conocimiento organizado y relacionado, con acceso rápido.

## Decisiones tomadas

- **Motor de grafo = PostgreSQL en TrueNAS** (nativo de red, FTS + JSONB + `WITH RECURSIVE`
  para recorridos, `pgvector` en fase 2). El **servicio HTTP** sigue siendo la autoridad
  única de escritura; Postgres gestiona la concurrencia.
- **Redis** (bus Hermes↔Mako ya existente, pi.hole:6379) como **capa de coordinación**:
  notifica a los agentes cuando el conocimiento cambia (canal `kb:changes`).
  No es el motor de la biblioteca.
- ~~SQLite~~ (descartado: fichero local, escritor único; para servicio en red Postgres es mejor).

## Arquitectura

```
                  ┌─────────────────────────────┐
  Hermes local ──►│        agent-mesh           │
  Hermes rack ───►│  Servicio HTTP (Docker)     │──► SQLite (nodos/edges/FTS5/vectores)
  Mako ──────────►│  FastAPI :8084              │
  JARVIS ────────►│  Auth: token (KB_TOKEN)     │
                  └──────────────┬──────────────┘
                                 │ pub/sub "kb:changes"
                                 ▼
                          Redis (pi.hole:6379)
```

- **Service** (`service/`): API REST sobre SQLite. Contenedor en el rack.
- **Client** (`client/`): librería Python reutilizada por los agentes (read/write).
- **Skill** (`skills/`): definición para Hermes de cómo leer/escribir en la KB.
- **Seed** (`scripts/seed.py`): ingesta inicial de MEMORY.md + USER.md + skills.
- **Redis** (`scripts/` u opcional): publica `kb:changes` tras cada escritura.

## Schema del grafo (semántico, no de código)

Tablas (PostgreSQL):
- `nodes(id bigserial, label text, name text, text text, properties jsonb, updated_at timestamptz)`
- `edges(id bigserial, type text, source int, target int, properties jsonb, created_at timestamptz)`
- FTS: índice GIN sobre `to_tsvector('spanish', label || name || text)`
- Fase 2: `node_vectors(id, vector pgvector)` para similaridad semántica

Labels de nodos: `Entidad`, `Concepto`, `Nota`, `Agente`, `Servicio`, `Skill`,
`Proyecto`, `Persona`, `Infra`, `Lugar`.

Tipos de edge: `RELACIONADO_CON`, `USADO_POR`, `CONECTADO_A`, `GESTIONA`,
`DEPENDE_DE`, `BASADO_EN`, `PROVEEDOR_DE`.

## API REST (borrador)

| Método | Ruta | Descripción |
|---|---|---|
| PUT | `/n/{label}/{name}` | Upsert nodo (con `text` para búsqueda/semántica) |
| PUT | `/e/{type}` | Upsert edge `{source, target}` |
| DELETE | `/n/{label}/{name}` | Borrar nodo (y sus edges) |
| GET | `/search?q=` | Búsqueda FTS5 (top-N) |
| POST | `/similar` | Búsqueda por embeddings |
| POST | `/graph` | Vecinos / recorrido a N saltos |
| GET | `/schema` | Labels + edge types actuales |
| GET | `/health` | Salud del servicio |

Auth: header `Authorization: Bearer $KB_TOKEN` (env), leer `.env`.

## Flujo de datos

1. Un agente escribe → `PUT /n` o `PUT /e`.
2. El servicio persiste en SQLite y **publica `kb:changes`** en Redis (cambió nodo `X`).
3. Los demás agentes suscritos refrescan su caché/contexto con la info nueva.
4. Cualquier agente consulta con `GET /search`, `POST /similar` o `POST /graph`
   → **le devuelve exactamente lo mismo** que a los demás (misma BD).

## Contenido semilla (seed)

Ingesta inicial desde lo que Hermes maneja hoy:
- `MEMORY.md` (perfil) y `USER.md` → nodos `Nota`/`Persona` + edges.
- Skills de Hermes → nodos `Skill` (descripción + cuándo usar) + edges `USADO_EN`.
- Añadir entidades de infraestructura (rack, TrueNAS, Mako, Hermes, JARVIS, servidores).

## Riesgos / pendientes

- **Embeddings**: ¿local (sentence-transformers) o API externa? (para `POST /similar`)
  → decisión pendiente; si no, MVP arranca solo con FTS5.
- **Redis ¿siempre disponible?**: si cae, el servicio sigue funcionando (solo pierde avisos).
- **Auth multi-agente**: token único por ahora; permitir varios roles más adelante.
- **Visibilidad**: repo y servicio **privados** (contienen IPs del rack y datos personales).
- **Migración de conocimiento**: el seed es la fuente inicial; luego los agentes escriben.

## No entra en el alcance (MVP)

- Consistencia distribuida real (single-writer vía el servicio es suficiente).
- Reconciliación de conflictos entre agentes (última escritura gana).
- Interfaz web/visualización del grafo (posible fase 2).
