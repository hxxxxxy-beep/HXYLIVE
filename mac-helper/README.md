# HXYLIVE Mac Helper

The helper is the Mac side of HXYLIVE. It listens only on `127.0.0.1:17899`, scans a selected video directory, and fetches short-lived VPS download URLs via Motrix (preferred) or Google Chrome from the Media-page **manual** download queue (Scheme 1: plain HTTP token URLs through Clash/Reality — see `docs/ARCHITECTURE.md`).

Install:

```bash
./mac-helper/install.sh --origin http://YOUR_VPS_IP:8080
```

Optional proxy (required on the development Mac that uses port 7897). With default VPS Reality mode, apply the Merge overlay first (survives airport re-import):

```bash
# After scp of /opt/hxylive/reality/clash-meta-mixin.yaml from the VPS:
./mac-helper/apply-clash-reality-mixin.sh /path/to/clash-meta-mixin.yaml
# Clash Verge Rev: enable "HXYLIVE Reality" Merge, then refresh the airport profile.
./mac-helper/install.sh \
  --origin http://YOUR_VPS_IP:8080 \
  --proxy http://127.0.0.1:7897
```

Do not paste Reality into the airport subscription body; keep it in the Merge/Override layer.

Motrix and Chrome both stage on the Mac (`~/Downloads` recommended; Chrome Preferences or `--chrome-download-dir`). The helper then **moves** finished files into `video_dir/<streamer>/` (cross-volume copy + delete source when the library is on an external disk). Do not point Chrome, Motrix, or `--chrome-download-dir` at the external video library — that wakes the disk for the whole transfer. Pass `--chrome-download-dir` only when Preferences cannot be read.

Heartbeat reports the helper `--origin` so the VPS can build download URLs without a browser request. Auto Sync has been removed; only Media-queue jobs are claimed.

The helper also pushes folder snapshots to the VPS and claims download jobs and local open/delete commands there. Opening a Mac video uses IINA when installed (`open -a IINA`), with a fallback to the system default app. Reveal-in-Finder still uses Finder. The Media page can list Mac files, open them, and start downloads even when the browser cannot reach `127.0.0.1:17899` (Chrome local-network restrictions, or with the system proxy off). If `--proxy` is set and that proxy is down, the helper reaches the VPS directly — avoid that fallback in Reality mode (public `:8080` is closed).

Settings → **Import from Chrome** uses this helper to read the current Google Chrome login cookies for Bilibili, Twitch, Chaturbate, Stripchat, and YouTube. Log into the site in Chrome first. The first import may ask for Keychain access to Chrome Safe Storage.

Verify:

```bash
curl --noproxy '*' http://127.0.0.1:17899/health
```

Uninstall:

```bash
./mac-helper/uninstall.sh
```

The installer generates the launchd plist for the current Mac. No machine-specific plist belongs in Git.
