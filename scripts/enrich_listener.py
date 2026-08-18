#!/usr/bin/env python3
"""
agent-mesh — escucha de eventos kb:seeded → re-ejecuta enrich.py.

Cuando Hermes o Mako publican "he persistido conocimiento" (canal/lista
`kb:seeded`, ver pub_seeded.py), este script re-ejecuta el enriquecimiento
de conectividad del grafo (scripts/enrich.py) para linkear los nodos nuevos
a sus hubs de dominio (anti-nodo-suelto).

Modos:
  python enrich_listener.py           # demonio (pub/sub, espera eventos)
  python enrich_listener.py --once    # modo cron: drena la lista y sale

Durabilidad: primero drena la lista `kb:seeded` (mensajes mientras estaba
caído) y luego queda a la escucha del canal. Dedupe por timestamp (solo
dispara si hay un evento MÁS RECIENTE que el último procesado).

Requiere: KB_BASE, KB_TOKEN, KB_DSN, KB_REDIS_URL (env o .env del repo).
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
REPO = Path(__file__).resolve().parent.parent
ENRICH = REPO / "scripts" / "enrich.py"
STATE_FILE = Path.home() / ".local" / "state" / "agentmesh-enrich-state.json"
MIN_INTERVAL_S = 10   # debounce: no correr enrich más de una vez cada 10s

def load_env() -> None:
    """inyecta KB_* desde el .env del repo si no están en el entorno."""
    p = REPO / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("KB_") and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

def last_ts() -> int:
    try:
        return int(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("ts", 0))
    except Exception:
        return 0

def save_ts(ts: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"ts": ts, "at": int(time.time())}), encoding="utf-8")

def run_enrich(on: int) -> bool:
    """Dispara enrich.py. Devuelve True si el proceso terminó bien."""
    env = os.environ.copy()
    env.update({
        "KB_BASE": os.environ.get("KB_BASE", "http://192.168.0.45:8084"),
        "KB_DSN": os.environ.get("KB_DSN", ""),
    })
    try:
        cp = subprocess.run(
            [sys.executable, str(ENRICH),
             "--base", env["KB_BASE"]],
            env=env, capture_output=True, text=True, timeout=240,
        )
        ok = cp.returncode == 0
        tail = (cp.stdout or "").strip().splitlines()
        print(f"[enrich] evento ts={on} → "
              f"{'OK' if ok else 'FALLO rc=' + str(cp.returncode)} :: "
              f"{(tail[-1] if tail else '(sin salida)')[:120]}")
        if not ok:
            print("[enrich] stderr:", (cp.stderr or "")[-500:])
        save_ts(on)
        return ok
    except subprocess.TimeoutExpired:
        print(f"[enrich] TIMEOUT ejecutando enrich para ts={on}")
        return False

def process_payload(payload: str) -> bool:
    try:
        ev = json.loads(payload)
        ts = int(ev.get("ts", 0))
        agent = ev.get("agent", "?")
    except Exception:
        return False
    if ts <= last_ts():
        return False                       # ya procesado
    if time.time() - os.path.getmtime(STATE_FILE) < MIN_INTERVAL_S and (time.time() - last_ts()) < MIN_INTERVAL_S:
        time.sleep(MIN_INTERVAL_S)         # debounce simple
    print(f"[enrich] evento de {agent} (ts={ts})")
    return run_enrich(ts)

def drain_list(r) -> bool:
    """Procesa los eventos acumulados en la lista (durabilidad)."""
    ran = False
    try:
        items = r.lrange(CHANNEL, 0, -1)
        if not items:
            return False
        # ordenar por ts asc para procesar el más reciente al final
        parsed = []
        for it in items:
            try:
                parsed.append(json.loads(it))
            except Exception:
                continue
        parsed.sort(key=lambda e: e.get("ts", 0))
        newest = max((e.get("ts", 0) for e in parsed), default=0)
        if newest > last_ts():
            run_enrich(newest)             # uno basta: enrich es idempotente
            ran = True
        # vaciar la cola procesada
        r.delete(CHANNEL)
    except Exception as e:
        print(f"[enrich] ⚠️ drain_list: {e}")
    return ran

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="modo cron: drena y sale")
    args = ap.parse_args()

    load_env()
    try:
        import redis
        # timeouts explícitos: redis-py 8.x puede colgarse en el SUBSCRIBE si
        # no se fijan (visto en MateBook con python3.14 + user-site).
        r = redis.Redis.from_url(os.environ.get("KB_REDIS_URL", DEFAULT_REDIS),
                                 decode_responses=True,
                                 socket_connect_timeout=10, socket_timeout=10)
        r.ping()
    except Exception as e:
        print(f"[enrich] ❌ Redis inalcanzable: {e}")
        return 1

    # Primero drenamos eventos acumulados (vulnera cualquier hueco)
    did = drain_list(r)
    if args.once:
        print(f"[enrich] --once terminado (disparado={did})")
        return 0

    print(f"[enrich] demonio a la escucha en '{CHANNEL}' ...")
    while True:
        try:
            pubsub = r.pubsub()
            pubsub.subscribe(CHANNEL)
            for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                process_payload(msg.get("data", ""))
                drain_list(r)              # limpiar acumulados pendientes
        except redis.exceptions.TimeoutError:
            # un timeout puntual (p.ej. tras un enrich largo) NO debe matar el
            # demonio: se re-suscribe y se sigue escuchando.
            print("[enrich] ⚠️ timeout puntual en pubsub — re-suscribiendo")
        except KeyboardInterrupt:
            print("[enrich] saliendo (Ctrl+C)")
            return 0
        except Exception as e:
            print(f"[enrich] ⚠️ error en bucle ({e}) — re-suscribiendo")

if __name__ == "__main__":
    raise SystemExit(main())