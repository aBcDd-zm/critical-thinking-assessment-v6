FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY index.html tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts ./
COPY src ./src

ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM nginx:1.27-alpine

RUN apk add --no-cache apache2-utils

# The Tencent host runs Docker with user-namespace remapping, so changing
# tmpfs ownership from container root to nginx (uid 101) is forbidden. Keep
# Nginx workers at container uid 0 so no ownership transition is attempted;
# all capabilities are dropped except process uid/gid setup, the filesystem is
# read-only, and the container has no host mounts or public host port.
RUN sed -i 's/^user  nginx;/user  root;/' /etc/nginx/nginx.conf

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/10-admin-basic-auth.sh /docker-entrypoint.d/10-admin-basic-auth.sh
COPY --from=build /app/dist /usr/share/nginx/html
RUN chmod 0755 /docker-entrypoint.d/10-admin-basic-auth.sh

EXPOSE 8080
