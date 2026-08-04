#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
RUNTIME_DIR="$PROJECT_ROOT/.local-run"

stop_exact_process() {
  local label="$1"
  local pid_file="$2"
  local expected="$3"

  if [[ ! -f "$pid_file" ]]; then
    return
  fi

  local pid="$(<"$pid_file")"
  if [[ "$pid" != <-> ]]; then
    print -u2 "$label PID 文件无效，未执行停止操作：$pid_file"
    return
  fi

  if kill -0 "$pid" 2>/dev/null; then
    local command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$command_line" == *"$expected"* ]]; then
      kill "$pid"
      for _ in {1..30}; do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 0.1
      done
      if kill -0 "$pid" 2>/dev/null; then
        print -u2 "$label 已发送停止信号，但进程仍未退出（PID $pid）"
        return 1
      fi
      print "$label 已停止（PID $pid）"
    else
      print -u2 "$label PID 已被其他进程占用，未执行停止操作：$command_line"
      return
    fi
  fi

  rm -f "$pid_file"
}

stop_exact_process "前端" "$RUNTIME_DIR/frontend.pid" "--port 5176"
stop_exact_process "后端" "$RUNTIME_DIR/backend.pid" "--port 8060"
