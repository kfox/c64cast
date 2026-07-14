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
# Redirect cd's stdout: if the caller's shell has CDPATH set, a relative cd
# echoes the resolved directory, which would otherwise leak into our output.
cd "$(dirname "$0")/.." > /dev/null

# With no args, argparse falls through to the built-in default config and
# tries (and fails) to connect to it. Show --help instead.
if [ "$#" -eq 0 ]; then
  set -- --help
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run python -m c64cast "$@"
else
  exec python -m c64cast "$@"
fi
