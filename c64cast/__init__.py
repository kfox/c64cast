"""Video/audio streaming to Ultimate 64 hardware over the U64 REST API."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# `__version__` when no installed distribution claims this package. Named
# because two surfaces — `--version` and `--doctor` — have to recognize the
# sentinel to explain it, and a bare literal in three files drifts.
UNINSTALLED_VERSION = "0+unknown"

try:
    __version__ = _pkg_version("c64cast")
except PackageNotFoundError:
    # Package not installed (running from a source checkout without
    # `uv sync`). Fall back to a sentinel rather than crashing.
    __version__ = UNINSTALLED_VERSION

__all__ = ["UNINSTALLED_VERSION", "__version__"]
