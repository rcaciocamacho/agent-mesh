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

# resolver KB_DSN y credenciales RustFS: variable de entorno > .env
if [[ -f "$ENV_FILE" ]]; then
  for VAR in KB_DSN KB_RUSTFS_ENDPOINT KB_RUSTFS_KEY KB_RUSTFS_SECRET \
             KB_RUSTFS_BUCKET KB_RUSTFS_KEEP_DAYS; do
    if [[ -z "${!VAR:-}" ]]; then
      VAL="$(grep -E "^$VAR=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
      [[ -n "$VAL" ]] && export "$VAR=$VAL"
    fi
  done
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

# ── sync a RustFS (S3 en TrueNAS) ───────────────────────────────────────────
# Si KB_RUSTFS_ENDPOINT/BUCKET están definidos, sube los backups del directorio
# al bucket y limpia los antiguos (mismo patrón que neurovegatea-sync).
# Credenciales desde .env; nunca hardcodeadas.
sync_rustfs() {
  local PY_BIN="${KB_RUSTFS_PY:-python3}"
  local KEEP_DAYS="${KB_RUSTFS_KEEP_DAYS:-90}"
  if [[ -z "${KB_RUSTFS_ENDPOINT:-}" || -z "${KB_RUSTFS_BUCKET:-}" ]]; then
    echo "   (RustFS no configurado — salto)"
    return 0
  fi
  if ! "$PY_BIN" -c "import boto3" 2>/dev/null; then
    echo "   ⚠️ boto3 no disponible en $PY_BIN — salto RustFS"
    return 0
  fi
  echo "== sync RustFS → $KB_RUSTFS_BUCKET =="
  "$PY_BIN" - "$BACKUP_DIR" "$KB_RUSTFS_ENDPOINT" "$KB_RUSTFS_KEY" \
            "$KB_RUSTFS_SECRET" "$KB_RUSTFS_BUCKET" "$KEEP_DAYS" <<'PY' 2>>"$LOG_FILE"
import boto3, urllib3, os, sys, glob
from datetime import datetime, timezone, timedelta
urllib3.disable_warnings()
backup_dir, endpoint, key, secret, bucket, keep_days = sys.argv[1:7]
s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=key,
                  aws_secret_access_key=secret,
                  config=boto3.session.Config(signature_version="s3v4"),
                  verify=False)
try:
    s3.head_bucket(Bucket=bucket)
except Exception:
    s3.create_bucket(Bucket=bucket)
    print(f"created bucket {bucket}")
for f in glob.glob(os.path.join(backup_dir, "agentmesh-*.sql.gz")):
    key_name = os.path.basename(f)
    s3.upload_file(f, bucket, key_name)
    print(f"uploaded {key_name}")
cutoff = datetime.now(timezone.utc) - timedelta(days=int(keep_days))
deleted = 0
for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
    if obj["LastModified"] < cutoff:
        s3.delete_object(Bucket=bucket, Key=obj["Key"])
        deleted += 1
print(f"cleaned {deleted} old backup(s)")
PY
  if [[ $? -eq 0 ]]; then
    echo "   ✓ RustFS sync OK"
  else
    echo "   ⚠️ sync RustFS falló (revisar $LOG_FILE)"
  fi
}

LOG_FILE="/tmp/kb_backup.log"
sync_rustfs