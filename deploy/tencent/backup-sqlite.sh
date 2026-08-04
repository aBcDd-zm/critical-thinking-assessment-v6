#!/bin/sh
set -eu

echo "Refusing backup operation: V6 has no authorized deployment datastore." >&2
echo "Use the local acceptance workflow; do not point this script at V5 or production data." >&2
exit 64
