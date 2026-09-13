"""Video/audio streaming to Ultimate 64 hardware over the U64 REST API."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

UNINSTALLED_VERSION = "0+unknown"

try:
    __version__ = _pkg_version("c64cast")
except PackageNotFoundError:
    __version__ = UNINSTALLED_VERSION

__all__ = ["UNINSTALLED_VERSION", "__version__"]
