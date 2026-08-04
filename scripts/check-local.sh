#!/bin/zsh
set -euo pipefail

mode="${1:-running}"
BACKEND_URL="http://127.0.0.1:8060/api/v1/health"
FRONTEND_URL="http://127.0.0.1:5176/assessment"

case "$mode" in
  running)
    curl --fail --silent --show-error "$BACKEND_URL" >/dev/null
    curl --fail --silent --show-error "$FRONTEND_URL" >/dev/null
    print "V6 健康检查通过：8060 / 5176 均可访问。"
    ;;
  stopped)
    for port in 8060 5176; do
      if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        print -u2 "端口 $port 仍有监听进程："
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2
        exit 1
      fi
    done
    print "V6 停止检查通过：8060 / 5176 均已关闭。"
    ;;
  *)
    print -u2 "用法：$0 running|stopped"
    exit 2
    ;;
esac
