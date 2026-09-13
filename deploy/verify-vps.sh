#!/bin/sh
set -eu

PROJECT_DIR="${HXYLIVE_PROJECT_DIR:-/opt/hxylive}"

test -f "$PROJECT_DIR/docker-compose.yml"
test -f "$PROJECT_DIR/.env"
test -d "$PROJECT_DIR/data"
docker inspect hxylive >/dev/null
docker inspect flaresolverr >/dev/null
nginx -t
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8080/api/version

# TCP BBR when the kernel supports it (installed by deploy/enable-bbr.sh).
KERNEL_RELEASE="$(uname -r)"
if sysctl net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -qw bbr \
    || [ -e "/lib/modules/$KERNEL_RELEASE/kernel/net/ipv4/tcp_bbr.ko" ] \
    || [ -e "/lib/modules/$KERNEL_RELEASE/kernel/net/ipv4/tcp_bbr.ko.xz" ]; then
    test "$(sysctl -n net.ipv4.tcp_congestion_control)" = "bbr"
    test "$(sysctl -n net.core.default_qdisc)" = "fq"
    test -f /etc/sysctl.d/99-bbr.conf
    test -f /etc/modules-load.d/bbr.conf
fi

# Access mode: reality (default) vs direct naked :8080.
MODE_FILE="$PROJECT_DIR/reality/access-mode"
MODE="direct"
if [ -f "$MODE_FILE" ]; then
    MODE="$(tr -d '[:space:]' <"$MODE_FILE")"
fi
case "$MODE" in
    reality)
        systemctl is-active --quiet xray
        ss -lnt | grep -qE '[:.]443 '
        # Public :8080 must be denied when UFW is active.
        if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then
            ufw status | grep -qE '443/tcp.*ALLOW'
            if ufw status numbered 2>/dev/null | grep -q '8080/tcp'; then
                ufw status | grep '8080/tcp' | grep -qi 'DENY\|REJECT'
            fi
        fi
        test -f "$PROJECT_DIR/reality/clash-meta-mixin.yaml"
        test -f "$PROJECT_DIR/reality/clash-meta-client.yaml"
        printf '%s\n' 'Access mode reality: Xray :443 up; Mac: apply reality/clash-meta-mixin.yaml (Merge overlay).'
        ;;
    direct)
        if systemctl is-active --quiet xray 2>/dev/null; then
            printf '%s\n' 'WARN: access-mode is direct but xray is still active.' >&2
        fi
        printf '%s\n' 'Access mode direct: public :8080 expected (provider security group + UFW).'
        ;;
    *)
        printf 'Unknown access mode in %s: %s\n' "$MODE_FILE" "$MODE" >&2
        exit 1
        ;;
esac

printf '\n%s\n' 'HXYLIVE VPS verification passed.'
