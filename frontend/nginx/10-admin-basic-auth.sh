#!/bin/sh
set -eu

case "${ADMIN_BASIC_USER:-}" in
  ""|*[!A-Za-z0-9._-]*)
    echo "ADMIN_BASIC_USER must contain only letters, digits, dot, underscore or dash" >&2
    exit 1
    ;;
esac

admin_password=${ADMIN_BASIC_PASSWORD:-}
if [ "${#admin_password}" -lt 16 ]; then
  echo "ADMIN_BASIC_PASSWORD must be at least 16 characters" >&2
  exit 1
fi

if ! printf '%s' "${ADMIN_API_TOKEN:-}" | grep -Eq '^[0-9A-Fa-f]{64}$'; then
  echo "ADMIN_API_TOKEN must be exactly 64 hexadecimal characters" >&2
  exit 1
fi

umask 077
mkdir -p /etc/nginx/auth
htpasswd -bcB /etc/nginx/auth/admin.htpasswd "$ADMIN_BASIC_USER" "$admin_password" >/dev/null
# Some Tencent Docker hosts use user-namespace remapping and reject chown even
# for container root.  Nginx only needs the bcrypt hash (never the plaintext
# password), so keep the directory searchable and the hash read-only instead.
chmod 0755 /etc/nginx/auth
chmod 0644 /etc/nginx/auth/admin.htpasswd

for temp_dir in client proxy fastcgi uwsgi scgi; do
  mkdir -p "/tmp/nginx/$temp_dir"
  chmod 1777 "/tmp/nginx/$temp_dir"
done
