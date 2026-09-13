#!/bin/sh
set -eu

PLIST_PATH="$HOME/Library/LaunchAgents/com.hxylive.mac-helper.plist"
APP_DIR="$HOME/Library/Application Support/HXYLIVE"
GUI_DOMAIN="gui/$(id -u)"

launchctl bootout "$GUI_DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
if [ -f "$PLIST_PATH" ]; then
    rm "$PLIST_PATH"
fi
if [ -d "$APP_DIR" ]; then
    rm -R "$APP_DIR"
fi

printf '%s\n' 'HXYLIVE Mac Helper removed. Recordings in ~/Movies/HXYLIVE were preserved.'
