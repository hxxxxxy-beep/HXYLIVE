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

If Chrome saves files outside the library folder, pass `--chrome-download-dir`. Otherwise the helper watches Chrome Preferences plus `~/Downloads`.

Verify:

```bash
curl --noproxy '*' http://127.0.0.1:17899/health
```

Uninstall:

```bash
./mac-helper/uninstall.sh
```

The installer generates the launchd plist for the current Mac. No machine-specific plist belongs in Git.
