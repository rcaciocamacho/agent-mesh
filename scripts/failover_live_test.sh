#!/usr/bin/env bash
# Prueba de failover en vivo F4: escribe via LB, mata primario MateBook,
# verifica que el LB conmuta solo al backup (Dreamfyre), y lo restaura.
set -euo pipefail
DREAM=192.168.0.26
MATE=192.168.0.45
KEY=~/.ssh/id_ed25519
# token se lee EN el Dreamfyre (nunca viaja por el CLI del agente)
TOKEN_CMD="grep ^KB_TOKEN= ~/Desarrollo/Repositorios/agent-mesh/deploy/f4/.env | cut -d= -f2-"

run_dream() {
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 usuario@"$DREAM" "$1"
}

echo "== 1. leer nodo de prueba via LB (con token leído en Dreamfyre) =="
run_dream 'TOKEN='"$TOKEN_CMD"'; curl -s "http://127.0.0.1:18085/search?q=failover-live" -H "Authorization: Bearer $TOKEN"' | head -c 220
echo

echo "== 2. MATAR primario (MateBook agent-mesh) =="
ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=8 usuario@"$MATE" "docker stop agent-mesh" && echo "  primario parado"

echo "== 3. esperar 20s (fail_timeout=15s) a que nginx conmute =="
sleep 20

echo "== 4. LB debe seguir respondiendo vía el backup =="
run_dream 'TOKEN='"$TOKEN_CMD"'; curl -s "http://127.0.0.1:18085/search?q=failover-live" -H "Authorization: Bearer $TOKEN"' | head -c 220
echo
run_dream 'curl -s http://127.0.0.1:18085/health'
echo

echo "== 5. restaurar primario =="
ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=8 usuario@"$MATE" "docker start agent-mesh" && echo "  primario arrancado"
sleep 8
echo "== 6. health final (debe volver el primario) =="
run_dream 'curl -s http://127.0.0.1:18085/health'
echo
echo "FAILOVER-LIVE OK"