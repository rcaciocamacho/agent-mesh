#!/usr/bin/env bash
# Smoke test de agent-mesh spec 02 (F0-F6) contra un Postgres de prueba en Docker.
# Uso: bash scripts/smoke_test.sh   (requiere acceso al grupo docker)
# NO toca TrueNAS: levanta postgres:16 en :55432 y derriba todo al terminar.
set -euo pipefail
cd "$(dirname "$0")/.."

PG_CT="agent-mesh-smoke-pg"
SVC_PORT=18084
PG_IMAGE="${PG_IMAGE:-postgres:16}"
export KB_DSN="postgresql://kb:kb@127.0.0.1:55432/agentmesh"
export KB_TOKEN=""                 # auth OFF para el smoke test
export KB_TOKENS=""                # idem
export KB_REDIS_URL=""             # sin redis en el smoke test
export KB_TTL_SWEEP="${KB_TTL_SWEEP:-2}"  # barrendero rápido para validar TTL

cleanup() {
  echo "== cleanup =="
  docker rm -f "$PG_CT" >/dev/null 2>&1 || true
  pkill -f "uvicorn main:app --port $SVC_PORT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== 1. levantar postgres de prueba =="
docker rm -f "$PG_CT" >/dev/null 2>&1 || true
docker run -d --name "$PG_CT" -e POSTGRES_USER=kb -e POSTGRES_PASSWORD=kb \
  -e POSTGRES_DB=agentmesh -p 55432:5432 "$PG_IMAGE" >/dev/null
for i in $(seq 1 30); do
  docker exec "$PG_CT" pg_isready -U kb -d agentmesh >/dev/null 2>&1 && break
  sleep 1
done
echo "   postgres listo"

echo "== 2. arrancar el service =="
cd service
../.venv/bin/uvicorn main:app --host 127.0.0.1 --port $SVC_PORT >/tmp/agm_smoke.log 2>&1 &
SVC_PID=$!
cd ..
for i in $(seq 1 30); do
  curl -sf -m 2 "http://127.0.0.1:$SVC_PORT/health" >/dev/null 2>&1 && break
  sleep 1
done

echo "== 3. health =="
curl -s "http://127.0.0.1:$SVC_PORT/health"; echo

echo "== 4. migraciones (debe verse 5 filas, 1 skipped si sin pgvector) =="
docker exec "$PG_CT" psql -U kb -d agentmesh -c \
  "SELECT id, name, status FROM schema_migrations ORDER BY id"

echo "== 5. nodo durable + nodo con TTL =="
curl -s -X PUT "http://127.0.0.1:$SVC_PORT/n/Concepto/prueba" \
  -H 'Content-Type: application/json' \
  -d '{"label":"Concepto","name":"prueba","text":"nodo de smoke test"}'; echo
curl -s -X PUT "http://127.0.0.1:$SVC_PORT/n/Nota/efimera" \
  -H 'Content-Type: application/json' \
  -d '{"label":"Nota","name":"efimera","text":"esto expira","ttl_seconds":2}'; echo

echo "== 6. search + similar =="
curl -s "http://127.0.0.1:$SVC_PORT/search?q=smoke" | head -c 300; echo
curl -s -X POST "http://127.0.0.1:$SVC_PORT/similar?limit=3" \
  -H 'Content-Type: application/json' -d '{"text":"prueba de similitud"}' | head -c 400; echo

echo "== 7. optimistic locking (versión esperada 999 → 409) =="
code=$(curl -s -o /tmp/agm_conflict.json -w '%{http_code}' -X PUT \
  "http://127.0.0.1:$SVC_PORT/n/Concepto/prueba" \
  -H 'Content-Type: application/json' \
  -d '{"label":"Concepto","name":"prueba","text":"pisar","if_match":999}')
echo "   HTTP $code $(cat /tmp/agm_conflict.json)"

echo "== 8. version tras upsert normal =="
curl -s -X PUT "http://127.0.0.1:$SVC_PORT/n/Concepto/prueba" \
  -H 'Content-Type: application/json' \
  -d '{"label":"Concepto","name":"prueba","text":"v2","if_match":1}'; echo

echo "== 9. conversaciones =="
curl -s -X PUT "http://127.0.0.1:$SVC_PORT/conversations/ses-1" \
  -H 'Content-Type: application/json' \
  -d '{"resumen":"charla sobre el smoke test","lang":"es"}'; echo
curl -s "http://127.0.0.1:$SVC_PORT/conversations/ses-1"; echo

echo "== 10. TTL: esperar 6s (2 ciclos del barrendero KB_TTL_SWEEP=$KB_TTL_SWEEP) =="
sleep 6
echo "   buscar 'efimera' (debe estar vacío):"
curl -s "http://127.0.0.1:$SVC_PORT/search?q=efimera" | head -c 200; echo
docker exec "$PG_CT" psql -U kb -d agentmesh -t -c \
  "SELECT 'nodos_restantes='||count(*) FROM nodes"

echo "== 11. audit (si auth ON no aplica; aquí con agente legacy vacío) =="
docker exec "$PG_CT" psql -U kb -d agentmesh -c \
  "SELECT agent, action, detail->>'name' AS name FROM audit_log ORDER BY id"

echo "== 12. parar service =="
kill $SVC_PID 2>/dev/null || true
wait $SVC_PID 2>/dev/null || true
echo "SMOKE OK"
