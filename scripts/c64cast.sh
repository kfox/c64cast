#!/usr/bin/env bash
# Launch c64cast, forwarding all args. Two modes, picked by what you pass:
#   - config-driven:  scripts/c64cast.sh --config c64cast.toml
#   - quick playback:  scripts/c64cast.sh clip.mp4 tune.sid assets/pictures/
#     (positional MEDIA args build an in-memory playlist; one scene each,
#      played once. Point at hardware with -u, e.g. -u u64://192.168.2.64
#      or -u tr://. Needs the `yt` extra for non-direct URLs.)
#
# Runs via uv when present so the project .venv is always used (matches the
# mise + direnv + uv workflow); falls back to a bare `python` otherwise.
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
  exec uv run python -m c64cast "$@"
else
  exec python -m c64cast "$@"
fi
