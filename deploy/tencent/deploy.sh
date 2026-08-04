#!/bin/sh
set -eu

echo "Refusing deployment: 思衡 V6 is a local-only experimental Demo." >&2
echo "No server, Docker, Caddy, DNS, database, backup, or secret action is authorized from this workspace." >&2
exit 64
