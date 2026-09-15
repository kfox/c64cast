#!/usr/bin/env bash
# Launch c64cast, forwarding all args, through uv when it is on PATH.
# See CONTRIBUTING.md, "Running from a checkout".
set -euo pipefail
# Redirected because a relative cd echoes its destination when CDPATH is set.
cd "$(dirname "$0")/.." > /dev/null

# Without args c64cast starts a run rather than printing --help.
if [ "$#" -eq 0 ]; then
  set -- --help
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run python -m c64cast "$@"
else
  exec python -m c64cast "$@"
fi
