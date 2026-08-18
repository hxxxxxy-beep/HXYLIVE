# Offline secrets checklist (never commit real values)

Store outside Git (password manager / encrypted disk).

## VPS `.env`

```bash
grep -E '^[A-Z0-9_]+=' /opt/hxylive/.env | cut -d= -f1
```

Save at least: `PASSWORD`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_GAME_ID`, `HOST_PORT`, `PORT`, `HOST_DATA_DIR`, `TZ`, optional Chaturbate + proxy vars.

## SSH

Private key, SSH config host alias, VPS IP or hostname.

## Data (optional)

```bash
sudo tar -C /opt/hxylive -czf ~/hxylive-data-$(date +%Y%m%d).tar.gz data
```

Restore into `/opt/hxylive/data` before `install-vps.sh`.

## Mac

Video directory, outbound proxy URL if required (often `http://127.0.0.1:7897`), Google Chrome installed. Optional Chrome download folder if it is not the HXYLIVE library directory.
