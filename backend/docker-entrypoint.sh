#!/bin/sh
set -eu
umask 077

if [ "${DATABASE_URL:-}" != "sqlite:////app/data/v6.db" ]; then
  echo "Refusing production start: DATABASE_URL must be sqlite:////app/data/v6.db" >&2
  exit 1
fi

if [ "${MODEL_GATEWAY_MODE:-}" != "real" ]; then
  echo "Refusing production start: MODEL_GATEWAY_MODE must be real" >&2
  exit 1
fi

if [ "${APP_ENV:-}" != "production" ]; then
  echo "Refusing production start: APP_ENV must be production" >&2
  exit 1
fi

if [ -z "${DEEPSEEK_API_KEY:-}" ] || [ -z "${ADMIN_TOKEN:-}" ]; then
  echo "Refusing production start: required server credentials are missing" >&2
  exit 1
fi

if [ "${TTS_MODE:-disabled}" = "fake" ]; then
  echo "Refusing production start: TTS_MODE must not be fake" >&2
  exit 1
fi

mkdir -p /app/data
alembic upgrade head

# The database contains participant free text. Keep it and SQLite sidecars
# private even when a restored bind mount arrived with broader permissions.
for sqlite_file in /app/data/v6.db /app/data/v6.db-wal /app/data/v6.db-shm; do
  if [ -e "$sqlite_file" ]; then
    chmod 600 "$sqlite_file"
  fi
done

exec "$@"
