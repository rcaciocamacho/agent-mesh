---
name: agent-mesh
description: Use to read/write the shared agent-mesh knowledge graph.
version: 1.0.0
author: Rubén + Hermes
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [knowledge-graph, shared-memory, multi-agent, agent-mesh]
    related_skills: [recalling-past-context, project-knowledge]
---

# Agent-Mesh — biblioteca de conocimiento compartida entre agentes

## When to Use
- Cuando quieras **consultar conocimiento de la organización** (servicios, infraestructura,
  proyectos, preferencias, entidades) que **todos los agentes comparten** (Hermes local/rack,
  Mako, JARVIS).
- Cuando aprendas un **hecho durable y compartible** (no efímero) que deberían conocer el resto
  de agentes del homelab → **escríbelo en el grafo**, no solo en la memoria local.
- Antes de responder "¿quién usa X?" / "¿cómo se conecta A con B?" / "¿qué hay de Y?",
  consulta el grafo.

## Idea clave
La **misma base de conocimiento para todos los agentes**: da igual con quién hable el usuario,
todos se alimentan de la misma fuente. Es complementario a la memoria local (MEMORY/USER).
- **Memoria local (MEMORY.md/USER.md)** → solo este perfil Hermes.
- **Agent-mesh** → conocimiento compartido (infra, servicios, proyectos, entidades, decisiones).

## Cómo consultar (lectura)
El cliente vive en el repo `agent-mesh` (`client/client.py`, stdlib puro). Desde cualquier
entorno Python:

```python
import sys; sys.path.insert(0, "<...>/agent-mesh/client")
from client import Client

kb = Client("http://<rack-host>:8084", token="<KB_TOKEN>")
kb.schema()                                   # labels + edge types disponibles
kb.search("presencia jarvis", limit=10)       # búsqueda full-text (FTS)
kb.graph(node_id, hops=2)                     # vecinos / recorrido del grafo
```

Base URL y token en config/env (`KB_BASE`, `KB_TOKEN`). Si no se puede alcanzar el servicio,
**degradar con elegancia**: usa la memoria local y anota que la KB no estaba disponible.

## Cómo escribir (hechos compartibles)
Solo escribe hechos **durables y relevantes para otros agentes** (infra, servicios, IPs,
proyectos, decisiones, preferencias de Rubén). Reglas:

- `label` entre: `Entidad, Concepto, Nota, Agente, Servicio, Skill, Proyecto, Persona, Infra, Lugar`.
- `edge` entre: `RELACIONADO_CON, USADO_POR, CONECTADO_A, GESTIONA, DEPENDE_DE, BASADO_EN, PROVEEDOR_DE`.
- `name` estable y único por label; `text` = contenido consultable; `properties` = metadatos JSON.

```python
ruben = kb.upsert_node("Persona", "Rubén", text="Gestor del homelab")
jarvis = kb.upsert_node("Servicio", "JARVIS", text="Asistente de voz")
kb.upsert_edge("GESTIONA", ruben["id"], jarvis["id"])

# si es SOLO local/efímero, NO lo metas aquí → usa memoria local
```

## Pitfalls
- **No duplicar**: antes de escribir, `search()` para ver si el hecho ya existe (upsert por label+name).
- **No volcar nada privado/no compartible** sin necesidad: es una base compartida por varios agentes.
- **Auth**: si `KB_TOKEN` está configurado, el servicio exige `Bearer`; un 401 = token ausente/mal.
- **Disponibilidad**: si el servicio cae, no bloquees; degrada a memoria local y avisa.
- Para migrar/mantener la semilla: `python scripts/seed.py` desde el repo (ingiere MEMORY/USER/skills).

## Verificación
- `kb.health()` → `{"status":"ok","nodes":N,"edges":M}`.
- `kb.schema()` → te muestra qué labels/tipos de edge hay antes de consultar/escribir.
