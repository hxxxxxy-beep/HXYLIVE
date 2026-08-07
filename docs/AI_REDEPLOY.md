# HXYLIVE — AI redeploy (clean Mac + VPS)

**Repository:** https://github.com/hxxxxxy-beep/HXYLIVE (`main`)

Paste this repo URL into a new AI session. Read this file and [`../AGENTS.md`](../AGENTS.md), then deploy the latest `main` on a clean Ubuntu VPS and optional Mac Helper.

| Side | Role |
|---|---|
| VPS | Production site + recorder + DB (Docker `hxylive` + Nginx + FlareSolverr) |
| Mac | Optional Helper `:17899` — scans the local video folder and opens VPS download links in Chrome |

```text
Browser → Nginx :8080 → 127.0.0.1:8081 → container hxylive
                              └─ /opt/hxylive/data → /data
Mac Helper → 127.0.0.1:17899 (localhost only)
```

Canonical install path: `/opt/hxylive`.

## Ask the user once (do not invent secrets)

- VPS IP or hostname
- Web `PASSWORD`
- `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` (if Twitch discovery is needed)
- Whether to restore a `/opt/hxylive/data` backup or start empty
- Mac video directory (default `~/Movies/HXYLIVE`)
- Whether this Mac must use outbound proxy `http://127.0.0.1:7897`

Secrets are never in Git. See [`SECRETS_OFFLINE_CHECKLIST.md`](SECRETS_OFFLINE_CHECKLIST.md).

## VPS

```bash
git clone https://github.com/hxxxxxy-beep/HXYLIVE.git /opt/hxylive
cd /opt/hxylive && cp .env.example .env && chmod 600 .env
# edit PASSWORD, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TZ=Asia/Shanghai
# optional: restore data/ into HOST_DATA_DIR first
./deploy/install-vps.sh && ./deploy/verify-vps.sh
curl -sS http://127.0.0.1:8080/api/version
```

Expect: public `:8080`, container `127.0.0.1:8081`, FlareSolverr `:8191`, data `/opt/hxylive/data`.

## Mac Helper

```bash
git clone https://github.com/hxxxxxy-beep/HXYLIVE.git && cd HXYLIVE
./mac-helper/install.sh \
  --origin http://<VPS_IP>:8080 \
  --video-dir "$HOME/Movies/HXYLIVE" \
  --proxy http://127.0.0.1:7897   # omit if unused
curl --noproxy '*' http://127.0.0.1:17899/health
```

Confirm the Media page can request a Mac folder scan.

## Validate before declaring success

```bash
python3 -m unittest discover -s tests
docker compose config --quiet
```

On VPS: `/api/version`, Nginx `:8080`, container healthy.  
On Mac: Helper `/health`, Media scan works.  
Report whether persistent recordings were restored or started empty.

## Rules

- Do not force-push `main`, wipe production `data`, or change git config unless the user explicitly asks.
- Production cutover only when the user explicitly asks.
- UI and deploy docs are English. Bilibili API fixtures may include Chinese category/user names from the live API.
- Product naming is `HXYLIVE` / `hxylive` only.

## Fresh-AI prompt (copy/paste)

```text
Clone https://github.com/hxxxxxy-beep/HXYLIVE and read docs/AI_REDEPLOY.md plus AGENTS.md.
Deploy the latest main on a clean Ubuntu VPS at /opt/hxylive and install Mac Helper on this Mac.
I will provide: VPS IP, .env secrets, data restore choice, Mac video dir, and whether to use proxy http://127.0.0.1:7897.
Ask only for missing required values, then install and verify.
```
