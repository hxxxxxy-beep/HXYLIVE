# HXYLIVE — AI redeploy (clean Mac + VPS)

**Repository:** https://github.com/hxxxxxy-beep/HXYLIVE (`main`)

GitHub `main` is the source of truth for Cursor ↔ GitHub sync. A new device starts from that URL, not from a stale local folder or chat history.

**Architecture (schemes):** read [`ARCHITECTURE.md`](ARCHITECTURE.md). **This deploy installs Scheme 1** (VLESS-Reality + HTTP pull). Do not implement Scheme 2 (Tunnel + R2) or Scheme 3 (Relay) during a normal redeploy.

## New device / new AI (start here)

Give the AI **only** this repository URL (`https://github.com/hxxxxxy-beep/HXYLIVE`). No zip, no chat history, no stale local folder. The agent must:

1. `git clone` latest `main` on this Mac (WAN via `http://127.0.0.1:7897` when on the development Mac).
2. Read this file, [`../AGENTS.md`](../AGENTS.md), and [`ARCHITECTURE.md`](ARCHITECTURE.md) (Scheme 1 is the current WAN path).
3. Ask once for missing secrets / host facts (never invent them). Use [`.env.example`](../.env.example) as the template.
4. On a clean **Debian 12+ or Ubuntu 22.04+** VPS: ensure `git`/`curl` if needed, clone to `/opt/hxylive`, write `.env` (including confirmed `REALITY_DEST` when Reality is on), run `deploy/install-vps.sh` (bootstrap + app + default VLESS-Reality), then `deploy/verify-vps.sh`. Do not ask the user to hand-edit apt packages or Nginx on the VPS.
5. Install Mac Helper on this Mac, apply the Reality Merge overlay into Clash (`:7897`) via `mac-helper/apply-clash-reality-mixin.sh`, then run the validation checklist before declaring success.

Dependency resolution is automated: VPS via Docker/`requirements.txt` inside `install-vps.sh`; Mac Helper via stdlib Python 3 (no pip package set required for the helper itself).

```text
Clone https://github.com/hxxxxxy-beep/HXYLIVE and read docs/AI_REDEPLOY.md plus AGENTS.md.
Deploy the latest main on a clean Debian 12+ or Ubuntu 22.04+ VPS at /opt/hxylive and install Mac Helper on this Mac.
I will provide: VPS IP, SSH access, .env secrets, Reality camouflage host confirmation (or ENABLE_REALITY=0), data restore choice, Mac video dir, Motrix availability, optional Chrome download dir, and Clash on http://127.0.0.1:7897.
Ask only for missing required values, then install and verify. Do not ask me to manually install Docker/Nginx or edit VPS configs.
```

| Side | Role |
|---|---|
| VPS | Production site + recorder + DB (Docker `hxylive` + Nginx + FlareSolverr); **Scheme 1** VLESS-Reality on host `:443`; SSD staging + HDD library with dual-disk storage gates |
| Mac | Clash client for Reality (imports snippet); Helper `:17899` archives videos (Motrix preferred; Chrome optional); **manual** Media download queue only |

```text
Scheme 1 (default deploy — reality mode):
  Mac Clash :7897  --VLESS-Reality-->  VPS :443 (Xray)
                                           └─ freedom → http://<VPS_IP>:8080 on loopback path
  Browser / Motrix / Helper use http://<VPS_IP>:8080 as origin (plain HTTP, not HTTPS)
  and MUST traverse Clash. Downloads = short-lived token + Nginx X-Accel-Redirect + Range.

  Nginx :8080 → 127.0.0.1:8081 → container hxylive
                    └─ /opt/hxylive/data → /data
                    └─ /opt/hxylive/staging → /staging
  Mac Helper → 127.0.0.1:17899 (localhost only)

Direct mode (opt-out): public naked :8080; Xray stopped — not the recommended China bulk path.
```

Canonical install path: `/opt/hxylive`. Supported VPS OS: **Debian 12+** and **Ubuntu 22.04+** (Debian family). Application runtime is containerized; host OS only needs packages installed by `deploy/bootstrap-host.sh`. Reality runs on the **host** (not in Docker).

## Ask the user once (do not invent secrets)

- VPS IP or hostname
- SSH user / key (how this Mac reaches the VPS)
- Web `PASSWORD`
- `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` (if Twitch discovery is needed)
- `YOUTUBE_API_KEY` (if YouTube discovery is needed)
- **WAN access (Scheme 1):** default `ENABLE_REALITY=1`. If keeping default: run `deploy/recommend-reality-dest.sh` on the VPS, present the top pick, and get an explicit `REALITY_DEST` confirmation (host only). Set `ENABLE_REALITY=0` only if the user wants naked public `:8080`.
- Whether to restore a `/opt/hxylive/data` backup or start empty
- Mac video directory (default `~/Movies/HXYLIVE`)
- Whether Motrix is installed (recommended for large downloads); Chrome is optional fallback and for cookie import
- Optional `--chrome-download-dir` only if Chrome Preferences cannot be read
- Mac Clash / mihomo on `http://127.0.0.1:7897` (required for Reality mode and for this development Mac)

Secrets are never in Git. See [`SECRETS_OFFLINE_CHECKLIST.md`](SECRETS_OFFLINE_CHECKLIST.md).

## VPS (AI runs these; user does not hand-edit the host)

From this Mac, SSH/SCP to the VPS **via** `ProxyCommand` / `7897` when required. On a brand-new image, install only enough to clone, then let the installer bootstrap the rest:

```bash
# On VPS as root (via SSH). Skip apt if git+curl already work.
apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y git curl ca-certificates

git clone https://github.com/hxxxxxy-beep/HXYLIVE.git /opt/hxylive
cd /opt/hxylive && cp .env.example .env && chmod 600 .env
# AI writes PASSWORD, TWITCH_*, YOUTUBE_API_KEY, TZ=Asia/Shanghai, ENABLE_REALITY, REALITY_DEST
# optional: restore data/ into HOST_DATA_DIR first
./deploy/recommend-reality-dest.sh   # show picks; user confirms REALITY_DEST before install when Reality is on
./deploy/install-vps.sh && ./deploy/verify-vps.sh
curl -sS http://127.0.0.1:8080/api/version
```

`install-vps.sh` always runs `deploy/bootstrap-host.sh` first (idempotent): installs Nginx and Docker Engine + Compose from Docker’s official apt repo when missing, enables services. Then it configures Nginx, enables TCP BBR (`deploy/enable-bbr.sh` when supported), `docker compose up -d --build`, and by default installs VLESS-Reality then `deploy/set-access-mode.sh reality` (public `:8080` closed, `:443` open).

Expect: loopback Nginx `:8080`, container `127.0.0.1:8081`, FlareSolverr `:8191`, data `/opt/hxylive/data`, staging `/opt/hxylive/staging`, Reality secrets under `/opt/hxylive/reality/` (gitignored).

### Access mode toggle (Scheme 1)

```bash
sudo ./deploy/set-access-mode.sh reality   # Xray on; UFW deny public 8080; allow 443
sudo ./deploy/set-access-mode.sh direct    # Xray off; UFW allow public 8080
```

Cloud provider **security groups must match** the mode (scripts configure UFW; SG is outside the repo):

| Mode | Security group |
|---|---|
| `reality` (default) | TCP **22 + 443** only (no public 8080) |
| `direct` | TCP **22 + 8080** |

### If Scheme 1 cannot stay stable from China

Escalate only when the user asks; do not redesign during a routine redeploy:

1. **Replace VPS IP** (still Scheme 1).
2. **Scheme 2** — Cloudflare Tunnel (management) + R2 short staging (video). See [`ARCHITECTURE.md`](ARCHITECTURE.md). One transport only; stop Scheme 1 bulk HTTP pull URLs when switching.
3. **Scheme 3** — paid Relay VPS (last).

Do **not** use an airport for bulk video. Do **not** deploy Tunnel/R2 or a relay “just in case.”

`verify-vps.sh` asserts BBR when supported and checks the recorded access mode. Re-run BBR alone: `sudo ./deploy/enable-bbr.sh`.

Default compose host port is **8081** (matches Nginx). Do not publish the app container on public `8080`.

Lifecycle audit (survives container recreate): `/opt/hxylive/data/audit/recording-lifecycle.jsonl` and `media-busy.json` (restart orphan reconcile, not a deploy gate).

Mac downloads are **manual only** (recording projects + download queue). After the Helper confirms a copy, matching VPS library files can be purged. Live `.ts` lands on SSD staging (`HOST_STAGING_DIR` → `/staging`) and is moved to the HDD library (`HOST_DATA_DIR` → `/data`) when closed; convert runs on the library disk. Storage gates: HDD below the configured free-GB threshold turns Auto Record off (manual re-enable after space recovers); SSD below its threshold pauses captures until staging is empty. Watch `df -h` on both mounts after deploy.

## Mac Helper + Reality client

1. Copy `/opt/hxylive/reality/clash-meta-mixin.yaml` (preferred) off the VPS via SCP through `7897`. Optional: `MAC_CLASH.txt`, `client-share.txt`.
2. On the Mac, apply the **Merge overlay** (survives airport subscription refresh — do not paste Reality into the subscription body):

```bash
./mac-helper/apply-clash-reality-mixin.sh /path/to/clash-meta-mixin.yaml
```

   Clash Verge Rev: Profiles → enable **HXYLIVE Reality** (Merge) → refresh the airport Remote profile once. Other clients: paste the mixin into Merge / Override / mixin (`prepend-proxies` + `prepend-rules`). Keep `:7897` as the airport entry; only `<VPS_IP>` uses `hxylive-reality`.
3. Install Helper:

```bash
export http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897
export HTTP_PROXY="$http_proxy" HTTPS_PROXY="$https_proxy"
./mac-helper/install.sh \
  --origin http://<VPS_IP>:8080 \
  --video-dir "$HOME/Movies/HXYLIVE" \
  --proxy http://127.0.0.1:7897
curl --noproxy '*' http://127.0.0.1:17899/health
```

Motrix and Chrome staging should be on the Mac (`~/Downloads`); the helper moves finished files into the video library. Pass `--chrome-download-dir "$HOME/Downloads"` when Preferences cannot be read. Confirm the Media page lists Mac files. Mac downloads are started from the Media page / download queue (Auto Sync has been removed).

**User-only steps:** confirm `REALITY_DEST`; apply Clash Merge overlay (`apply-clash-reality-mixin.sh`); set cloud security group. AI cannot click the Clash GUI.

## Validate before declaring success

```bash
python3 -m unittest discover -s tests
docker compose config --quiet
```

On VPS: `/api/version` on loopback, Nginx, container healthy, access-mode checks (Xray `:443` when reality).  
On Mac: Helper `/health`, Clash rule for VPS IP, Media scan works.  
Report whether persistent recordings were restored or started empty.

## Rules

- Do not force-push `main`, wipe production `data`, or change git config unless the user explicitly asks.
- Production cutover only when the user explicitly asks.
- Source, UI copy, and deploy docs are English. Bilibili API fixtures may include Chinese category/user names from the live API.
- Product naming is `HXYLIVE` / `hxylive` only.
- On this development Mac, GitHub / VPS SSH/SCP / package installs use `http://127.0.0.1:7897`. Localhost Helper calls stay unproxied.
- Default deploy is **Scheme 1** (VLESS-Reality + HTTP `:8080` pull). Do not treat naked `:8080` as the China Mac bulk-download path. Do not stand up Scheme 2/3 unless the user explicitly orders that escalation.
- Do not ask the user to manually install Docker, Compose, Nginx, or Xray, or to edit Nginx/sysctl by hand — scripts own that.
