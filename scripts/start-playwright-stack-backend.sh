#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
TEMP_ROOT="$(mktemp -d /tmp/siheng-v6-playwright-stack.XXXXXX)"
BACKEND_PID=""

cleanup() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  case "$TEMP_ROOT" in
    /tmp/siheng-v6-playwright-stack.*) rm -rf -- "$TEMP_ROOT" ;;
    *) print -u2 "拒绝清理非预期临时目录：$TEMP_ROOT" ;;
  esac
}
trap cleanup EXIT INT TERM HUP

if [[ ! -x "$PROJECT_ROOT/backend/.venv/bin/uvicorn" ]]; then
  print -u2 "后端依赖尚未安装，请先运行 make setup-backend"
  exit 1
fi

export DATABASE_URL="sqlite:///$TEMP_ROOT/v6-playwright.db"
export AUTO_CREATE_DB="false"
export MODEL_GATEWAY_MODE="mock"
export DEEPSEEK_API_KEY=""
export TTS_MODE="fake"
export DOUBAO_TTS_API_KEY=""
export ADMIN_TOKEN=""
export CORS_ORIGINS='["http://127.0.0.1:5177"]'

cd "$PROJECT_ROOT/backend"
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8061 &
BACKEND_PID="$!"
wait "$BACKEND_PID"
