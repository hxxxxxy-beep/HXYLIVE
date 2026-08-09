# HXYLIVE

Self-hosted live-stream discovery, recording, playback, and media management.

- **VPS:** FastAPI app, recorder, media library, Nginx, Docker Compose, persistent `/data`
- **Mac:** localhost Helper that scans a video folder and opens short-lived VPS download links in Chrome

**New Mac / new AI:** paste only `https://github.com/hxxxxxy-beep/HXYLIVE` — the agent reads [`docs/AI_REDEPLOY.md`](docs/AI_REDEPLOY.md) + [`AGENTS.md`](AGENTS.md), asks for secrets once, then clones, configures, and deploys.  
**Secrets checklist:** [`docs/SECRETS_OFFLINE_CHECKLIST.md`](docs/SECRETS_OFFLINE_CHECKLIST.md)

Runtime databases, recordings, credentials, cookies, passwords, and machine paths are not stored in Git.

## Architecture

```text
Browser
  |
  | http(s)://VPS:8080
  v
Nginx :8080
  |
  | http://127.0.0.1:8081
  v
HXYLIVE container :8080  --->  /opt/hxylive/data
  |
  +-- FlareSolverr :8191

Browser on Mac  --->  HXYLIVE Mac Helper :17899 (localhost only)
                           |
                           +-- scans ~/Movies/HXYLIVE
                           +-- opens selected downloads in Chrome
```

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Features

- Discover and filter live channels (Twitch, Bilibili, Chaturbate, Stripchat)
- Manual and automatic recording (FFmpeg + provider stream resolvers)
- Media library with profiles, device filters (VPS / Mac), live status (Live / Private / Locked / Offline), last-live timestamps
- Mac folder sync and Chrome download dispatch
- Password protection and system status

## Repository layout

| Path | Purpose |
| --- | --- |
| `app/` | FastAPI application, providers, recorder, tasks, APIs |
| `static/` | Web UI |
| `tests/` | Unit and static regression tests |
| `docker-compose.yml` | VPS app + FlareSolverr |
| `Dockerfile`, `docker/` | Application image |
| `deploy/` | Nginx + VPS install/verify scripts |
| `mac-helper/` | Mac localhost helper + launchd installer |
| `.env.example` | Secret-free server config template |
| `.github/workflows/` | Tests and multi-arch image publish |

## VPS install

Requirements: Debian/Ubuntu, Git, Docker Engine, Docker Compose v2, Nginx, Twitch app credentials if needed.

```bash
git clone https://github.com/hxxxxxy-beep/HXYLIVE.git /opt/hxylive
cd /opt/hxylive
cp .env.example .env && chmod 600 .env
# set PASSWORD, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TZ=Asia/Shanghai
./deploy/install-vps.sh
./deploy/verify-vps.sh
```

Ports: public Nginx `8080`, private app `127.0.0.1:8081`, FlareSolverr `127.0.0.1:8191`.  
Persistent state: `/opt/hxylive/data` (never commit).

## Mac Helper install

Needs Python 3 and Google Chrome.

```bash
git clone https://github.com/hxxxxxy-beep/HXYLIVE.git && cd HXYLIVE
./mac-helper/install.sh \
  --origin http://YOUR_VPS_IP:8080 \
  --proxy http://127.0.0.1:7897
```

Omit `--proxy` if unused. Health: `curl --noproxy '*' http://127.0.0.1:17899/health`.  
Uninstall (keeps videos): `./mac-helper/uninstall.sh`.

## Configuration

Copy `.env.example` → `.env`. Never commit `.env`.

| Variable | Purpose |
| --- | --- |
| `PASSWORD` | Web password |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Twitch Helix |
| `HOST_DATA_DIR` | Persistent host data directory |
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
