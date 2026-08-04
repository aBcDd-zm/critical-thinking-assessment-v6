#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.production.yml"
ENV_FILE=${CTA_V6_ENV_FILE:-"$PROJECT_ROOT/.env.production"}
BACKUP_DIR="$PROJECT_ROOT/backups"

die() {
  echo "V6 SQLite backup failed: $1" >&2
  exit 1
}

[ -r "$ENV_FILE" ] || die ".env.production is unavailable"
command -v docker >/dev/null 2>&1 || die "Docker is not installed"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"

file_mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || true)
case "$file_mode" in
  400|600) ;;
  *) die ".env.production must be owned privately (run chmod 600 .env.production)" ;;
esac

export CTA_V6_BACKEND_ENV_FILE="$ENV_FILE"
docker compose --project-directory "$PROJECT_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet

container_id=$(docker compose --project-directory "$PROJECT_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps -q backend)
[ -n "$container_id" ] || die "the V6 backend container is not running"

mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
snapshot_path="/tmp/cta-v6-backup-${timestamp}.db"
backup_path="$BACKUP_DIR/cta-v6-${timestamp}.sqlite3"

# sqlite3.Connection.backup creates a consistent snapshot without stopping the
# interview service. The temporary source is inside the backend's tmpfs, not
# the persistent database volume.
docker compose --project-directory "$PROJECT_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend \
  python - "$snapshot_path" <<'PY'
import sqlite3
import sys

source = sqlite3.connect("file:/app/data/v6.db?mode=ro", uri=True)
destination = sqlite3.connect(sys.argv[1])
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY

docker cp "$container_id:$snapshot_path" "$backup_path"
docker compose --project-directory "$PROJECT_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend \
  sh -ceu 'rm -f -- "$1"' sh "$snapshot_path"

[ -s "$backup_path" ] || die "the snapshot file is empty"
chmod 0600 "$backup_path"
sha256sum "$backup_path"
echo "Backup written to $backup_path"
