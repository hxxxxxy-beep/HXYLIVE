# Offline secrets checklist (never commit real values)

Store outside Git (password manager / encrypted disk).

## VPS `.env`

```bash
grep -E '^[A-Z0-9_]+=' /opt/hxylive/.env | cut -d= -f1
```

Save at least: `PASSWORD`, `ENABLE_REALITY`, `REALITY_DEST` (when Reality is on), `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_GAME_ID`, `YOUTUBE_API_KEY`, `HOST_PORT`, `PORT`, `HOST_DATA_DIR`, `HOST_STAGING_DIR`, `TZ`, optional Chaturbate + proxy vars.

Also back up `/opt/hxylive/reality/` (UUID, keys, `clash-meta-mixin.yaml`) off-box; it is gitignored and not in `data/`.

## SSH

Private key, SSH config host alias, VPS IP or hostname.

## Data (optional)

```bash
sudo tar -C /opt/hxylive -czf ~/hxylive-data-$(date +%Y%m%d).tar.gz data
```

Restore into `/opt/hxylive/data` before `install-vps.sh`.

## Mac

Video directory, Clash Meta/mihomo on `http://127.0.0.1:7897`, Reality Merge overlay applied (`clash-meta-mixin.yaml` via `mac-helper/apply-clash-reality-mixin.sh`) when Reality is on, Google Chrome installed. Optional Chrome download folder if it is not the HXYLIVE library directory.
