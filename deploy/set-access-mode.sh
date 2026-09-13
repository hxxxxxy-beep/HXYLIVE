#!/bin/sh
# Toggle HXYLIVE WAN access mode on the VPS.
#
#   reality  — VLESS-Reality on :443; public :8080 closed; Xray running
#   direct   — naked Nginx :8080 open; Reality stopped
#
# Cloud provider security groups must match (scripts configure UFW when available).
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root: sudo ./deploy/set-access-mode.sh reality|direct' >&2
    exit 1
fi

MODE="${1:-}"
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
EXPECTED_DIR="/opt/hxylive"
if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
    printf 'HXYLIVE must be cloned to %s (current: %s).\n' "$EXPECTED_DIR" "$PROJECT_DIR" >&2
    exit 1
fi

REALITY_DIR="$PROJECT_DIR/reality"
MODE_FILE="$REALITY_DIR/access-mode"
mkdir -p "$REALITY_DIR"
chmod 700 "$REALITY_DIR"

ensure_ufw() {
    if ! command -v ufw >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y
        apt-get install -y ufw
    fi
}

apply_ufw_reality() {
    ensure_ufw
    ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
    ufw delete allow 8080/tcp >/dev/null 2>&1 || true
    ufw deny 8080/tcp >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true
    printf '%s\n' 'UFW: allow 22 + 443; deny 8080 (reality mode).'
    printf '%s\n' 'Also set the cloud security group to TCP 22 + 443 only (no public 8080).'
}

apply_ufw_direct() {
    ensure_ufw
    ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
    ufw allow 8080/tcp >/dev/null 2>&1 || true
    ufw delete deny 8080/tcp >/dev/null 2>&1 || true
    ufw delete allow 443/tcp >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true
    printf '%s\n' 'UFW: allow 22 + 8080; Reality port 443 closed (direct mode).'
    printf '%s\n' 'Also set the cloud security group to TCP 22 + 8080.'
}

case "$MODE" in
    reality)
        if [ ! -f /usr/local/etc/xray/config.json ] && [ ! -f "$REALITY_DIR/config.json" ]; then
            printf '%s\n' 'Reality is not installed. Set REALITY_DEST and run deploy/install-reality.sh first.' >&2
            exit 1
        fi
        if [ -f "$REALITY_DIR/config.json" ]; then
            install -d -m 755 /usr/local/etc/xray
            install -m 600 "$REALITY_DIR/config.json" /usr/local/etc/xray/config.json
        fi
        systemctl enable xray >/dev/null 2>&1 || true
        systemctl restart xray
        apply_ufw_reality
        printf '%s\n' reality >"$MODE_FILE"
        chmod 644 "$MODE_FILE"
        printf '%s\n' 'Access mode: reality (no public naked :8080; use Mac Clash VLESS-Reality → http://<VPS_IP>:8080).'
        ;;
    direct)
        systemctl stop xray >/dev/null 2>&1 || true
        systemctl disable xray >/dev/null 2>&1 || true
        apply_ufw_direct
        printf '%s\n' direct >"$MODE_FILE"
        chmod 644 "$MODE_FILE"
        printf '%s\n' 'Access mode: direct (public :8080 open; Reality stopped). Prefer reality for China Mac bulk downloads.'
        ;;
    *)
        printf '%s\n' "Usage: $0 reality|direct" >&2
        if [ -f "$MODE_FILE" ]; then
            printf 'Current mode: %s\n' "$(cat "$MODE_FILE")"
        fi
        exit 1
        ;;
esac
