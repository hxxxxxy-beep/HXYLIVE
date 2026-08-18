# HXYLIVE repository instructions

## Mission

This repository is the source of truth for rebuilding HXYLIVE on a clean VPS and a clean Mac. HXYLIVE is a self-hosted live-stream discovery, recording, playback, and media-management system.

**URL-only handoff:** a new AI session that receives only `https://github.com/hxxxxxy-beep/HXYLIVE` must clone `main`, read [`docs/AI_REDEPLOY.md`](docs/AI_REDEPLOY.md) and this file, ask once for secrets/host facts, then install and verify. Do not invent secrets. GitHub `main` is the source of truth for Cursor ↔ GitHub sync.

For a clean-machine deployment, inspect the current environment, preserve any explicitly supplied backup data, use the documented defaults where appropriate, and ask for information that is genuinely required or materially changes the deployment.

**Primary deploy guide:** [`docs/AI_REDEPLOY.md`](docs/AI_REDEPLOY.md)

## Language

Source code, identifiers, comments, UI copy, and deploy docs are English. Bilibili live-API fixtures may keep Chinese category and user names. Do not add non-English comments or identifiers.

## Fixed architecture

- VPS project path: `/opt/hxylive`
- VPS persistent data: `/opt/hxylive/data`
- Public Nginx port: `8080`
- Private HXYLIVE host port: `127.0.0.1:8081`
- Container application port: `8080`
- FlareSolverr: `127.0.0.1:8191`
- Main container name: `hxylive`
- Mac Helper label: `com.hxylive.mac-helper`
- Mac Helper endpoint: `127.0.0.1:17899`
- Default Mac video folder: `~/Movies/HXYLIVE`
- Mac outbound proxy (mandatory on this development Mac): `http://127.0.0.1:7897`

## Mac network proxy (mandatory on this Mac)

On the development/deployment Mac, all traffic to the public internet and the remote VPS must use `http://127.0.0.1:7897`:

- HTTP(S): `http_proxy` / `https_proxy` (and uppercase variants)
- Git / GitHub / package downloads
- SSH / SCP to the VPS: `ProxyCommand='/usr/bin/nc -X connect -x 127.0.0.1:7897 %h %p'`
- Mac Helper: always pass `--proxy http://127.0.0.1:7897` (or `HXYLIVE_PROXY_URL`)

Do not open direct WAN connections from this Mac. Localhost-only calls (`127.0.0.1:17899`, etc.) stay unproxied.

## Clean VPS deployment

1. Confirm Docker, Docker Compose, Nginx, Git, and curl are installed.
2. Clone this repository to `/opt/hxylive` (`main`).
3. Create `/opt/hxylive/.env` from `.env.example`; obtain unavailable secrets from the user.
4. Never recover secrets from Git history, logs, or archived conversations.
5. Run `deploy/install-vps.sh`.
6. Run `deploy/verify-vps.sh`.
7. Verify the public URL and container health.

Do not commit or overwrite `/opt/hxylive/data`. If the user supplies a data backup, restore it before starting the final containers and verify ownership/permissions.

## Clean Mac deployment

1. Confirm Python 3 and Google Chrome are available.
2. Run `mac-helper/install.sh --origin <VPS URL> --proxy http://127.0.0.1:7897` (proxy required on this Mac).
3. Confirm launchd label `com.hxylive.mac-helper`.
4. Confirm `http://127.0.0.1:17899/health`.
5. Confirm the HXYLIVE Media page can scan the Mac folder.

The committed repository must never contain a generated plist with a real username, IP address, password, or local absolute path.

## Validation

Before declaring success:

- run the repository test suite;
- confirm `docker compose config`;
- confirm Nginx configuration;
- confirm VPS `/api/version`;
- confirm Mac Helper `/health`;
- confirm the Media page can request a scan;
- report whether persistent recordings were restored or started empty.

## Naming

The product, directory, services, variables, scripts, browser identifiers, and documentation use `HXYLIVE`/`hxylive`. Do not reintroduce any former project name. Compatibility with old runtime data may be added only when explicitly needed for a migration and must be documented.
