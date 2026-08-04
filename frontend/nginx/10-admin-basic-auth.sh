#!/bin/sh
set -eu

case "${SITE_BASIC_USER:-}" in
  ""|*[!A-Za-z0-9._-]*)
    echo "SITE_BASIC_USER must contain only letters, digits, dot, underscore or dash" >&2
    exit 1
    ;;
esac

site_password=${SITE_BASIC_PASSWORD:-}
if [ "${#site_password}" -lt 16 ]; then
  echo "SITE_BASIC_PASSWORD must be at least 16 characters" >&2
  exit 1
fi

# This password gate applies only to /admin and /api/v1/admin.  It is kept in
# the runtime filesystem so no credential reaches the frontend bundle.
umask 077
mkdir -p /etc/nginx/auth
htpasswd -bcB /etc/nginx/auth/admin.htpasswd "$SITE_BASIC_USER" "$site_password" >/dev/null

# Docker user-namespace remapping can reject chown even for container root.
# Nginx only needs the bcrypt hash, so keep the directory searchable and the
# file read-only without attempting an ownership transition.
chmod 0755 /etc/nginx/auth
chmod 0644 /etc/nginx/auth/admin.htpasswd
