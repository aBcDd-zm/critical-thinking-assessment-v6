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

if ! printf '%s' "${ADMIN_TOKEN:-}" | grep -Eq '^[0-9A-Fa-f]{64}$'; then
  echo "ADMIN_TOKEN must be exactly 64 hexadecimal characters" >&2
  exit 1
fi

umask 077
mkdir -p /etc/nginx/auth
htpasswd -bcB /etc/nginx/auth/site.htpasswd "$SITE_BASIC_USER" "$site_password" >/dev/null
# Some Tencent Docker hosts use user-namespace remapping and reject chown even
# for container root.  Nginx only needs the bcrypt hash (never the plaintext
# password), so keep the directory searchable and the hash read-only instead.
chmod 0755 /etc/nginx/auth
chmod 0644 /etc/nginx/auth/site.htpasswd

for temp_dir in client proxy fastcgi uwsgi scgi; do
  mkdir -p "/tmp/nginx/$temp_dir"
  chmod 1777 "/tmp/nginx/$temp_dir"
done
