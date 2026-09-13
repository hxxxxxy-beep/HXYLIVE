#!/bin/sh
# Install the HXYLIVE Reality Clash Merge overlay so airport subscription refresh
# does not wipe VPS routing. Prefer Clash Verge Rev "Merge" profiles.
set -eu

usage() {
    cat <<'EOF'
Usage:
  ./mac-helper/apply-clash-reality-mixin.sh /path/to/clash-meta-mixin.yaml

Copies the Merge overlay to ~/.config/hxylive/ and, when Clash Verge Rev is
present, registers an enable-once Merge profile named "HXYLIVE Reality".

Re-import / refresh the airport Remote profile as usual; leave the Merge enabled.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
    usage >&2
    printf '%s\n' 'Missing mixin file. Copy clash-meta-mixin.yaml from the VPS first.' >&2
    exit 2
fi

if ! grep -q 'hxylive-reality' "$SRC" || ! grep -q 'prepend-proxies' "$SRC"; then
    printf '%s\n' "File does not look like HXYLIVE clash-meta-mixin.yaml: $SRC" >&2
    exit 2
fi

DEST_DIR="${HOME}/.config/hxylive"
DEST_MIXIN="$DEST_DIR/clash-reality-mixin.yaml"
mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST_MIXIN"
chmod 600 "$DEST_MIXIN"
printf '%s\n' "Installed durable mixin: $DEST_MIXIN"

VERGE_DIR=""
for candidate in \
    "${HOME}/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev" \
    "${HOME}/Library/Application Support/clash-verge-rev" \
    "${HOME}/.config/clash-verge-rev" \
    "${HOME}/.local/share/io.github.clash-verge-rev.clash-verge-rev"
do
    if [ -d "$candidate/profiles" ]; then
        VERGE_DIR="$candidate"
        break
    fi
done

if [ -z "$VERGE_DIR" ]; then
    cat <<EOF
Clash Verge Rev profiles directory not found.

Manual (any Clash Meta client with Merge/Override/mixin):
  1. Keep your airport as the main/Remote profile.
  2. Create or edit a Merge/Override layer; paste contents of:
       $DEST_MIXIN
  3. Enable that Merge and refresh the airport once.
  4. Confirm mixed-port / system proxy still listens on 127.0.0.1:7897.

Do not paste Reality into the airport subscription body.
EOF
    exit 0
fi

PROFILES_DIR="$VERGE_DIR/profiles"
PROFILES_YAML="$VERGE_DIR/profiles.yaml"
MERGE_FILE="Merge_hxylive.yaml"
MERGE_UID="Merge_hxylive"
MERGE_NAME="HXYLIVE Reality"

cp "$DEST_MIXIN" "$PROFILES_DIR/$MERGE_FILE"
chmod 600 "$PROFILES_DIR/$MERGE_FILE"
printf '%s\n' "Wrote Clash Verge Merge file: $PROFILES_DIR/$MERGE_FILE"

if [ ! -f "$PROFILES_YAML" ]; then
    printf '%s\n' "Missing $PROFILES_YAML — open Clash Verge once, then re-run this script." >&2
    exit 1
fi

BACKUP="$PROFILES_YAML.bak-hxylive-$(date +%Y%m%d%H%M%S)"
cp "$PROFILES_YAML" "$BACKUP"

python3 - "$PROFILES_YAML" "$MERGE_UID" "$MERGE_FILE" "$MERGE_NAME" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
uid = sys.argv[2]
file_name = sys.argv[3]
name = sys.argv[4]
text = path.read_text(encoding="utf-8")

if re.search(rf"(?m)^\s*(uid:\s*{re.escape(uid)}|file:\s*{re.escape(file_name)})\s*$", text):
    print(f"Merge profile already registered in {path} (file refreshed).")
    sys.exit(0)

item = (
    f"  - uid: {uid}\n"
    f"    type: merge\n"
    f"    name: {name}\n"
    f"    file: {file_name}\n"
)

if re.search(r"(?m)^items:\s*$", text):
    text = re.sub(r"(?m)^items:\s*$", "items:\n" + item.rstrip("\n"), text, count=1)
elif re.search(r"(?m)^items:\s*\n", text):
    text = re.sub(r"(?m)^(items:\s*\n)", r"\1" + item, text, count=1)
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += "items:\n" + item

path.write_text(text, encoding="utf-8")
print(f"Registered Merge profile '{name}' in {path}")
PY

cat <<EOF
Clash Verge Rev next steps (once):
  1. Open Profiles.
  2. Find "${MERGE_NAME}" (Merge type) → right-click → Enable.
  3. Select / refresh your airport Remote profile.
  4. Keep mixed-port on 127.0.0.1:7897.

After that, re-importing or refreshing the airport keeps VPS routing via Reality.
Backup of profiles.yaml: $BACKUP
EOF
