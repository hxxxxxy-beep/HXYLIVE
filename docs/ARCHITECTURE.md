# HXYLIVE architecture

HXYLIVE is a self-hosted live-stream discovery, recording, playback, and media-management system. The VPS records and hosts the app; a China Mac archives finished videos through the Mac Helper (Motrix preferred).

This document is the canonical product architecture, including **three WAN / download schemes**. Deploy steps live in [`AI_REDEPLOY.md`](AI_REDEPLOY.md); agent constraints live in [`../AGENTS.md`](../AGENTS.md).

---

## Access and download schemes

HXYLIVE defines three schemes for how the China Mac reaches the VPS and pulls large recordings. Escalate in order. Do not run two bulk-download backends in parallel.

| Scheme | Role | Status |
| --- | --- | --- |
| **Scheme 1** — VLESS-Reality + HTTP pull | Primary path | **Adopted now** |
| **Scheme 2** — Cloudflare Tunnel + R2 staging | Structural alternative | Designed; implement only after Scheme 1 fails (including after IP replace) |
| **Scheme 3** — Relay VPS | Last resort | Paid; highest cost and failure surface |

**Escalation:** Scheme 1 → (cheap fix: **replace VPS IP**, still Scheme 1) → Scheme 2 → Scheme 3.

**Always available, not a freight scheme:** an existing third-party proxy (“airport”) is an **emergency management lifeboat** only (SSH / panel / diagnosis). It must not carry day-scale ~50 GB or month-scale ~1.5 TB video.

**Principle:** pay for problems that already happened, not for imagined ones. Do not buy a relay “just in case.” Do not deploy Scheme 2 while Scheme 1 still works.

### Scheme 1 — VLESS-Reality + HTTP pull (current)

```text
Mac Clash :7897  --VLESS-Reality-->  VPS :443 (host Xray)
                                      └─ freedom → http://<VPS_IP>:8080 (loopback / hairpin)
Nginx :8080 → 127.0.0.1:8081 → container hxylive
Mac Helper → 127.0.0.1:17899 (localhost only)
```

- **Application download protocol:** plain **HTTP** on `:8080` (not HTTPS). Short-lived token URLs (`/api/mac/download/<token>`), Nginx `X-Accel-Redirect`, `Content-Disposition: attachment`, HTTP Range for resume.
- **WAN encryption:** the VLESS-Reality tunnel (Clash), not site TLS on `:8080`.
- Browser, Motrix, and Helper all use origin `http://<VPS_IP>:8080` and **must** traverse Clash when `ENABLE_REALITY=1` (public `:8080` is closed).
- **Direct mode** (`set-access-mode.sh direct` / `ENABLE_REALITY=0`): naked public `http://IP:8080`; Xray stopped. Not the recommended China Mac bulk path.
- Default file concurrency for the download queue is **6** (configurable).

### Scheme 2 — Cloudflare Tunnel + R2 staging (next if Scheme 1 dies)

```text
Management:  Mac ── Cloudflare Tunnel ──► VPS app (:8080 on host)
Video:       VPS ── upload ──► R2 (short staging) ── signed URL ──► Mac (Motrix)
             after Mac verify ──► delete R2 object
```

- **Tunnel** is for the **management plane** (UI / API / Helper control). It is not the bulk video pipe. Keep SSH (and optionally Reality or airport) as a **side path** so a Cloudflare outage is not total lockout.
- **R2** is **temporary staging**, not a permanent video warehouse. Typical flow: select on Media → queue → upload to R2 → Mac download → integrity check (SHA-256 recommended) → delete R2. Lifecycle rules only clean orphans; they must not replace “delete after successful download.”
- R2 public egress is free; **VPS → R2 upload still consumes GreenCloud egress / fair use**. Decoupling China from the VPS IP does not remove VPS outbound volume (~1.5 TB/month at current scale).
- Cap concurrent R2-resident / in-flight tasks (default align with file concurrency **6**) so staging stays small (order of gigabytes, not terabytes).
- **Switch rule:** when enabling Scheme 2 for freight, **stop issuing Scheme 1 direct-pull download URLs**. One transport at a time. Extend the existing `download_queue` / Helper job model; do not invent a parallel state machine.
- Suggested per-file states: `queued → uploading_r2 → r2_ready → mac_downloading → verifying → purge_r2 → done` (retry keeps R2 on verify failure).

### Scheme 3 — Relay VPS (last)

```text
Mac ──► Relay VPS ──► origin GreenCloud VPS
```

Adds a hop so the Mac (or clients) no longer depend on the origin IP path. Requires enough bandwidth for the same ~1.5 TB/month class of traffic. Use only after Scheme 2 is insufficient or impractical.

### Scheme decision summary

| Scheme | Main job | Depends on origin VPS IP for Mac bulk download? | Cost | Complexity | When |
| --- | --- | --- | --- | --- | --- |
| 1 Reality + HTTP | Management + video | Yes (via Reality) | Low | Low (shipped) | **Now** |
| 1 + replace IP | Same stack, new address | Yes (new IP) | Low | Very low | First paid try after Scheme 1 path failure |
| 2 Tunnel + R2 | Mgmt via Tunnel; video via R2 | No for Mac←R2 video | Low–medium | Medium | After IP replace still fails |
| 3 Relay | Full path relay | No (to origin) | Medium–high | High | Last |
| Airport | Emergency admin | Via third-party nodes | Already paid | Low | Anytime for rescue only |

---

## VPS responsibilities

The FastAPI application serves the web UI and APIs, discovers provider streams, starts and supervises recording processes, converts recordings, maintains the media library, and issues short-lived download jobs for the Mac Helper.

Monitor polls tracked channels (including Media card sources) and writes durable **live sessions** (`live_sessions` in SQLite) so the Media page can compare detected live intervals with recording file coverage via `/api/media-timeline`. Live history starts after this feature is deployed; it is not backfilled.

After MP4 conversion, fragments are assigned to **recording projects** (gap-buffer grouping). Mac downloads are **manual only** via a server-side download queue (project/file concurrency; Media selection appends; Helper claims scheduler-eligible Motrix jobs). Auto Sync has been removed. Matching copies confirmed on Mac are purged from the VPS while project members remain as Mac cards. Manual Mac concat is serial (`concat-project` Helper command).

**Dual-disk layout (production):** live `.ts` is written to an SSD staging volume (`STAGING_DIR`, e.g. `/staging`), then moved to the HDD library (`OUTPUT_DIR` / `HOST_DATA_DIR`) as soon as each file is closed. TS→MP4 conversion runs on the library disk. Storage policy: HDD free below a configurable threshold (default 100 GB) turns Auto Record off and disables the switch until space recovers (then stays off until manually re-enabled); SSD free below a configurable threshold (default 5 GB) pauses captures while staging is drained, keeps the switch on with a “cleaning staging” status, and resumes automatically only after staging recording files are fully cleared. Heavy H.264 transcodes (HEVC/AV1/VP9) are serialized with limited threads and nice priority. Emergency library deletes from the old single-disk water-level design are removed.

Docker Compose runs:

- `hxylive`: the application, FFmpeg, Playwright Chromium, and provider logic;
- `flaresolverr`: optional Cloudflare-assisted provider access (challenge solving for some sites — unrelated to Scheme 2 Tunnel/R2).

Nginx listens on `:8080` and proxies HTTP/WebSocket traffic to the private application port, serving protected recording ranges through an internal alias. The application container does not bind directly to a public interface.

**WAN (host), Scheme 1:** `deploy/install-vps.sh` installs host Xray VLESS-Reality on `:443` and runs `deploy/set-access-mode.sh reality`, which closes public `:8080` via UFW. Clients use Clash (`:7897`) with a durable Merge overlay from `/opt/hxylive/reality/clash-meta-mixin.yaml` (`mac-helper/apply-clash-reality-mixin.sh`) so airport subscription refresh keeps `<VPS_IP>` → `hxylive-reality`. `set-access-mode.sh direct` stops Xray and re-opens public `:8080`.

---

## Persistent state

The container sees `/data` (library / HDD via `HOST_DATA_DIR`) and `/staging` (hot TS / SSD via `HOST_STAGING_DIR`). Defaults on a single-disk lab host can point both under the same disk; production uses separate mounts.

Typical library state includes:

- application database and settings;
- promoted recording files and conversion work;
- thumbnails and profile images;
- provider cookies/session state;
- playback progress and profile metadata.

Staging holds only in-progress `.ts` files until they are moved to the library.

GitHub contains schemas and code, not this mutable state. Never commit `/opt/hxylive/data` or `/opt/hxylive/reality/`.

---

## Mac Helper responsibilities

The helper is a Python standard-library HTTP service bound only to `127.0.0.1:17899`. It:

- accepts requests only from the configured HXYLIVE web origin;
- scans the configured Mac video directory;
- reports downloaded recording IDs and file sizes to the VPS on a short heartbeat;
- retrieves short-lived download jobs from the VPS (Media-page queue / helper poll);
- reports its configured web origin on heartbeat so the VPS can build download URLs without a browser request;
- opens each selected download URL in Motrix (preferred) or Google Chrome.

Under Scheme 1 the URL is an HTTP token on `:8080` via Clash. Under Scheme 2 the URL would be an R2 signed object URL; the Helper still only consumes “a URL + method,” not a second product surface.

The helper also reads Google Chrome cookies for provider session import (Bilibili, Twitch, Chaturbate, Stripchat, YouTube). The first import may show a macOS Keychain prompt for "Chrome Safe Storage".

The Media page can show Mac files, open or delete them, and start downloads even when the browser cannot reach localhost. Mac-only covers are uploaded by the helper and served from the VPS in that case. If `--proxy` is set and that proxy is down, the helper may fall back to a direct VPS connection — avoid that fallback in Reality mode (public `:8080` is closed).

The helper does not expose a LAN listener and does not store VPS credentials.

---

## Security boundaries

- Web access should be protected by `PASSWORD`.
- `.env`, provider cookies, databases, recordings, and generated Mac plist files are ignored.
- The application port and FlareSolverr bind to loopback on the VPS.
- The Mac Helper binds to loopback and enforces one allowed web origin.
- Automatic Docker update access is disabled by default and must not receive the Docker socket casually.
- Scheme 2 credentials (R2 / Tunnel) must stay off Git, same as Reality secrets.

---

## Recording lifecycle audit

Durable evidence under `/opt/hxylive/data/audit/` (JSONL + `media-busy.json` for restart orphan reconcile) survives container recreate. Use it to explain missing or interrupted recordings — it is not a deploy gate.
