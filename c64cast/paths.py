"""Canonical locations for c64cast's machine-local settings + persisted data.

One place that answers "where does everything live", so the app works
identically from a repo checkout, a `pip install`, or a future PyPI wheel — no
`Path(__file__).parent.parent` repo-anchoring (which silently breaks for any
non-editable install). Two roots, each with an explicit env override:

  * **settings** — `settings_path()` → the machine-settings TOML
    (:mod:`c64cast.config.load_machine_settings`). Config base dir:
    `%APPDATA%\\c64cast\\` on Windows, else `$XDG_CONFIG_HOME/c64cast` or
    `~/.config/c64cast`. `$C64CAST_SETTINGS` overrides the whole path.
  * **data** — `data_root()` → the base for persisted machine-specific state
    (DAC calibrations, WLED + loop presets). Data base dir:
    `%LOCALAPPDATA%\\c64cast\\` on Windows, else `$XDG_DATA_HOME/c64cast` or
    `~/.local/share/c64cast`. `$C64CAST_DATA_DIR` overrides it.

Everything is a **function**, not a module constant: the env overrides are
read late (each call) so tests can point them at a tmp dir with a plain
``mock.patch.dict(os.environ, ...)``, and every call site is a cold path
(config load, calibration save, preset store construction) where the extra
`os.environ` lookup is free. Stdlib only, and deliberately **no intra-package
imports** — this module sits at the bottom of the dependency graph so anything
(config, transport, doctor, …) can import it without a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str) -> Path | None:
    """A non-empty environment variable as a Path, else None (an unset or
    empty value falls through to the platform default)."""
    val = os.environ.get(name)
    return Path(val) if val else None


def _config_base() -> Path:
    """Per-user config base directory (the parent of ``c64cast/``)."""
    if os.name == "nt":
        appdata = _env_path("APPDATA")
        return appdata if appdata is not None else Path.home() / "AppData" / "Roaming"
    xdg = _env_path("XDG_CONFIG_HOME")
    return xdg if xdg is not None else Path.home() / ".config"


def _data_base() -> Path:
    """Per-user data base directory (the parent of the ``c64cast`` data dir)."""
    if os.name == "nt":
        local = _env_path("LOCALAPPDATA")
        return local if local is not None else Path.home() / "AppData" / "Local"
    xdg = _env_path("XDG_DATA_HOME")
    return xdg if xdg is not None else Path.home() / ".local" / "share"


def settings_path() -> Path:
    """Path to the machine-settings TOML.

    ``$C64CAST_SETTINGS`` (if set) overrides the whole path; otherwise
    ``<config base>/c64cast/settings.toml``. The file need not exist —
    callers treat a missing file as "no machine settings"."""
    override = _env_path("C64CAST_SETTINGS")
    if override is not None:
        return override
    return _config_base() / "c64cast" / "settings.toml"


def data_root() -> Path:
    """Base directory for c64cast's persisted machine-specific data.

    ``$C64CAST_DATA_DIR`` (if set) overrides it; otherwise
    ``<data base>/c64cast``. Subdirectories are created lazily by the
    writers (via :func:`c64cast.transport.atomic_write_text` /
    ``mkdir(parents=True)``), so this never touches the filesystem."""
    override = _env_path("C64CAST_DATA_DIR")
    if override is not None:
        return override
    return _data_base() / "c64cast"


def calibration_dir() -> Path:
    """Directory holding per-system DAC calibration tables
    (``<data root>/calibration/dac``)."""
    return data_root() / "calibration" / "dac"


def presets_dir() -> Path:
    """Directory holding WLED device presets (``<data root>/presets``)."""
    return data_root() / "presets"


def loop_presets_dir() -> Path:
    """Directory holding per-video A/B loop presets
    (``<data root>/presets/loops``)."""
    return presets_dir() / "loops"


def legacy_data_root() -> Path | None:
    """The old repo-checkout data anchor (the package's parent directory),
    but only when it actually looks like the source checkout (a
    ``pyproject.toml`` sits there). Used *solely* by ``--doctor`` to detect
    calibration/preset files left at the old location and print the exact
    ``mv`` commands — there is no implicit migration. Returns None for an
    installed package (no repo checkout to migrate from)."""
    repo_root = Path(__file__).resolve().parent.parent
    if (repo_root / "pyproject.toml").is_file():
        return repo_root
    return None
