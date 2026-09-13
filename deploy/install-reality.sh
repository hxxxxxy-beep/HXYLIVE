#!/bin/sh
# Install Xray-core VLESS-Reality on the HXYLIVE VPS (host, not container).
# Secrets live under /opt/hxylive/reality/ (gitignored). Requires REALITY_DEST in .env.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root: sudo ./deploy/install-reality.sh' >&2
    exit 1
fi

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
EXPECTED_DIR="/opt/hxylive"
if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
    printf 'HXYLIVE must be cloned to %s (current: %s).\n' "$EXPECTED_DIR" "$PROJECT_DIR" >&2
    exit 1
fi

ENV_FILE="$PROJECT_DIR/.env"
REALITY_DIR="$PROJECT_DIR/reality"
CONFIG_JSON="$REALITY_DIR/config.json"
CLIENT_YAML="$REALITY_DIR/clash-meta-client.yaml"
CLIENT_MIXIN="$REALITY_DIR/clash-meta-mixin.yaml"
CLIENT_TXT="$REALITY_DIR/client-share.txt"
CLIENT_MAC_README="$REALITY_DIR/MAC_CLASH.txt"
STATE_DIR="$REALITY_DIR"

if [ ! -f "$ENV_FILE" ]; then
    printf '%s\n' 'Missing /opt/hxylive/.env' >&2
    exit 1
fi

# Load selected keys from .env without sourcing arbitrary shell.
get_env() {
    key="$1"
    default="${2:-}"
    val="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '\r' | sed 's/^"//;s/"$//')" || true
    if [ -z "$val" ]; then
        printf '%s' "$default"
    else
        printf '%s' "$val"
    fi
}

ENABLE_REALITY="$(get_env ENABLE_REALITY 1)"
REALITY_DEST="$(get_env REALITY_DEST "")"
REALITY_PUBLIC_HOST="$(get_env REALITY_PUBLIC_HOST "")"
REALITY_SHORT_ID="$(get_env REALITY_SHORT_ID "")"
REALITY_UUID="$(get_env REALITY_UUID "")"

case "$ENABLE_REALITY" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *)
        printf '%s\n' 'ENABLE_REALITY is off; skip Reality install. Use deploy/set-access-mode.sh direct.'
        exit 0
        ;;
esac

if [ -z "$REALITY_DEST" ]; then
    printf '%s\n' 'REALITY_DEST is empty. Run deploy/recommend-reality-dest.sh, confirm a host with the user, set REALITY_DEST in .env, then rerun.' >&2
    if [ -x "$PROJECT_DIR/deploy/recommend-reality-dest.sh" ]; then
        sh "$PROJECT_DIR/deploy/recommend-reality-dest.sh" || true
    fi
    exit 2
fi

# Strip scheme/port if the user pasted a URL-ish value.
REALITY_DEST="$(printf '%s' "$REALITY_DEST" | sed -E 's#^https?://##; s#/.*##; s#:.*##')"

ensure_openssl() {
    if ! command -v openssl >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y
        apt-get install -y openssl ca-certificates curl unzip
    fi
}

ensure_openssl

install_xray() {
    if command -v xray >/dev/null 2>&1; then
        printf '%s\n' 'Xray already installed.'
        return 0
    fi
    printf '%s\n' 'Installing Xray-core...'
    tmp="$(mktemp -d)"
    # Official install script from XTLS; runs on the VPS (not via Mac proxy).
    if ! curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o "$tmp/install-release.sh"; then
        printf '%s\n' 'Failed to download Xray install script from GitHub.' >&2
        rm -rf "$tmp"
        exit 1
    fi
    sh "$tmp/install-release.sh" install
    rm -rf "$tmp"
    if ! command -v xray >/dev/null 2>&1; then
        printf '%s\n' 'Xray install failed: binary missing.' >&2
        exit 1
    fi
}

install_xray

mkdir -p "$REALITY_DIR"
chmod 700 "$REALITY_DIR"

if [ -z "$REALITY_UUID" ]; then
    REALITY_UUID="$(xray uuid)"
fi
if [ -z "$REALITY_SHORT_ID" ]; then
    REALITY_SHORT_ID="$(openssl rand -hex 8)"
fi

KEY_FILE="$REALITY_DIR/x25519.json"
if [ -f "$REALITY_DIR/private.key" ] && [ -f "$REALITY_DIR/public.key" ]; then
    PRIVATE_KEY="$(cat "$REALITY_DIR/private.key")"
    PUBLIC_KEY="$(cat "$REALITY_DIR/public.key")"
else
    # xray x25519 prints PrivateKey / Password(Public) lines (format varies by version).
    xray x25519 >"$KEY_FILE"
    PRIVATE_KEY="$(grep -iE '^Private( key|Key)?:' "$KEY_FILE" | head -n1 | awk '{print $NF}' | tr -d '\r')"
    # Newer Xray prints public material as "Password:"; older builds use "Public key:".
    PUBLIC_KEY="$(grep -iE '^(Public( key|Key)?|Password):' "$KEY_FILE" | head -n1 | awk '{print $NF}' | tr -d '\r')"
    if [ -z "$PRIVATE_KEY" ] || [ -z "$PUBLIC_KEY" ]; then
        printf '%s\n' 'Failed to parse xray x25519 output.' >&2
        cat "$KEY_FILE" >&2
        exit 1
    fi
    printf '%s\n' "$PRIVATE_KEY" >"$REALITY_DIR/private.key"
    printf '%s\n' "$PUBLIC_KEY" >"$REALITY_DIR/public.key"
    chmod 600 "$REALITY_DIR/private.key" "$REALITY_DIR/public.key"
fi

if [ -z "$REALITY_PUBLIC_HOST" ]; then
    REALITY_PUBLIC_HOST="$(curl -4 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)"
fi
if [ -z "$REALITY_PUBLIC_HOST" ]; then
    REALITY_PUBLIC_HOST="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
fi
if [ -z "$REALITY_PUBLIC_HOST" ]; then
    printf '%s\n' 'Could not autodetect public IP. Set REALITY_PUBLIC_HOST in .env' >&2
    exit 1
fi

umask 077
cat >"$CONFIG_JSON" <<EOF
{
  "log": {
    "loglevel": "warning"
  },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": 443,
      "protocol": "vless",
      "settings": {
        "clients": [
          {
            "id": "${REALITY_UUID}",
            "flow": "xtls-rprx-vision"
          }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${REALITY_DEST}:443",
          "xver": 0,
          "serverNames": [
            "${REALITY_DEST}"
          ],
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": [
            "${REALITY_SHORT_ID}"
          ]
        }
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls", "quic"]
      }
    }
  ],
  "outbounds": [
    {
      "protocol": "freedom",
      "tag": "direct"
    }
  ]
}
EOF

# Point systemd unit at our config when using Xray-install defaults.
install -d -m 755 /usr/local/etc/xray
install -m 600 "$CONFIG_JSON" /usr/local/etc/xray/config.json

if ! xray -test -config /usr/local/etc/xray/config.json; then
    printf '%s\n' 'Xray config test failed.' >&2
    exit 1
fi

systemctl enable xray >/dev/null 2>&1 || true
systemctl restart xray

# Persist non-secret deploy hints for toggles / docs (UUID is secret — keep in reality/ only).
cat >"$STATE_DIR/install-meta.env" <<EOF
REALITY_DEST=${REALITY_DEST}
REALITY_PUBLIC_HOST=${REALITY_PUBLIC_HOST}
REALITY_SHORT_ID=${REALITY_SHORT_ID}
EOF
chmod 600 "$STATE_DIR/install-meta.env"
printf '%s\n' "$REALITY_UUID" >"$STATE_DIR/uuid"
chmod 600 "$STATE_DIR/uuid"

# Prefer IP-CIDR for numeric hosts; DOMAIN for hostnames (Clash Verge Merge / mihomo).
PROXY_ENTRY=$(cat <<EOF
  - name: hxylive-reality
    type: vless
    server: ${REALITY_PUBLIC_HOST}
    port: 443
    uuid: ${REALITY_UUID}
    network: tcp
    tls: true
    udp: true
    flow: xtls-rprx-vision
    servername: ${REALITY_DEST}
    reality-opts:
      public-key: ${PUBLIC_KEY}
      short-id: ${REALITY_SHORT_ID}
    client-fingerprint: chrome
EOF
)

RULE_LINES=""
if printf '%s' "$REALITY_PUBLIC_HOST" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    RULE_LINES="  - IP-CIDR,${REALITY_PUBLIC_HOST}/32,hxylive-reality,no-resolve"
elif printf '%s' "$REALITY_PUBLIC_HOST" | grep -Eq ':'; then
    RULE_LINES="  - IP-CIDR,${REALITY_PUBLIC_HOST}/128,hxylive-reality,no-resolve"
else
    RULE_LINES="  - DOMAIN,${REALITY_PUBLIC_HOST},hxylive-reality"
fi

# Raw node snippet (share / manual paste). Prefer clash-meta-mixin.yaml on the Mac.
cat >"$CLIENT_YAML" <<EOF
# HXYLIVE VLESS-Reality node only (Clash Meta / mihomo).
# Prefer clash-meta-mixin.yaml + mac-helper/apply-clash-reality-mixin.sh so airport
# subscription refresh does not wipe this route.
proxies:
${PROXY_ENTRY}
EOF
chmod 600 "$CLIENT_YAML"

# Clash Verge Rev / Mihomo Party Merge overlay — survives remote subscription refresh.
cat >"$CLIENT_MIXIN" <<EOF
# HXYLIVE Clash Merge overlay (Clash Verge Rev "Merge" / Mihomo Party override)
# Do NOT replace your airport profile with this file.
# Apply once via: mac-helper/apply-clash-reality-mixin.sh ./clash-meta-mixin.yaml
# Then keep enabling this Merge while you re-import / refresh the airport subscription.
#
# Effect: only traffic to ${REALITY_PUBLIC_HOST} uses hxylive-reality; other traffic stays on the airport.

prepend-rules:
${RULE_LINES}

prepend-rule-providers: {}

prepend-proxies:
${PROXY_ENTRY}

prepend-proxy-providers: {}

prepend-proxy-groups: []

append-rules: []

append-rule-providers: {}

append-proxies: []

append-proxy-providers: {}

append-proxy-groups: []
EOF
chmod 600 "$CLIENT_MIXIN"

SHARE="vless://${REALITY_UUID}@${REALITY_PUBLIC_HOST}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${REALITY_DEST}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${REALITY_SHORT_ID}&type=tcp#hxylive-reality"
printf '%s\n' "$SHARE" >"$CLIENT_TXT"
chmod 600 "$CLIENT_TXT"

cat >"$CLIENT_MAC_README" <<EOF
HXYLIVE Mac Clash (Scheme 1 Reality) — durable overlay

Goal: keep your airport subscription for normal internet; route only the VPS
host (${REALITY_PUBLIC_HOST}) through hxylive-reality. Refreshing/re-importing
the airport must NOT wipe this route.

Files on VPS (gitignored):
  clash-meta-mixin.yaml   — preferred (Clash Verge Merge format)
  clash-meta-client.yaml  — node-only fallback
  client-share.txt        — vless:// share line

Mac (recommended):
  1. scp clash-meta-mixin.yaml from /opt/hxylive/reality/ (via 7897 ProxyCommand)
  2. From the HXYLIVE repo on the Mac:
       ./mac-helper/apply-clash-reality-mixin.sh /path/to/clash-meta-mixin.yaml
  3. Clash Verge Rev: Profiles → enable "HXYLIVE Reality" Merge (right-click Enable)
     then refresh the airport profile once.
  4. Keep Clash listening on 127.0.0.1:7897.

Re-import airport: update/refresh the Remote profile as usual. Leave the
HXYLIVE Reality Merge enabled — do not paste Reality into the subscription file.

Other clients: paste clash-meta-mixin.yaml into the client's Merge / Override /
mixin layer (prepend-proxies + prepend-rules), not into the subscription body.
EOF
chmod 644 "$CLIENT_MAC_README"

printf '%s\n' "Reality installed. Client files: $CLIENT_MIXIN (preferred), $CLIENT_YAML, $CLIENT_TXT, $CLIENT_MAC_README"
printf '%s\n' "Camouflage dest/SNI: ${REALITY_DEST}"
printf '%s\n' "Public host: ${REALITY_PUBLIC_HOST}"
printf '%s\n' 'Mac: copy clash-meta-mixin.yaml and run mac-helper/apply-clash-reality-mixin.sh'
printf '%s\n' 'Next: deploy/set-access-mode.sh reality  (closes public :8080, keeps :443)'
