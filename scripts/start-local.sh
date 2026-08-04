#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
RUNTIME_DIR="$PROJECT_ROOT/.local-run"
BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
FRONTEND_PID_FILE="$RUNTIME_DIR/frontend.pid"
BACKEND_PORT=8060
FRONTEND_PORT=5176

# Keep this standalone V6 demo from silently inheriting a model selection made
# for another project in the parent shell.  Credentials still come from the
# local environment/.env; only the model name is scoped to V6.
export DEEPSEEK_MODEL="${V6_DEEPSEEK_MODEL:-deepseek-v4-flash}"

ensure_port_available() {
  local label="$1"
  local port="$2"
  local pid_file="$3"

  if [[ -f "$pid_file" ]] && kill -0 "$(<"$pid_file")" 2>/dev/null; then
    return
  fi
  if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    print -u2 "$label端口 $port 已被其他进程占用，未启动 V6。"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2
    exit 1
  fi
}

if [[ ! -x "$PROJECT_ROOT/backend/.venv/bin/uvicorn" ]]; then
  print -u2 "后端依赖尚未安装，请先运行 make setup-backend"
  exit 1
fi

if [[ ! -d "$PROJECT_ROOT/frontend/node_modules" ]]; then
  print -u2 "前端依赖尚未安装，请先运行 make setup-frontend"
  exit 1
fi

mkdir -p "$RUNTIME_DIR" "$PROJECT_ROOT/backend/data"

ensure_port_available "后端" "$BACKEND_PORT" "$BACKEND_PID_FILE"
ensure_port_available "前端" "$FRONTEND_PORT" "$FRONTEND_PID_FILE"

cd "$PROJECT_ROOT/backend"
.venv/bin/alembic upgrade head

if [[ -f "$BACKEND_PID_FILE" ]] && kill -0 "$(<"$BACKEND_PID_FILE")" 2>/dev/null; then
  print -u2 "后端已经在运行（PID $(<"$BACKEND_PID_FILE")）"
else
  nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" \
    > "$RUNTIME_DIR/backend.log" 2>&1 &
  print $! > "$BACKEND_PID_FILE"
fi

cd "$PROJECT_ROOT/frontend"
if [[ -f "$FRONTEND_PID_FILE" ]] && kill -0 "$(<"$FRONTEND_PID_FILE")" 2>/dev/null; then
  print -u2 "前端已经在运行（PID $(<"$FRONTEND_PID_FILE")）"
else
  nohup ./node_modules/.bin/vite --host 127.0.0.1 --port "$FRONTEND_PORT" \
    > "$RUNTIME_DIR/frontend.log" 2>&1 &
  print $! > "$FRONTEND_PID_FILE"
fi

for _ in {1..40}; do
  if curl --fail --silent "http://127.0.0.1:$BACKEND_PORT/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl --fail --silent "http://127.0.0.1:$BACKEND_PORT/api/v1/health" >/dev/null 2>&1; then
  print -u2 "后端未通过健康检查，请查看 $RUNTIME_DIR/backend.log"
  exit 1
fi

for _ in {1..40}; do
  if curl --fail --silent "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl --fail --silent "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null 2>&1; then
  print -u2 "前端未通过健康检查，请查看 $RUNTIME_DIR/frontend.log"
  exit 1
fi

print "思衡 V6 本地实验环境已启动："
print "  用户端 http://127.0.0.1:$FRONTEND_PORT/assessment"
print "  复核台 http://127.0.0.1:$FRONTEND_PORT/admin"
print "  API    http://127.0.0.1:$BACKEND_PORT/docs"
