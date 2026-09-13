#!/bin/sh
# Install host packages required by HXYLIVE on Debian 12+ / Ubuntu 22.04+.
# Idempotent: skips steps when Docker Engine, Compose, Nginx, git, and curl are present.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root: sudo ./deploy/bootstrap-host.sh' >&2
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    printf '%s\n' 'Unsupported host: /etc/os-release is missing.' >&2
    exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release

case "${ID:-}" in
    debian|ubuntu) ;;
    *)
        printf 'Unsupported OS ID=%s. Use Debian 12+ or Ubuntu 22.04+.\n' "${ID:-unknown}" >&2
        exit 1
        ;;
esac

export DEBIAN_FRONTEND=noninteractive

apt_update_once() {
    if [ "${_HXYLIVE_APT_UPDATED:-0}" = "1" ]; then
        return 0
    fi
    apt-get update -y
    _HXYLIVE_APT_UPDATED=1
}

ensure_apt_packages() {
    need=""
    for pkg in "$@"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            need="$need $pkg"
        fi
    done
    need=$(printf '%s' "$need" | sed 's/^ *//')
    if [ -z "$need" ]; then
        return 0
    fi
    apt_update_once
    # shellcheck disable=SC2086
    apt-get install -y $need
}

ensure_apt_packages ca-certificates curl git gnupg nginx openssl iproute2 ufw

have_docker=0
have_compose=0
if command -v docker >/dev/null 2>&1; then
    have_docker=1
    if docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; then
        have_compose=1
    fi
fi

if [ "$have_docker" -eq 1 ] && [ "$have_compose" -eq 1 ]; then
    printf '%s\n' 'Docker Engine and Compose already present; skipping Docker install.'
else
    apt_update_once
    ensure_apt_packages ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.asc ]; then
        curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
    fi

    CODENAME="${VERSION_CODENAME:-}"
    if [ -z "$CODENAME" ]; then
        printf '%s\n' 'VERSION_CODENAME missing from /etc/os-release; cannot configure Docker apt repo.' >&2
        exit 1
    fi

    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$(dpkg --print-architecture)" \
        "$ID" \
        "$CODENAME" > /etc/apt/sources.list.d/docker.list

    apt-get update -y
    _HXYLIVE_APT_UPDATED=1
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker >/dev/null 2>&1 || true
systemctl enable --now nginx >/dev/null 2>&1 || true

# Soft-open SSH if UFW is already active. Public :8080 vs :443 is owned by
# deploy/set-access-mode.sh after install (reality closes :8080; direct opens it).
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then
    ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
fi

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Docker install failed: docker binary missing.' >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    printf '%s\n' 'Docker Compose plugin/install failed.' >&2
    exit 1
fi
if ! command -v nginx >/dev/null 2>&1; then
    printf '%s\n' 'Nginx install failed.' >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    printf '%s\n' 'git/curl install failed.' >&2
    exit 1
fi

printf '%s\n' 'HXYLIVE host bootstrap complete (git, curl, nginx, Docker Engine + Compose).'
