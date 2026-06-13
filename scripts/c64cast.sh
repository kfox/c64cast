#!/usr/bin/env bash
# Launch c64cast, forwarding all args. Runs via uv when present so the
# project .venv is always used (matches the mise + direnv + uv workflow);
# falls back to a bare `python` otherwise.
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
  exec uv run python -m c64cast "$@"
else
  exec python -m c64cast "$@"
fi
