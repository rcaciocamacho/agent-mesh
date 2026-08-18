#!/usr/bin/env python3
"""
agent-mesh — publicador de evento "conocimiento persistido" por Redis.

Lo llaman los agentes (Hermes, Mako, …) DESPUÉS de ejecutar su persistencia
(seed.py/seed_config.py) cada N horas. Publica en el canal `kb:seeded` un
mensaje de evento; cualquier escucha (enrich_listener / enrich_poll) re-ejecuta
el enriquecimiento de conectividad del grafo con los nodos nuevos.

Durabilidad: RPUSH a la lista `kb:seeded` + PUBLISH al canal `kb:seeded`.

Uso:
    python scripts/pub_seeded.py --agent hermes
    (KB_REDIS_URL vía env o .env del repo)

    Con venv hermano: se puede invocar sin redis instalado usando
    PUBLISH via `redis-cli` si no hay módulo redis (fallback automático).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_REDIS = "redis://192.168.0.46:6379/0"
CHANNEL = "kb:seeded"

def _env_from_repo_dotenv():
    """Carga KB_REDIS_URL del .env del repo si existe y no está en env."""
    if os.environ.get("KB_REDIS_URL"):
        return
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("KB_REDIS_URL="):
                os.environ["KB_REDIS_URL"] = line.split("=", 1)[1].strip()

def _load_redis():
    try:
        import redis
        return redis.Redis.from_url(os.environ.get("KB_REDIS_URL", DEFAULT_REDIS),
                                    decode_responses=True)
    except ImportError:
        return None

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=os.environ.get("AGENT_NAME", "hermes"))
    args = ap.parse_args()
    _env_from_repo_dotenv()

    event = {
        "agent": args.agent,
        "ts": int(time.time()),
        "kind": "seeded",
        "channel": CHANNEL,
    }
    payload = json.dumps(event, ensure_ascii=False)

    r = _load_redis()
    if r is not None:
        r.rpush(CHANNEL, payload)          # durable queue
        r.publish(CHANNEL, payload)        # live notify
        print(f"✓ kb:seeded publicado por {args.agent} (rpush+publish)")
        return 0

    # fallback: redis-cli
    url = os.environ.get("KB_REDIS_URL", DEFAULT_REDIS)
    try:
        subprocess.run(["redis-cli", "-u", url, "PUBLISH", CHANNEL, payload],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(["redis-cli", "-u", url, "RPUSH", CHANNEL, payload],
                       check=True, capture_output=True, timeout=10)
        print(f"✓ kb:seeded publicado por {args.agent} (redis-cli fallback)")
        return 0
    except Exception as e:
        print(f"⚠️ {sys.argv[0]}: no se pudo publicar: {e}")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())