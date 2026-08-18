#!/usr/bin/env bash
# Backup de la KB de agent-mesh (F6 del spec 02).
# pg_dump del Postgres de TrueNAS → fichero con rotación. NO borra nada en origen.
#
# Uso:  bash scripts/backup_kb.sh [/ruta/al/.env]
#   - Lee KB_DSN del .env del repo (o de $KB_DSN si está exportado).
#   - Backup a: $KB_BACKUP_DIR (por defecto ./backups/)
#   - Retención: $KB_BACKUP_KEEP (por defecto 7 backups; 0 = infinito)
#
# Para programarlo (ej. diario a las 4:00):
#   0 4 * * * cd ~/Desarrollo/Repositorios/agent-mesh && bash scripts/backup_kb.sh >> /tmp/kb_backup.log 2>&1
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-$REPO_DIR/.env}"
BACKUP_DIR="${KB_BACKUP_DIR:-$REPO_DIR/backups}"
KEEP="${KB_BACKUP_KEEP:-7}"
# pg_dump puede no estar en el host (p.ej. MateBook sin clientes postgres).
# Permitir override, p.ej.: KB_PG_DUMP="docker exec agent-mesh-pg pg_dump"
#   o KB_PG_DUMP="docker exec <contenedor> pg_dump"
PG_DUMP_CMD="${KB_PG_DUMP:-pg_dump}"

# resolver KB_DSN: variable de entorno > .env
if [[ -z "${KB_DSN:-}" && -f "$ENV_FILE" ]]; then
  export "$(grep -E '^KB_DSN=' "$ENV_FILE" | head -1)"
fi
if [[ -z "${KB_DSN:-}" ]]; then
  echo "ERROR: KB_DSN no definido (ni en entorno ni en $ENV_FILE)" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/agentmesh-$TS.sql.gz"

echo "== backup $TS =="
$PG_DUMP_CMD --no-owner --no-privileges "$KB_DSN" | gzip > "$OUT"
SIZE="$(du -h "$OUT" | cut -f1)"
echo "   ✓ $OUT ($SIZE)"

# rotación
if [[ "$KEEP" -gt 0 ]]; then
  COUNT="$(ls -1 "$BACKUP_DIR"/agentmesh-*.sql.gz 2>/dev/null | wc -l)"
  while [[ "$COUNT" -gt "$KEEP" ]]; do
    OLD="$(ls -1t "$BACKUP_DIR"/agentmesh-*.sql.gz | tail -1)"
    echo "   🧹 rotación: eliminando $OLD"
    rm -f "$OLD"
    COUNT="$((COUNT-1))"
  done
fi

echo "== fin =="