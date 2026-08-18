#!/usr/bin/env python3
"""
agent-mesh — enriquecimiento de conectividad (anti-nodo-suelto).

Convierte la estrella plana (todo -> Hermes) en un grafo por dominios:
  - Crea/asegura hubs de Servicio/Infra (UniWebAdmin, Lodestone, TrueNAS,
    MateBook, Redis/pi.hole, Navidrome).
  - Rubén GESTIONA esos servicios.
  - Enlaza cada Skill/Memoria al hub que le corresponde por keywords
    (edge RELACIONADO_CON). Idempotente (edges UNIQUE(type,source,target)).

Uso: python scripts/enrich.py --base <url> --token <KB_TOKEN>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from client import Client  # noqa: E402

# hub: (label, name, text) — se crea si no existe
HUBS = [
    ("Servicio", "agent-mesh", "Knowledge graph compartido de los agentes (Hermes/Mako/JARVIS); servicio en MateBook :8084, PostgreSQL TrueNAS, Redis kb:changes."),
    ("Servicio", "UniWebAdmin", "Gestor del hotel (reportes, empleados, conciliación) bajo UniWebAdmin; repo y despliegue gestionados por Rubén."),
    ("Servicio", "Lodestone", "Hub de logs con IA del homelab (notification-hub); app en MateBook :8200, Gotify."),
    ("Infra", "TrueNAS", "NAS del homelab (192.168.0.20): almacenamiento + PostgreSQL 17 (base agentmesh del knowledge graph)."),
    ("Infra", "MateBook", "Portátil 192.168.0.45 del homelab: ejecuta agent-mesh (Docker :8084) y Lodestone (:8200)."),
    ("Infra", "Redis-pihole", "Bus Redis del homelab (pi.hole 192.168.0.46:6379, sin pass): coordinación agentes (canal agents) + kb:changes."),
    ("Servicio", "Navidrome", "Servidor de música del homelab (primario para Rubén); YouTube solo para descargas."),
    ("Agente", "JARVIS", "Asistente de voz del homelab (cerebro en JarvisVitaminado/jarvisbrain, backend jarvis-backend.service, wake word, presencia, luces Tapo, monitor DDC)."),
]

# keywords (minúsculas) -> nombre exacto del hub destino
KEYWORDS = [
    (("agent-mesh", "knowledge graph"), "agent-mesh"),
    (("uniwebadmin", "concilia"), "UniWebAdmin"),
    (("lodestone", "notification-hub", "bitacora"), "Lodestone"),
    (("truenas", "postgres"), "TrueNAS"),
    (("matebook",), "MateBook"),
    (("navidrome",), "Navidrome"),
    (("redis", "pi.hole", "pi-hole"), "Redis-pihole"),
    (("jarvis", "tapo", "wake word"), "JARVIS"),
]

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("KB_BASE", "http://127.0.0.1:8084"))
    ap.add_argument("--token", default=os.environ.get("KB_TOKEN", ""))
    ap.add_argument("--dsn", default=os.environ.get("KB_DSN", ""),
                    help="DSN opcional para matching exhaustivo (leer todos los nodos).")
    args = ap.parse_args()
    kb = Client(args.base, args.token)
    print(f"→ service: {kb.base}  health: {kb.health()}")

    # 1) hubs
    hub_ids = {}
    for label, name, text in HUBS:
        nd = kb.upsert_node(label, name, text=text)
        hub_ids[name] = nd["id"]
        print(f"  hub {label}/{name} -> #{nd['id']}")

    # 2) Rubén GESTIONA cada hub de Servicio
    ruben = kb.upsert_node("Persona", "Rubén", "usuario — gestor del homelab; español nativo.")
    gestion = 0
    for name, nid in hub_ids.items():
        kb.upsert_edge("GESTIONA", ruben["id"], nid); gestion += 1
    print(f"  Rubén GESTIONA -> {gestion} servicios")

    # 3) matching exhaustivo (si hay DSN) o por búsquedas (fallback cliente puro)
    leaves = []   # (id, name, text)
    if args.dsn:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(args.dsn, connect_timeout=6)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id,label,name,text FROM nodes")
        leaves = [(r["id"], r["name"], r["text"] or "") for r in cur.fetchall()]
        conn.close()
        print(f"  matching exhaustivo sobre {len(leaves)} nodos (DSN)")
    else:
        for name in ["skill", "memoria", "preferencia", "servicio", "infra", "entidad", "concepto"]:
            try:
                res = kb.search(name, limit=200)
                leaves += [(r["id"], r["name"], r["text"] or "") for r in res]
            except Exception:
                pass
        print(f"  matching por búsquedas sobre {len(leaves)} nodos (fallback)")

    edges_added = 0
    def norm(s): return (s or "").lower()
    for node_id, name, text in leaves:
        hay = norm(name) + " " + norm(text) + " " + norm(name)
        for kw_group, hub in KEYWORDS:
            if hub not in hub_ids:
                continue
            if any(kw in hay for kw in kw_group):
                try:
                    kb.upsert_edge("RELACIONADO_CON", node_id, hub_ids[hub])
                    edges_added += 1
                except Exception as e:
                    print("  edge err", node_id, hub, str(e)[:60])
    print(f"  edges RELACIONADO_CON añadidos (nodo->hub): {edges_added}")
    print(f"  total: {kb.health()}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
