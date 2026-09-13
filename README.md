# HXYLIVE

Self-hosted live-stream discovery, recording, playback, and media management.

- **VPS:** FastAPI app, recorder, media library, Nginx, Docker Compose, SSD staging + HDD library
- **Mac:** localhost Helper that archives videos (Motrix preferred; Chrome optional) via a **manual** download queue

**New Mac / new AI:** paste only `https://github.com/hxxxxxy-beep/HXYLIVE` — the agent clones latest `main`, reads [`docs/AI_REDEPLOY.md`](docs/AI_REDEPLOY.md) + [`AGENTS.md`](AGENTS.md), asks for secrets once, then deploys. GitHub `main` is the Cursor ↔ GitHub source of truth.  
**Secrets checklist:** [`docs/SECRETS_OFFLINE_CHECKLIST.md`](docs/SECRETS_OFFLINE_CHECKLIST.md)  
**Architecture (three access schemes):** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

Runtime databases, recordings, credentials, cookies, passwords, and machine paths are not stored in Git.

## Architecture

**Adopted now — Scheme 1:** VLESS-Reality + plain HTTP `:8080` short-lived downloads (not HTTPS). Escalate only if needed: replace IP → Scheme 2 (Tunnel + R2 staging) → Scheme 3 (Relay). Details in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```text
Scheme 1 (default):
  Mac Clash :7897 --VLESS-Reality--> VPS :443 (host Xray) --> http://VPS:8080 (not public)
Naked opt-out (direct): Browser --> http://VPS:8080

Nginx :8080 --> 127.0.0.1:8081 --> HXYLIVE container :8080
                                      +-- /opt/hxylive/data (library)
                                      +-- /opt/hxylive/staging (hot TS)
                                      +-- FlareSolverr :8191

Browser on Mac --> HXYLIVE Mac Helper :17899 (localhost only)
                     +-- scans video library / claims download jobs
                     +-- fetches via Motrix (preferred) or Chrome through Clash
```

Default deploy enables VLESS-Reality (`ENABLE_REALITY=1`); toggle with `deploy/set-access-mode.sh`.

## Features

- Discover and filter live channels (Twitch, YouTube, Bilibili, Chaturbate, Stripchat)
- Manual and automatic recording (FFmpeg + provider stream resolvers)
- Media library with profiles, device filters (VPS / Mac), live status (Live / Private / Locked / Offline), last-live timestamps
- Manual Mac download queue (Motrix preferred); dual-disk storage gates (HDD Auto Record off / SSD staging drain)
- Password protection and system status

## Repository layout

| Path | Purpose |
| --- | --- |
| `app/` | FastAPI application, providers, recorder, tasks, APIs |
| `static/` | Web UI |
| `tests/` | Unit and static regression tests |
| `docker-compose.yml` | VPS app + FlareSolverr |
| `Dockerfile`, `docker/` | Application image |
| `deploy/` | Host bootstrap, Nginx, VLESS-Reality, access-mode toggle, VPS install/verify |
| `mac-helper/` | Mac localhost helper + launchd installer |
| `.env.example` | Secret-free server config template |
| `.github/workflows/` | Tests and multi-arch image publish |

## VPS install

Requirements: **Debian 12+** or **Ubuntu 22.04+**, root SSH, and Twitch/YouTube credentials if those providers are needed.  
`deploy/install-vps.sh` runs `deploy/bootstrap-host.sh` to install Nginx and Docker Engine + Compose when missing — a clean VPS does not need hand-installed Docker/Nginx.

```bash
# Brand-new image only (skip if git already works):
apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y git curl ca-certificates

git clone https://github.com/hxxxxxy-beep/HXYLIVE.git /opt/hxylive
cd /opt/hxylive
cp .env.example .env && chmod 600 .env
# set PASSWORD, TWITCH_*, YOUTUBE_API_KEY, TZ, REALITY_DEST (confirm after recommend-reality-dest.sh)
./deploy/install-vps.sh
./deploy/verify-vps.sh
```

Default access: Reality on public `443`, public `8080` closed (`set-access-mode.sh reality`).  
Opt out: `ENABLE_REALITY=0` or `set-access-mode.sh direct` (naked `8080`).  
Private app `127.0.0.1:8081`, FlareSolverr `127.0.0.1:8191`.  
Persistent state: `/opt/hxylive/data`, `/opt/hxylive/staging`, and `/opt/hxylive/reality/` (never commit).

## Mac Helper install

Needs Python 3. Install [Motrix](https://motrix.app/) for the recommended download path; Google Chrome is optional (fallback / cookie import).

```bash
git clone https://github.com/hxxxxxy-beep/HXYLIVE.git && cd HXYLIVE
# Reality mode: scp clash-meta-mixin.yaml from the VPS, then:
# ./mac-helper/apply-clash-reality-mixin.sh /path/to/clash-meta-mixin.yaml
./mac-helper/install.sh \
  --origin http://YOUR_VPS_IP:8080 \
  --proxy http://127.0.0.1:7897
```

Omit `--proxy` if unused. Keep Reality in the Clash Merge overlay (not inside the airport subscription). The helper watches Motrix/Chrome download folders and files completed videos into the library; pass `--chrome-download-dir` only if Chrome Preferences cannot be read. Health: `curl --noproxy '*' http://127.0.0.1:17899/health`.  
Uninstall (keeps videos): `./mac-helper/uninstall.sh`.

## Configuration

Copy `.env.example` → `.env`. Never commit `.env`.

| Variable | Purpose |
| --- | --- |
| `PASSWORD` | Web password |
| `ENABLE_REALITY` | Default `1` — install VLESS-Reality and close public `:8080` |
| `REALITY_DEST` | Confirmed camouflage host (SNI/dest), required when Reality is on |
| `REALITY_PUBLIC_HOST` | Optional public IP/host override for client snippet |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Twitch Helix |
| `YOUTUBE_API_KEY` | YouTube Data API (Discover + live metadata) |
| `HOST_DATA_DIR` | Persistent host library directory (HDD) |
| `HOST_STAGING_DIR` | Persistent host hot-TS directory (SSD) |
| `HOST_PORT` | Private host port behind Nginx (default `8081`) |
| `TZ` | Server timezone |
| `HXYLIVE_PROXY_URL` | Optional outbound provider proxy |
| `CHATURBATE_USERNAME`, `CHATURBATE_PASSWORD` | Optional provider login |

## Development checks

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q app tests mac-helper
docker compose config --quiet
```

## License

See [LICENSE](LICENSE). Users must comply with applicable laws and third-party platform terms.
