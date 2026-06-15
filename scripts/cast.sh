#!/usr/bin/env bash
# Quick playback shortcut: one scene per file/URL argument, in order, no loop.
# Builds an in-memory config (no TOML on disk) and runs it. See
# c64cast/quickcast.py for the argument -> scene-type mapping.
#
# Examples:
#   scripts/cast.sh clip.mp4 tune.sid assets/pictures/
#   scripts/cast.sh -u http://192.168.2.64 'https://youtu.be/...'
#
# Runs via uv when present so the project .venv is always used (matches the
# mise + direnv + uv workflow); falls back to a bare `python` otherwise.
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
  exec uv run python -m c64cast.quickcast "$@"
else
  exec python -m c64cast.quickcast "$@"
fi
