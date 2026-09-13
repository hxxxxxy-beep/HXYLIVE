#!/bin/sh
# Probe common TLS1.3 camouflage targets from this host and print ranked suggestions.
# Does not write secrets. Prefer confirming the top pick before install-reality.sh.
set -eu

CANDIDATES="
www.microsoft.com
www.cloudflare.com
www.apple.com
gateway.icloud.com
dl.google.com
"

printf '%s\n' 'Probing Reality camouflage destinations (SNI/dest) from this host...'

rank=0
rm -f /tmp/hxylive-reality-dest.recommend
for host in $CANDIDATES; do
    host=$(printf '%s' "$host" | tr -d '[:space:]')
    [ -n "$host" ] || continue
    if command -v timeout >/dev/null 2>&1; then
        # shellcheck disable=SC2086
        out="$(timeout 5 openssl s_client -connect "${host}:443" -servername "$host" -tls1_3 </dev/null 2>/dev/null || true)"
    else
        out="$(openssl s_client -connect "${host}:443" -servername "$host" -tls1_3 </dev/null 2>/dev/null || true)"
    fi
    if printf '%s' "$out" | grep -q 'BEGIN CERTIFICATE'; then
        rank=$((rank + 1))
        printf 'OK  %s  (suggest dest=%s:443 serverNames=%s)\n' "$host" "$host" "$host"
        if [ "$rank" -eq 1 ]; then
            printf '%s\n' "$host" > /tmp/hxylive-reality-dest.recommend
        fi
    else
        printf 'NO  %s\n' "$host"
    fi
done

if [ "$rank" -eq 0 ]; then
    printf '%s\n' 'No candidate passed TLS1.3 probe. Set REALITY_DEST manually (host only, port 443 assumed).' >&2
    exit 1
fi

printf '\n%s\n' 'Confirm one host with the user, then set REALITY_DEST in /opt/hxylive/.env and rerun install.'
if [ -f /tmp/hxylive-reality-dest.recommend ]; then
    printf 'Top recommendation: %s\n' "$(cat /tmp/hxylive-reality-dest.recommend)"
fi
