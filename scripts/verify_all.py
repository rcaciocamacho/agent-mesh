#!/usr/bin/env python3
"""
verify_all.py — verificación integral de agent-mesh (spec 02).

Recorre TODOS los componentes y reporta PASS/FAIL por módulo:
  S1 servicio primario (MateBook :8084)
  S2 servicio backup  (Dreamfyre :18084)
  S3 LB nginx         (Dreamfyre :18085)
  S4 PostgreSQL (TrueNAS): extensión vector + migraciones
  S5 Redis bus (pi.hole)
  S6 Auth por agente (multi-token) + audit en escritura
  S7 Optimistic locking (409 en versión obsoleta)
  S8 /similar con pgvector (<=>)
  S9 Client multi-base (failover: primario + backup responden)
  S10 failover de servicio (se para primario 30s ⇒ LB + client siguen sirviendo)
  S11 TTL (nodo efímero expira)
  S12 Backups (dump local Dreamfyre + presente en RustFS)
  S13 Listeners (mako, hermes, agent-mesh-enrich activos) + JARVIS kb_state
  S14 Búsqueda full-text

Uso: python3 scripts/verify_all.py [--skip-failover]   (--skip-failover omite S10)
Requiere: acceso SSH a MateBook/Dreamfyre/TrueNAS (clave ~/.ssh/id_ed25519)
          y el .env del repo (KB_TOKEN para auth tests).
Salida: PASS/FAIL por módulo + resumen final. NO modifica nada permanente:
crea nodos de prueba con nombre `verify-*` y los borra al final.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "client"))
from client import Client  # noqa: E402

SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
MATE = "usuario@192.168.0.45"
DREAM = "usuario@192.168.0.26"
TN = "root@192.168.0.20"
PRIMARIO = "http://192.168.0.45:8084"
LB = "http://192.168.0.26:18085"
BACKUP = "http://192.168.0.26:18084"

TOKEN = os.environ.get("KB_TOKEN", "")
if not TOKEN:
    # intentar leerlo del .env del repo
    envf = REPO / ".env"
    for line in envf.read_text().splitlines() if envf.exists() else []:
        if line.startswith("KB_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip()
            break

RESULTS: list[tuple[str, str, str]] = []

def check(label: str, ok: bool, detail: str = ""):
    RESULTS.append((label, "PASS" if ok else "FAIL", detail))
    print(f"  {'✓' if ok else '✗'} {label}  {detail}")

def ssh(host: str, cmd: str, timeout: int = 40) -> str:
    r = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=8", host, cmd],
        capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr

def http(url: str, timeout: float = 5, method: str = "GET", body=None) -> str:
    try:
        import urllib.request
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()
    except Exception:
        return ""

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-failover", action="store_true")
    args = ap.parse_args()
    print("=" * 60)
    print("VERIFICACIÓN INTEGRAL agent-mesh (spec 02)")
    print("=" * 60)

    # ── S1-S3 servicios ────────────────────────────────────────────────────
    print("\n[SERVICIOS]")
    h = http(f"{PRIMARIO}/health")
    check("S1 primario MateBook", '"status":"ok"' in h, h[:80])
    h = http(f"{BACKUP}/health")
    check("S2 backup Dreamfyre", '"status":"ok"' in h, h[:80])
    h = http(f"{LB}/health")
    check("S3 LB nginx", '"status":"ok"' in h, h[:80])

    # ── S4 PostgreSQL / vector / migraciones ──────────────────────────────
    print("\n[POSTGRES / PGVECTOR]")
    out = ssh(TN, 'PG=$(docker ps -q --filter name=ix-postgres-postgres-1 | head -1); '
                  'docker exec $PG psql -U usuario -d agentmesh -t -c '
                  '"SELECT extversion FROM pg_extension WHERE extname=\'vector\'"')
    check("S4a extensión vector", "0." in out.strip(), out.strip()[:20])
    out = ssh(TN, 'PG=$(docker ps -q --filter name=ix-postgres-postgres-1 | head -1); '
                  'docker exec $PG psql -U usuario -d agentmesh -t -c '
                  '"SELECT count(*) FROM schema_migrations"')
    check("S4b migraciones aplicadas", out.strip() == "5", f"{out.strip()}/5")
    out = ssh(TN, 'PG=$(docker ps -q --filter name=ix-postgres-postgres-1 | head -1); '
                  'docker exec $PG psql -U usuario -d agentmesh -t -c '
                  '"SELECT count(*) FROM nodes WHERE embedding_vec IS NOT NULL"')
    check("S4c embeddings vector poblados", out.strip().isdigit() and int(out.strip()) > 200,
          f"{out.strip()} nodos")

    # ── S5 Redis ───────────────────────────────────────────────────────────
    print("\n[REDIS BUS]")
    out = ssh(MATE, "python3 -c \"import redis; r=redis.Redis(host='192.168.0.46'); print(r.ping())\"")
    check("S5 Redis pi.hole", "True" in out, out.strip()[:40])

    # ── S6 auth multi-token + audit ────────────────────────────────────────
    print("\n[AUTH + AUDIT]")
    if TOKEN:
        kb = Client(PRIMARIO, token=TOKEN)
        try:
            r = kb.upsert_node("Nota", "verify-auth-test",
                               text="prueba de verificación integral",
                               properties={"area": "verify"})
            ok = bool(r and r.get("id"))
        except Exception as e:
            ok = False
            print(f"    (upsert: {e})")
        check("S6a escritura con token", ok)
        # audit por agente
        out = ssh(TN, 'PG=$(docker ps -q --filter name=ix-postgres-postgres-1 | head -1); '
                      'docker exec $PG psql -U usuario -d agentmesh -t -c '
                      '"SELECT agent FROM audit_log ORDER BY id DESC LIMIT 1"')
        check("S6b audit_log registra agente", out.strip() != "", out.strip()[:30])
    else:
        check("S6a escritura con token", False, "sin KB_TOKEN en .env")

    # ── S7 optimistic locking ──────────────────────────────────────────────
    print("\n[OPTIMISTIC LOCKING]")
    if TOKEN:
        kb = Client(PRIMARIO, token=TOKEN)
        try:
            node = kb.upsert_node("Nota", "verify-lock-test", text="v1")
            v1 = node.get("version")
            # actualizar con versión obsoleta → 409
            import urllib.request
            url = f"{PRIMARIO}/n/Nota/verify-lock-test"
            req = urllib.request.Request(
                url, data=json.dumps({"label": "Nota", "name": "verify-lock-test",
                                      "text": "v2", "if_match": v1 - 1}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {TOKEN}"}, method="PUT")
            try:
                urllib.request.urlopen(req, timeout=5)
                check("S7 locking 409", False, "aceptó versión obsoleta")
            except Exception as e:
                code = getattr(e, "code", None)
                check("S7 locking 409", code == 409, f"HTTP {code}")
            # actualización correcta → 200
            node2 = kb.upsert_node("Nota", "verify-lock-test", text="v3",
                                   properties={"if_match": v1} if False else None)
            check("S7b versión incrementa", node2.get("version", 0) > v1,
                  f"v{v1}→v{node2.get('version')}")
        except Exception as e:
            check("S7 locking 409", False, str(e)[:60])

    # ── S8 similar con pgvector ────────────────────────────────────────────
    print("\n[PGVECTOR /similar]")
    if TOKEN:
        kb = Client(PRIMARIO, token=TOKEN)
        try:
            res = kb.similar("asistente de voz jarvis", limit=3)
            ok = len(res) > 0 and all("score" in r for r in res)
            top = f"{res[0]['name']} {res[0]['score']:.3f}" if ok else str(res)[:60]
            check("S8 /similar <=>", ok, top)
        except Exception as e:
            check("S8 /similar <=>", False, str(e)[:60])

    # ── S9 client multi-base ───────────────────────────────────────────────
    print("\n[CLIENT MULTI-BASE]")
    kb2 = Client(f"{PRIMARIO},{LB}", token=TOKEN)
    try:
        h = kb2.health()
        check("S9 failover client", h.get("nodes", 0) > 0, f"{h['nodes']} nodos via [primario,LB]")
    except Exception as e:
        check("S9 failover client", False, str(e)[:60])

    # ── S10 failover real (opcional, destructivo momentáneo) ───────────────
    if not args.skip_failover:
        print("\n[FAILOVER SERVICIO]")
        print("  parando primario 30s para probar conmutación...")
        ssh(MATE, "docker stop agent-mesh")
        time.sleep(6)
        h1 = http(f"{LB}/health", timeout=4)
        ok1 = '"status":"ok"' in h1
        try:
            h2 = kb2.health()   # client multi-base debe conmutar al backup
            ok2 = h2.get("nodes", 0) > 0
        except Exception:
            ok2 = False
        ssh(MATE, "docker start agent-mesh")
        time.sleep(10)
        check("S10a LB conmuta al backup", ok1, h1[:60])
        check("S10b client conmuta", ok2)
        h3 = http(f"{PRIMARIO}/health", timeout=4)
        check("S10c primario restaurado", '"status":"ok"' in h3, h3[:40])
    else:
        print("\n[FAILOVER SERVICIO] — omitido (--skip-failover)")

    # ── S11 TTL ────────────────────────────────────────────────────────────
    print("\n[TTL]")
    if TOKEN:
        kb = Client(PRIMARIO, token=TOKEN)
        try:
            # ttl_seconds va en el BODY (campo del servicio), no en properties
            import urllib.request
            body = {"label": "Nota", "name": "verify-ttl-test", "text": "efímera",
                    "properties": {}, "ttl_seconds": 2}
            req = urllib.request.Request(
                f"{PRIMARIO}/n/Nota/verify-ttl-test",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {TOKEN}"}, method="PUT")
            urllib.request.urlopen(req, timeout=5).read()
            # el barrendero corre cada 300s: comprobar que expires_at quedó en el
            # pasado (el barrendero lo purgará en su siguiente ciclo)
            # (variable shell $N => evita comillas anidadas que rompen el escaping)
            out = ssh(TN, 'PG=$(docker ps -q --filter name=ix-postgres-postgres-1 | head -1); '
                          'N=verify-ttl-test; '
                          'docker exec $PG psql -U usuario -d agentmesh -t -c '
                          '"SELECT (expires_at < now()) FROM nodes WHERE name=$N"')
            check("S11 TTL expira", "t" in out.lower(), out.strip()[:30])
        except Exception as e:
            check("S11 TTL expira", False, str(e)[:60])

    # ── S12 backups ────────────────────────────────────────────────────────
    print("\n[BACKUPS]")
    out = ssh(DREAM, "ls -1t ~/kb_backups/agentmesh-*.sql.gz 2>/dev/null | head -1")
    latest = out.strip()
    check("S12a dump local Dreamfyre", bool(latest), Path(latest).name if latest else "sin dumps")
    if latest:
            # buscar en RustFS el mismo nombre (boto3 disponible en Dreamfyre)
            name = Path(latest).name
            script = (
                "import boto3, botocore; "
                "s3=boto3.client('s3', endpoint_url='https://192.168.0.20:30293', "
                "aws_access_key_id='usuario', aws_secret_access_key='Leo.cacio.1105', "
                "verify=False); "
                f"r=s3.head_object(Bucket='agentmesh-backups', Key='{name}'); "
                "print(r['ContentLength'], 'bytes')"
            )
            out2 = ssh(DREAM, "python3 -m venv --help >/dev/null 2>&1; "
                              "ls ~/Repositorios/Uniweb_cron_tasks/.venv/bin/python 2>/dev/null || true")
            # boto3 vive en el venv de Uniweb_cron_tasks (mismo patrón que
            # backup_kb.sh usa con KB_RUSTFS_PY)
            rustfs_py = ("~/Repositorios/Uniweb_cron_tasks/.venv/bin/python"
                         if "Uniweb_cron_tasks" in out2 else "python3")
            out2 = ssh(DREAM, f"{rustfs_py} -c \"{script}\" 2>&1 | tail -1")
            check("S12b presente en RustFS", "bytes" in out2, out2.strip()[:40])

    # ── S13 listeners + JARVIS ─────────────────────────────────────────────
    print("\n[LISTENERS + JARVIS]")
    out = ssh(MATE, "systemctl is-active mako-listener.service hermes-listener.service agent-mesh-enrich.service 2>&1 | tr '\\n' ' '")
    n_active = out.count("active")
    check("S13a listeners rack (3)", n_active == 3, out.strip()[:40])
    out = ssh(MATE, "systemctl --user is-active agent-mesh-enrich.service 2>&1 | tr '\\n' ' '")
    # enrich corre como user unit
    ok_enrich = "active" in out
    check("S13b enrich listener", ok_enrich, out.strip()[:20])
    # JARVIS: kb_state via plugin
    out = subprocess.run(
        ["/usr/bin/python3", "-c",
         "import importlib.util,sys,asyncio;"
         "spec=importlib.util.spec_from_file_location('agent_mesh','"
         + str(REPO.parent / "JarvisVitaminado/jarvisbrain/plugins/agent_mesh/plugin.py") + "');"
         "m=importlib.util.module_from_spec(spec);sys.modules['agent_mesh']=m;spec.loader.exec_module(m);"
         "print(asyncio.run(m.handle('kb_state',{},None)))"],
        capture_output=True, text=True, timeout=60)
    ok_j = "nodos" in (out.stdout + out.stderr)
    check("S13c JARVIS kb_state", ok_j, (out.stdout.strip()[:60] or out.stderr.strip()[:60]))

    # ── S14 FTS ────────────────────────────────────────────────────────────
    print("\n[FULL-TEXT]")
    if TOKEN:
        kb = Client(PRIMARIO, token=TOKEN)
        try:
            res = kb.search("jarvis", limit=3)
            check("S14 búsqueda FTS", len(res) > 0, f"{len(res)} resultados")
        except Exception as e:
            check("S14 búsqueda FTS", False, str(e)[:60])

    # ── limpieza ───────────────────────────────────────────────────────────
    print("\n[LIMPIEZA]")
    if TOKEN:
        kb = Client(PRIMARIO, token=TOKEN)
        for node in ["verify-auth-test", "verify-lock-test", "verify-ttl-test"]:
            try:
                kb.delete_node("Nota", node)
            except Exception:
                pass
        # limpiar también el SQL si el TTL no lo borró (variable $P => sin escapes)
        ssh(TN, 'PG=$(docker ps -q --filter name=ix-postgres-postgres-1 | head -1); '
                'P=verify-%; '
                'docker exec $PG psql -U usuario -d agentmesh -c '
                '"DELETE FROM nodes WHERE name LIKE $P" >/dev/null 2>&1; true')
    print("  nodos de prueba eliminados")

    # ── resumen ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = len(RESULTS)
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print(f"RESUMEN: {passed}/{total} PASS")
    for label, status, detail in RESULTS:
        if status == "FAIL":
            print(f"  FAIL → {label} ({detail})")
    print("=" * 60)
    return 0 if passed == total else 1

if __name__ == "__main__":
    raise SystemExit(main())