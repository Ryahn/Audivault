#!/bin/sh
# Runs as root only long enough to make the app's own data folders writable,
# then drops to PUID:PGID so files written into your library (renames, imports,
# covers, metadata.json) are owned by your user, not root.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    for dir in "$AUDIVAULT_CONFIG_DIR" "$AUDIVAULT_METADATA_DIR"; do
        mkdir -p "$dir"
        # Only the app's own folders; audiobook/import folders are never chowned.
        chown -R "$PUID:$PGID" "$dir"
    done
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
