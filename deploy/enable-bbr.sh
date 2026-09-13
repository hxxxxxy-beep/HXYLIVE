#!/bin/sh
# Enable Linux TCP BBR for WAN paths (idempotent). Soft-skips if unsupported.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root: sudo ./deploy/enable-bbr.sh' >&2
    exit 1
fi

if ! modprobe tcp_bbr 2>/dev/null; then
    printf '%s\n' 'WARN: tcp_bbr module unavailable; skipping BBR enablement.' >&2
    exit 0
fi

printf '%s\n' 'tcp_bbr' > /etc/modules-load.d/bbr.conf
cat > /etc/sysctl.d/99-bbr.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF

sysctl -w net.core.default_qdisc=fq >/dev/null
sysctl -w net.ipv4.tcp_congestion_control=bbr >/dev/null

cc="$(sysctl -n net.ipv4.tcp_congestion_control)"
if [ "$cc" != "bbr" ]; then
    printf 'ERROR: expected tcp_congestion_control=bbr, got %s\n' "$cc" >&2
    exit 1
fi

printf '%s\n' 'TCP BBR enabled (fq + bbr).'
