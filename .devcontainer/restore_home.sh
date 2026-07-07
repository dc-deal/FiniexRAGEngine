#!/bin/sh
# Restores locally saved developer home state (tool sessions, shell history)
# into a freshly created home volume, once. The seed directory is personal
# and gitignored; without it this is a no-op. The marker prevents overwriting
# newer state on subsequent container starts.
set -e
SEED="$(cd "$(dirname "$0")" && pwd)/local/home-seed"
MARKER="$HOME/.home-seed-restored"
if [ -d "$SEED" ] && [ ! -e "$MARKER" ]; then
    cp -a "$SEED/." "$HOME/"
    date -u +'%Y-%m-%dT%H:%M:%SZ' > "$MARKER"
fi
