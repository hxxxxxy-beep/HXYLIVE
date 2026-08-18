# HXYLIVE Mac Helper

The helper is the Mac side of HXYLIVE. It listens only on `127.0.0.1:17899`, scans a selected video directory, and opens short-lived VPS download URLs in Google Chrome.

Install:

```bash
./mac-helper/install.sh --origin http://YOUR_VPS_IP:8080
```

Optional proxy (required on the development Mac that uses port 7897):

```bash
./mac-helper/install.sh \
  --origin http://YOUR_VPS_IP:8080 \
  --proxy http://127.0.0.1:7897
```

The helper watches Google Chrome's configured download folder. Matching completed files are moved into `video_dir/<streamer>/`. The video library is the destination, not an extra watch folder. Pass `--chrome-download-dir` only when Chrome Preferences cannot be read. `~/Downloads` is not watched unless it is Chrome's current download folder.

The helper also pushes folder snapshots to the VPS and claims download jobs and local open/delete commands there. The Media page can list Mac files, open them, and start downloads even when the browser cannot reach `127.0.0.1:17899` (Chrome local-network restrictions, or with the system proxy off). If `--proxy` is set and that proxy is down, the helper reaches the VPS directly.

Verify:

```bash
curl --noproxy '*' http://127.0.0.1:17899/health
```

Uninstall:

```bash
./mac-helper/uninstall.sh
```

The installer generates the launchd plist for the current Mac. No machine-specific plist belongs in Git.
