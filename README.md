# 🕸️ Agent-Mesh

> Biblioteca de **conocimiento viva y compartida** para los agentes del homelab (Hermes local,
> Hermes rack, **Mako**, JARVIS). Independientemente de con quién hables, la información de la
> que se nutren es **exactamente la misma**: una sola fuente de verdad en forma de grafo.
>
> *Los nombres de los agentes que aparecen en este README son los de **nuestro** montaje
> (casa + rack) y van **como ejemplo**: Agent-Mesh es **agnóstico** — funciona con cualquier
> conjunto de agentes que compartan el mismo grafo.*

## ✨ Características
- **Knowledge graph compartido** vía un servicio HTTP único (nodos + edges semánticos).
- **Full-text search** (FTS de PostgreSQL) para acceso rápido al conocimiento.
- **Recorridos de grafo** (`WITH RECURSIVE`) → "¿qué conecta A con B?", vecinos a N saltos.
- **PostgreSQL en TrueNAS** como motor (nativo de red, gestiona concurrencia, backups).
- **Redis** como capa de coordinación: avisa a todos los agentes cuando el conocimiento cambia.
- **Fase 2**: similaridad semántica con `pgvector` (embeddings).

## 🏗️ Arquitectura
```
                ┌─────────────────────────-─┐
 Hermes local ─►│        agent-mesh         │
 Hermes rack ──►│  Servicio HTTP (Docker)   │──► PostgreSQL (TrueNAS)
 OpenClaw -────►│  FastAPI :8084            │     nodos/edges/jsonb/FTS
 JARVIS Hermes ►│  Auth: Bearer KB_TOKEN    │
                └───────────┬─────────────-─┘
                            │ pub/sub "kb:changes"
                            ▼
                     Redis (bus del homelab)
```

## 📋 Requisitos
| Componente | Detalle |
|---|---|
| PostgreSQL 16+ | En TrueNAS (motor del grafo) |
| Redis | Bus de coordinación (opcional) |
| Docker | Para el servicio en el rack |

## 🚀 Instalación
1. `cp .env.example .env` y rellena `KB_DSN`, `KB_TOKEN`, `KB_REDIS_URL` (Postgres de TrueNAS).
2. Crear la BD/usuario en TrueNAS (o usar una existente): bd `agentmesh`, usuario con permiso.
3. En el rack: `docker compose up -d --build` (servicio en `:8084`).
4. **Semilla** (primera carga de conocimiento): `python scripts/seed.py --base http://<rack>:8084`
   → ingiere MEMORY.md + USER.md + skills como nodos/edges.

## 🔌 API
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado + conteo nodos/edges |
| GET | `/schema` | Labels + edge types actuales |
| PUT | `/n/{label}/{name}` | Upsert nodo `{text, properties}` |
| DELETE | `/n/{label}/{name}` | Borrar nodo (y edges) |
| PUT | `/e/{type}` | Upsert edge `{source, target, properties}` |
| GET | `/search?q=` | Búsqueda full-text |
| POST | `/graph?node=&hops=` | Vecinos / recorrido del grafo |

Auth: `Authorization: Bearer $KB_TOKEN` (si está configurado).

## 📁 Estructura
```
agent-mesh/
├── service/          → FastAPI + store PostgreSQL (imagen Docker)
├── client/           → librería Python (stdlib puro) para los agentes
├── scripts/seed.py   → ingesta inicial (memoria + skills)
├── skills/           → skill de Hermes para leer/escribir la KB
├── docker-compose.yml
└── .env.example
```

## Artículo

Cómo nació y qué resuelve agent-mesh, explicado de corrido:

**[agent-mesh: de codebase-memory a biblioteca compartida](https://raicesbinarias.es/articulos/agent-mesh-de-codebase-memory-a-biblioteca-compartida/)**

Publicado en [raicesbinarias.es](https://raicesbinarias.es/).

---

## 📄 Licencia
MIT — uso personal homelab.

