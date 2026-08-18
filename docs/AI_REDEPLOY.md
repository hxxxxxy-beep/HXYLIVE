# HXYLIVE — AI redeploy (clean Mac + VPS)

**Repository:** https://github.com/hxxxxxy-beep/HXYLIVE (`main`)

GitHub `main` is the source of truth for Cursor ↔ GitHub sync. A new device starts from that URL, not from a stale local folder or chat history.

## New device / new AI (start here)

Give the AI **only** this repository URL. It must:

1. Clone `main` (latest).
2. Read this file and [`../AGENTS.md`](../AGENTS.md).
3. Ask once for missing secrets / host facts (never invent them).
4. Deploy VPS and/or Mac Helper with the scripts below.
5. Run the validation checklist before declaring success.

```text
Clone https://github.com/hxxxxxy-beep/HXYLIVE and read docs/AI_REDEPLOY.md plus AGENTS.md.
Deploy the latest main on a clean Ubuntu VPS at /opt/hxylive and install Mac Helper on this Mac.
I will provide: VPS IP, .env secrets, data restore choice, Mac video dir, optional Chrome download dir, and whether to use proxy http://127.0.0.1:7897.
Ask only for missing required values, then install and verify.
```

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
- Optional Chrome download directory if it is not the library folder (otherwise the helper reads Chrome Preferences + `~/Downloads`)
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

Default compose host port is **8081** (matches Nginx). Do not publish the app on public `8080`.

## Mac Helper

On a development Mac that must use the local outbound proxy:

```bash
export http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897
export HTTP_PROXY="$http_proxy" HTTPS_PROXY="$https_proxy"
git clone https://github.com/hxxxxxy-beep/HXYLIVE.git && cd HXYLIVE
./mac-helper/install.sh \
  --origin http://<VPS_IP>:8080 \
  --video-dir "$HOME/Movies/HXYLIVE" \
  --proxy http://127.0.0.1:7897
curl --noproxy '*' http://127.0.0.1:17899/health
```

Omit `--proxy` and the `*_proxy` exports if unused. Pass `--chrome-download-dir` only when Chrome saves files outside the library folder and Chrome Preferences cannot be read. Confirm the Media page can request a Mac folder scan.

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
- Source, UI copy, and deploy docs are English. Bilibili API fixtures may include Chinese category/user names from the live API.
- Product naming is `HXYLIVE` / `hxylive` only.
- On this development Mac, all WAN traffic (GitHub, VPS SSH/SCP, package installs) uses `http://127.0.0.1:7897`. Localhost Helper calls stay unproxied.
