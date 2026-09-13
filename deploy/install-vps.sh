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

# Install git/curl/nginx/Docker on clean Debian/Ubuntu when missing.
sh "$PROJECT_DIR/deploy/bootstrap-host.sh"

if docker compose version >/dev/null 2>&1; then
    COMPOSE_MODE="plugin"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_MODE="standalone"
else
    printf '%s\n' 'Docker Compose is required after bootstrap.' >&2
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

# WAN TCP congestion control (persists across reboot; soft-skips if unsupported).
sh "$PROJECT_DIR/deploy/enable-bbr.sh"

mkdir -p /opt/hxylive/data/records /opt/hxylive/staging/records
# Nginx X-Accel downloads need to traverse data/ and read records/; keep other
# data files root-owned. 755 on the data root is enough for path traversal.
chmod 755 /opt/hxylive /opt/hxylive/data /opt/hxylive/data/records /opt/hxylive/staging /opt/hxylive/staging/records
find /opt/hxylive/data/records -type d -exec chmod 755 {} + 2>/dev/null || true
find /opt/hxylive/data/records -type f -exec chmod 644 {} + 2>/dev/null || true
find /opt/hxylive/staging/records -type d -exec chmod 755 {} + 2>/dev/null || true
find /opt/hxylive/staging/records -type f -exec chmod 644 {} + 2>/dev/null || true

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

# Access plane: default VLESS-Reality (public :8080 closed). Set ENABLE_REALITY=0 for naked :8080.
get_env() {
    key="$1"
    default="${2:-}"
    val="$(grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- | tr -d '\r' | sed 's/^"//;s/"$//')" || true
    if [ -z "$val" ]; then
        printf '%s' "$default"
    else
        printf '%s' "$val"
    fi
}

ENABLE_REALITY="$(get_env ENABLE_REALITY 1)"
case "$ENABLE_REALITY" in
    0|false|FALSE|no|NO|off|OFF)
        sh "$PROJECT_DIR/deploy/set-access-mode.sh" direct
        printf '%s\n' 'Access mode: direct (ENABLE_REALITY off).'
        ;;
    *)
        sh "$PROJECT_DIR/deploy/install-reality.sh"
        sh "$PROJECT_DIR/deploy/set-access-mode.sh" reality
        printf '%s\n' 'Access mode: reality. Mac: apply reality/clash-meta-mixin.yaml via mac-helper/apply-clash-reality-mixin.sh (:7897).'
        printf '%s\n' 'Toggle later: sudo ./deploy/set-access-mode.sh direct|reality'
        ;;
esac
