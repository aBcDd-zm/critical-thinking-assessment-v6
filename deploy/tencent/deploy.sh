#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.production.yml"
ENV_FILE=${CTA_V6_ENV_FILE:-"$PROJECT_ROOT/.env.production"}
CADDY_CONTAINER=${CTA_V6_CADDY_CONTAINER:-tencent-caddy-1}
CADDYFILE_PATH=${CTA_V6_CADDYFILE_PATH:-/etc/caddy/Caddyfile}
SHARED_NETWORK=tencent_default
V6_HOSTNAME=thinkagent.asia

die() {
  echo "V6 deployment preflight failed: $1" >&2
  exit 1
}

require_value() {
  key=$1
  if ! grep -Eq "^${key}=.+$" "$ENV_FILE"; then
    die "${key} is missing or empty in .env.production"
  fi
  if grep -Eq "^${key}=REPLACE_" "$ENV_FILE"; then
    die "${key} still contains the example placeholder"
  fi
}

prepare_compose() {
  [ -f "$COMPOSE_FILE" ] || die "docker-compose.production.yml is missing"
  [ -r "$ENV_FILE" ] || die "create .env.production from .env.production.example on this server"
  command -v docker >/dev/null 2>&1 || die "Docker is not installed"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"

  file_mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || true)
  case "$file_mode" in
    400|600) ;;
    *) die ".env.production must be owned privately (run chmod 600 .env.production)" ;;
  esac

  for required_key in DEEPSEEK_API_KEY ADMIN_USERNAME ADMIN_PASSWORD_HASH ADMIN_JWT_SECRET VITE_RESEARCH_CONTACT VITE_DATA_RETENTION_NOTICE TTS_MODE; do
    require_value "$required_key"
  done

  tts_mode=$(sed -n 's/^TTS_MODE=//p' "$ENV_FILE" | tail -n 1)
  case "$tts_mode" in
    disabled) ;;
    doubao)
      for required_key in DOUBAO_TTS_API_KEY DOUBAO_TTS_RESOURCE_ID DOUBAO_TTS_SPEAKER; do
        require_value "$required_key"
      done
      ;;
    *) die "TTS_MODE must be doubao or disabled" ;;
  esac

  # Keep the backend's env file explicit when this script is run from another
  # directory. The file is consumed by Docker only; nothing below prints it.
  export CTA_V6_BACKEND_ENV_FILE="$ENV_FILE"
}

deploy_stack() {
  prepare_compose
  docker network inspect "$SHARED_NETWORK" >/dev/null 2>&1 || die "shared Docker network ${SHARED_NETWORK} is unavailable"

  docker compose --project-directory "$PROJECT_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet
  docker compose --project-directory "$PROJECT_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build

  attempt=1
  while [ "$attempt" -le 30 ]; do
    if curl --fail --silent --show-error --max-time 3 http://127.0.0.1:18060/healthz >/dev/null 2>&1; then
      if docker compose --project-directory "$PROJECT_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend \
        python -c "import urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8060/api/v1/health/db', timeout=3); assert response.status == 200" >/dev/null 2>&1; then
        echo "V6 containers are healthy on loopback http://127.0.0.1:18060."
        echo "Shared Caddy, DNS, TLS, firewall, and public routes were not changed."
        echo "After merging the V6 host block, run: $0 reload-caddy"
        return 0
      fi
    fi
    sleep 2
    attempt=$((attempt + 1))
  done

  echo "V6 containers did not become healthy. Inspect only the named V6 project; do not print .env.production." >&2
  return 1
}

reload_shared_caddy() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed"
  docker inspect "$CADDY_CONTAINER" >/dev/null 2>&1 || die "shared Caddy container ${CADDY_CONTAINER} is unavailable"
  docker inspect --format '{{json .NetworkSettings.Networks}}' "$CADDY_CONTAINER" | grep -Fq '"tencent_default"' \
    || die "shared Caddy container is not attached to ${SHARED_NETWORK}"
  docker exec "$CADDY_CONTAINER" test -r "$CADDYFILE_PATH" \
    || die "Caddyfile path is unreadable: ${CADDYFILE_PATH}"

  # Check the mounted source directly.  `caddy adapt` emits JSON where these
  # literals are not guaranteed to survive verbatim, so grepping its output can
  # reject a valid V6 route before Caddy gets a chance to validate it.
  docker exec "$CADDY_CONTAINER" sh -ceu \
    'grep -Fq "$2" "$1"' \
    sh "$CADDYFILE_PATH" "cta-v6-web:8080" \
    || die "the current Caddy source has no V6 cta-v6-web route"
  docker exec "$CADDY_CONTAINER" sh -ceu \
    'grep -Fq "$2" "$1"' \
    sh "$CADDYFILE_PATH" "$V6_HOSTNAME" \
    || die "the current Caddy source has no ${V6_HOSTNAME} host block"

  docker exec "$CADDY_CONTAINER" caddy validate --config "$CADDYFILE_PATH" --adapter caddyfile
  docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDYFILE_PATH" --adapter caddyfile
  echo "Shared Caddy reloaded after validating its existing configuration."
}

case ${1:-deploy} in
  deploy)
    deploy_stack
    ;;
  reload-caddy)
    reload_shared_caddy
    ;;
  *)
    echo "Usage: $0 [deploy|reload-caddy]" >&2
    exit 64
    ;;
esac
