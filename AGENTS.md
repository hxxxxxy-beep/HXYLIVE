# HXYLIVE repository instructions

## Mission

This repository is the source of truth for rebuilding HXYLIVE on a clean VPS and a clean Mac. HXYLIVE is a self-hosted live-stream discovery, recording, playback, and media-management system.

**URL-only handoff:** a new AI session that receives only `https://github.com/hxxxxxy-beep/HXYLIVE` must clone `main`, read [`docs/AI_REDEPLOY.md`](docs/AI_REDEPLOY.md) and this file, ask once for secrets/host facts, then install and verify. Do not invent secrets. GitHub `main` is the source of truth for Cursor ↔ GitHub sync.

For a clean-machine deployment, inspect the current environment, preserve any explicitly supplied backup data, use the documented defaults where appropriate, and ask for information that is genuinely required or materially changes the deployment.

**Primary deploy guide:** [`docs/AI_REDEPLOY.md`](docs/AI_REDEPLOY.md)  
**Architecture (access schemes):** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — **Scheme 1** (VLESS-Reality + HTTP pull) is adopted now; Scheme 2 (Tunnel + R2) and Scheme 3 (Relay) escalate only after Scheme 1 fails.

## Language

Source code, identifiers, comments, UI copy, and deploy docs are English. Bilibili live-API fixtures may keep Chinese category and user names. Do not add non-English comments or identifiers.

## Fixed architecture

- VPS project path: `/opt/hxylive`
- VPS persistent data (library / HDD): `/opt/hxylive/data` (`HOST_DATA_DIR` → container `/data`)
- VPS hot TS staging (SSD): `/opt/hxylive/staging` (`HOST_STAGING_DIR` → container `/staging`)
- Nginx listen port: `8080` (public only in `direct` access mode; loopback/hairpin via Reality in default `reality` mode)
- Private HXYLIVE host port: `127.0.0.1:8081`
- Container application port: `8080`
- FlareSolverr: `127.0.0.1:8191`
- VLESS-Reality (host Xray): public `443` when `ENABLE_REALITY=1` (default)
- Reality secrets / Clash Merge overlay: `/opt/hxylive/reality/` (gitignored)
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
- Mac Helper: use `--proxy http://127.0.0.1:7897` when `--origin` is the VPS IP
- In Reality mode: apply `/opt/hxylive/reality/clash-meta-mixin.yaml` with `mac-helper/apply-clash-reality-mixin.sh` (Clash Verge Merge overlay) so airport re-import keeps `<VPS_IP>` → `hxylive-reality`

Do not open direct WAN connections from this Mac to the VPS when Reality is enabled. Localhost-only calls (`127.0.0.1:17899`, etc.) stay unproxied.

## Clean VPS deployment

Supported host OS: **Debian 12+** or **Ubuntu 22.04+**. Do not require the user to hand-install Docker/Nginx/Xray or edit host configs.

1. SSH from this Mac (via `7897` ProxyCommand when required). On a brand-new image, install `git`/`curl`/`ca-certificates` only if clone is otherwise impossible.
2. Clone this repository to `/opt/hxylive` (`main`).
3. Create `/opt/hxylive/.env` from `.env.example`; obtain unavailable secrets from the user (one ask). Default `ENABLE_REALITY=1`; confirm `REALITY_DEST` after `deploy/recommend-reality-dest.sh`.
4. Never recover secrets from Git history, logs, or archived conversations.
5. Run `deploy/install-vps.sh` (`deploy/bootstrap-host.sh` → `deploy/enable-bbr.sh` when supported → compose → `install-reality.sh` → `set-access-mode.sh reality` unless `ENABLE_REALITY=0`).
6. Run `deploy/verify-vps.sh` (asserts BBR when the kernel supports it, plus access-mode checks).
7. Align the cloud security group with access mode (`reality`: TCP 22+443; `direct`: TCP 22+8080).
8. Toggle later: `sudo ./deploy/set-access-mode.sh reality|direct` (reality closes public 8080; direct stops Xray and opens public 8080).

Do not commit or overwrite `/opt/hxylive/data` or `/opt/hxylive/reality`. If the user supplies a data backup, restore it before starting the final containers and verify ownership/permissions.

If Scheme 1 cannot stay stable from China: replace the VPS IP (still Scheme 1) → Scheme 2 (Tunnel management + R2 video staging) → Scheme 3 (Relay VPS last). Do not dual-run bulk download backends. Do not use an airport for bulk video. Do not deploy Scheme 2/3 during a routine reinstall.

## Clean Mac deployment

1. Confirm Python 3, Google Chrome, and Clash Meta/mihomo on `:7897` are available.
2. When `ENABLE_REALITY=1`, copy `clash-meta-mixin.yaml` from the VPS and run `mac-helper/apply-clash-reality-mixin.sh` (enable the Merge once; do not embed Reality in the airport subscription).
3. Run `mac-helper/install.sh --origin http://<VPS_IP>:8080 --proxy http://127.0.0.1:7897`.
4. Confirm launchd label `com.hxylive.mac-helper`.
5. Confirm `http://127.0.0.1:17899/health`.
6. Confirm the HXYLIVE Media page can scan the Mac folder.

The committed repository must never contain a generated plist with a real username, IP address, password, or local absolute path.

## Validation

Before declaring success:

- run the repository test suite;
- confirm `docker compose config`;
- confirm Nginx configuration;
- confirm VPS loopback `/api/version` and access-mode checks;
- confirm Mac Helper `/health` and Clash routing for the VPS IP;
- confirm the Media page can request a scan;
- report whether persistent recordings were restored or started empty.

## Naming

The product, directory, services, variables, scripts, browser identifiers, and documentation use `HXYLIVE`/`hxylive`. Do not reintroduce any former project name. Compatibility with old runtime data may be added only when explicitly needed for a migration and must be documented.

## Recording lifecycle audit

Durable evidence under `/opt/hxylive/data/audit/` (JSONL + `media-busy.json` for restart orphan reconcile) survives container recreate. Use it to explain missing or interrupted recordings — it is not a deploy gate.
