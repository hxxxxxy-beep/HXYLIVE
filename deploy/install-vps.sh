#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run this script as root: sudo ./deploy/install-vps.sh' >&2
    exit 1
fi

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
EXPECTED_DIR="/opt/hxylive"

if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
    printf 'HXYLIVE must be cloned to %s (current: %s).\n' "$EXPECTED_DIR" "$PROJECT_DIR" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Docker is required. Install Docker Engine and the Compose plugin, then rerun.' >&2
    exit 1
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE_MODE="plugin"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_MODE="standalone"
else
    printf '%s\n' 'Docker Compose is required.' >&2
    exit 1
fi

if ! command -v nginx >/dev/null 2>&1; then
    printf '%s\n' 'Nginx is required. Install it, then rerun.' >&2
    exit 1
fi

cd "$PROJECT_DIR"
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    printf '%s\n' 'Created /opt/hxylive/.env.'
    printf '%s\n' 'Edit PASSWORD and Twitch credentials, then rerun this installer.' >&2
    exit 2
fi

mkdir -p /opt/hxylive/data/records
# Nginx X-Accel downloads need to traverse data/ and read records/; keep other
# data files root-owned. 755 on the data root is enough for path traversal.
chmod 755 /opt/hxylive /opt/hxylive/data /opt/hxylive/data/records
find /opt/hxylive/data/records -type d -exec chmod 755 {} + 2>/dev/null || true
find /opt/hxylive/data/records -type f -exec chmod 644 {} + 2>/dev/null || true

install -m 644 deploy/hxylive-nginx.conf /etc/nginx/sites-available/hxylive
ln -sfn /etc/nginx/sites-available/hxylive /etc/nginx/sites-enabled/hxylive
rm -f /etc/nginx/sites-enabled/default
nginx -t

if [ "$COMPOSE_MODE" = "plugin" ]; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi

nginx -s reload 2>/dev/null || systemctl restart nginx

sleep 3
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8080/ >/dev/null
printf '%s\n' 'HXYLIVE VPS deployment is healthy at http://127.0.0.1:8080/.'
