#!/bin/sh
set -eu

# The Tencent host runs Docker with user-namespace remapping.  These paths
# must exist on the writable /tmp tmpfs before Nginx starts.
for temp_dir in client proxy fastcgi uwsgi scgi; do
  mkdir -p "/tmp/nginx/$temp_dir"
  chmod 1777 "/tmp/nginx/$temp_dir"
done
