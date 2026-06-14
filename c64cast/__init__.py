"""Video/audio streaming to Ultimate 64 hardware over the U64 REST API."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("c64cast")
except PackageNotFoundError:
    # Package not installed (running from a source checkout without
    # `pip install -e .`). Fall back to a sentinel rather than crashing.
    __version__ = "0+unknown"

__all__ = ["__version__"]
