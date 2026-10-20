#!/usr/bin/env python3
"""
agent-mesh — seed de CONFIGURACIÓN (no secretos) de Hermes al grafo.

Persiste hechos configurables no sensibles (modelo/proveedor, kanban, MCP,
voz, seguridad) como nodos enlazados a Agente/Hermes. NO vuelca secretos
(API keys, tokens, contraseñas) — esos quedan fuera del grafo compartido.

Uso:
    python scripts/seed_config.py --base http://<host>:8084 --token <KB_TOKEN>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from client import Client  # noqa: E402

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("KB_BASE", "http://127.0.0.1:8084"))
    ap.add_argument("--token", default=os.environ.get("KB_TOKEN", ""))
    args = ap.parse_args()

    kb = Client(args.base, args.token)
    print(f"→ service: {kb.base}  health: {kb.health()}")

    hermes = kb.upsert_node("Agente", "Hermes",
                            "Asistente Hermes (Nous Research) del perfil ruben.")

    configs = [
        ("Hermes-config-modelo",
         "Modelo por defecto de Hermes: deepseek-v4-flash vía provider opencode-go "
         "(base_url https://opencode.ai/zen/go/v1, api_mode chat_completions). "
         "Esto es la configuración de LLM del perfil ruben."),
        ("Hermes-config-agente",
         "Hermes: verify_on_stop=false, reasoning_effort=high, display tool_progress=all."),
        ("Hermes-config-kanban",
         "Kanban: auto_decompose=true (descomposición automática de tareas)."),
        ("Hermes-config-mcp",
         "MCP: servidor codebase_memory habilitado (binary ~/.local/bin/codebase-memory-mcp)."),
        ("Hermes-config-voz",
         "Voz Hermes: speech-to-text local (faster-whisper) y text-to-speech edge. "
         "Se controla con /voice on|tts|off."),
        ("Hermes-config-seguridad",
         "Seguridad Hermes: redacción de secretos activada por defecto "
         "(redact_secrets), escaneo pre-ejecución tirith habilitado si el binario está disponible."),
        ("Hermes-config-gateway",
         "Gateway Hermes local: servicio systemd hermes-gateway-ruben.service, "
         "web-ui en 127.0.0.1:9119."),
    ]

    n = 0
    for name, text in configs:
        nd = kb.upsert_node("Memoria", name, text=text, properties={"area": "config"})
        kb.upsert_edge("BASADO_EN", nd["id"], hermes["id"])
        n += 1

    print(f"✓ configuración (no-secretos): {n} nodos ingeridos.")
    print(f"  total en servicio: {kb.health()}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
