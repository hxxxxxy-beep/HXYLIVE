#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage:
  ./mac-helper/install.sh --origin URL [--video-dir DIR] [--proxy URL] [--chrome-download-dir DIR]

Examples:
  ./mac-helper/install.sh --origin http://203.0.113.10:8080
  ./mac-helper/install.sh --origin https://hxylive.example.com --proxy http://127.0.0.1:7897
  ./mac-helper/install.sh --origin http://203.0.113.10:8080 \
      --video-dir /Volumes/External/HXYLIVE \
      --chrome-download-dir /Volumes/External/Downloads
EOF
}

ORIGIN=""
VIDEO_DIR="$HOME/Movies/HXYLIVE"
PROXY_URL=""
CHROME_DOWNLOAD_DIR=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --origin)
            ORIGIN="${2:-}"
            shift 2
            ;;
        --video-dir)
            VIDEO_DIR="${2:-}"
            shift 2
            ;;
        --proxy)
            PROXY_URL="${2:-}"
            shift 2
            ;;
        --chrome-download-dir)
            CHROME_DOWNLOAD_DIR="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$ORIGIN" in
    http://*|https://*) ;;
    *)
        printf '%s\n' '--origin must be an http:// or https:// URL' >&2
        exit 2
        ;;
esac

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    printf '%s\n' 'Python 3 is required. Install it first, then rerun this installer.' >&2
    exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/HXYLIVE"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
HELPER_PATH="$APP_DIR/hxylive_mac_helper.py"
PLIST_PATH="$LAUNCH_AGENTS_DIR/com.hxylive.mac-helper.plist"
LOG_PATH="$LOG_DIR/HXYLIVEMacHelper.log"
LABEL="com.hxylive.mac-helper"
GUI_DOMAIN="gui/$(id -u)"

mkdir -p "$APP_DIR" "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$VIDEO_DIR"
install -m 755 "$SCRIPT_DIR/hxylive_mac_helper.py" "$HELPER_PATH"

"$PYTHON_BIN" - "$PLIST_PATH" "$PYTHON_BIN" "$HELPER_PATH" "$VIDEO_DIR" "$ORIGIN" "$PROXY_URL" "$CHROME_DOWNLOAD_DIR" "$LOG_PATH" <<'PY'
import plistlib
import sys

(
    plist_path,
    python_bin,
    helper_path,
    video_dir,
    origin,
    proxy_url,
    chrome_download_dir,
    log_path,
) = sys.argv[1:]
arguments = [
    python_bin,
    "-u",
    helper_path,
    "--video-dir",
    video_dir,
    "--origin",
    origin,
]
if proxy_url:
    arguments.extend(["--proxy", proxy_url])
if chrome_download_dir:
    arguments.extend(["--chrome-download-dir", chrome_download_dir])

payload = {
    "Label": "com.hxylive.mac-helper",
    "ProgramArguments": arguments,
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": log_path,
    "StandardErrorPath": log_path,
}
with open(plist_path, "wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY

plutil -lint "$PLIST_PATH"
launchctl bootout "$GUI_DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "$GUI_DOMAIN" "$PLIST_PATH"

sleep 1
if ! curl --noproxy '*' --fail --silent --show-error --max-time 5 http://127.0.0.1:17899/health >/dev/null; then
    printf 'HXYLIVE Mac Helper did not become healthy. Check %s\n' "$LOG_PATH" >&2
    exit 1
fi

printf 'HXYLIVE Mac Helper installed and running.\n'
printf 'Video directory: %s\n' "$VIDEO_DIR"
printf 'Allowed web origin: %s\n' "$ORIGIN"
if [ -n "$CHROME_DOWNLOAD_DIR" ]; then
    printf 'Chrome download watch dir: %s\n' "$CHROME_DOWNLOAD_DIR"
else
    printf 'Chrome download watch dir: Chrome Preferences\n'
fi
printf 'Log: %s\n' "$LOG_PATH"
case "$VIDEO_DIR" in
    /Volumes/*)
        printf '\nNote: external disks need macOS privacy access for folder scans.\n'
        printf 'If Media → Refresh Mac folder shows 0 local files, open:\n'
        printf '  System Settings → Privacy & Security → Files and Folders\n'
        printf 'and allow Removable Volumes for Python and osascript / Finder.\n'
        printf 'After a successful scan, confirmed files are cached in\n'
        printf '~/Library/Application Support/HXYLIVE/download-ledger.json.\n'
        ;;
esac
