"""Canonical locations for c64cast's machine-local settings + persisted data.

One place that answers "where does everything live", so the app works
identically from a repo checkout or an installed wheel — no
`Path(__file__).parent.parent` repo-anchoring (which silently breaks for any
non-editable install). Two roots, each with an explicit env override:

  * **settings** — `settings_path()` → the machine-settings TOML
    (:mod:`c64cast.app.config.load_machine_settings`). Config base dir:
    `%APPDATA%\\c64cast\\` on Windows, else `$XDG_CONFIG_HOME/c64cast` or
    `~/.config/c64cast`. `$C64CAST_SETTINGS` overrides the whole path.
  * **data** — `data_root()` → the base for persisted machine-specific state
    (DAC calibrations, WLED + loop presets, the dumped character ROM). Data base dir:
    `%LOCALAPPDATA%\\c64cast\\` on Windows, else `$XDG_DATA_HOME/c64cast` or
    `~/.local/share/c64cast`. `$C64CAST_DATA_DIR` overrides it.

Plus one read-only root that is *shipped* rather than machine-local:
`examples_dir()` → the packaged example configs (`c64cast/examples/`), which
`--config example:NAME` resolves against. See `resolve_config_spec`.

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


def expand_user(path: str) -> str:
    """Expand a leading ``~`` (or ``~user``) in a *user-supplied* path.

    Config files carry paths a human typed, and a human types ``~/Music/hvsc``.
    Nothing expands that for them: a shell does it for command-line arguments,
    but a TOML file has no shell, and both `os.path` and `glob` treat a leading
    ``~`` as a literal directory name — so an unexpanded path silently matches
    nothing (or raises "doesn't match expected extension" once it falls through
    to being treated as a literal filename).

    Call this where a path is **used**, not where it is loaded: a `Config` keeps
    the string the user wrote, so `config_serialize.dumps` round-trips ``~``
    instead of baking in an absolute home directory that stops being true on
    the next machine (or leaks a username into a config someone shares).
    """
    return os.path.expanduser(path)


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
    writers (via :func:`c64cast.control.transport.atomic_write_text` /
    ``mkdir(parents=True)``), so this never touches the filesystem."""
    override = _env_path("C64CAST_DATA_DIR")
    if override is not None:
        return override
    return _data_base() / "c64cast"


def calibration_dir() -> Path:
    """Directory holding per-system DAC calibration tables
    (``<data root>/calibration/dac``)."""
    return data_root() / "calibration" / "dac"


def unusable_capture_dir() -> Path:
    """Directory holding calibration captures that were refused
    (``<data root>/calibration/unusable``).

    A refused capture is the only record of *why* it was refused, and the run
    that produced it costs ~50 s of hardware time to repeat — twice now a
    calibration has been rejected on a number nobody could go back and explain,
    because the waveform was discarded on the way out."""
    return data_root() / "calibration" / "unusable"


def presets_dir() -> Path:
    """Directory holding WLED device presets (``<data root>/presets``)."""
    return data_root() / "presets"


def loop_presets_dir() -> Path:
    """Directory holding per-video A/B loop presets
    (``<data root>/presets/loops``)."""
    return presets_dir() / "loops"


def roms_dir() -> Path:
    """Directory holding ROM images dumped off the user's own machine
    (``<data root>/roms``; today just ``chargen.bin``, the character ROM
    :mod:`c64cast.hw.char_rom` reads back over DMA). Under the data root rather
    than the package because the bytes belong to the user's hardware — nothing
    here is ever shipped."""
    return data_root() / "roms"


def controllers_dir() -> Path:
    """Directory holding MIDI controller profiles written by ``--midi-setup``
    (``<data root>/controllers``; one ``<port-slug>.json`` per controller)."""
    return data_root() / "controllers"


def run_marker_path() -> Path:
    """The long-lived host's run marker (``<data root>/run.json``).

    Written when a session reaches ``running`` and removed when it comes down
    cleanly, so a marker found at the next start means the previous run died
    with the machine still mid-show — see
    :class:`c64cast.app.serve.SessionManager`."""
    return data_root() / "run.json"


def web_token_path() -> Path:
    """The web console's generated shared token (``<data root>/web_token``).

    Written ``0600`` the first time ``--serve`` runs without a token
    configured. It lives beside the DAC calibrations and WLED presets rather
    than in the config file so a config can be checked into a repo, and it is
    generated rather than optional because that surface starts and stops
    hardware — see :func:`c64cast.app.serve.resolve_tokens`."""
    return data_root() / "web_token"


# ---------------------------------------------------------------------------
# Packaged (read-only) resources
# ---------------------------------------------------------------------------

EXAMPLE_PREFIX = "example:"


def _package_dir() -> Path:
    """The installed ``c64cast`` package directory.

    Resolved through :mod:`importlib.resources` so whichever loader imported
    the package answers, but required to be a **real filesystem path**: a
    packaged config is handed to ``open()``, and an ensemble master names its
    per-system files relative to its own directory, so a ``Traversable`` that
    only offers ``read_text()`` could not run one. Every install shape c64cast
    supports (wheel, editable, source checkout) unpacks to real files; a
    zipimport would not, hence the explicit error rather than a later
    ``TypeError`` from ``open()``."""
    from importlib.resources import files

    res = files("c64cast")
    if not isinstance(res, Path):
        raise RuntimeError(
            "c64cast's packaged data files are not available as real files "
            f"({type(res).__name__}) — c64cast cannot run from a zipped "
            "distribution. Install it with uv and retry."
        )
    return res


def examples_dir() -> Path:
    """Directory holding the example configs that ship *inside* the package
    (``c64cast/examples/``, plus its ``ensemble/`` sub-directory), so
    ``--config example:NAME`` works with no checkout. See :func:`_package_dir`
    for why it must be a real path."""
    return _package_dir() / "examples"


def packaged_schema_path() -> Path:
    """The committed JSON Schema, which ships inside the package
    (``c64cast/data/c64cast.schema.json``) because every example config's
    first-line ``#:schema ../data/…`` directive resolves against it — from a
    checkout and from an install alike. Regenerate with ``make schema``."""
    return _package_dir() / "data" / "c64cast.schema.json"


def example_config_paths() -> list[Path]:
    """Every packaged example config: the single-file demos first, then the
    multi-file ones in sub-directories (``ensemble/``), each sorted by name."""
    root = examples_dir()
    return sorted(root.glob("*.toml")) + sorted(root.glob("*/*.toml"))


def example_name(path: Path) -> str:
    """The ``example:`` name of a packaged config — its path relative to
    :func:`examples_dir` without the suffix (``hello``, ``ensemble/master``)."""
    return path.relative_to(examples_dir()).with_suffix("").as_posix()


def resolve_example(name: str) -> Path:
    """Filesystem path of the packaged example config called ``name`` (with or
    without a trailing ``.toml``; ``ensemble/master`` selects a sub-directory
    demo).

    Raises ``ValueError`` — a *usage* error, not a config error — listing the
    closest matches when nothing matches, and for a name that escapes
    :func:`examples_dir` (``example:`` names a shipped file; anything else is
    an ordinary path and should be passed as one)."""
    import difflib

    stem = name[: -len(".toml")] if name.endswith(".toml") else name
    root = examples_dir()
    candidate = root / f"{stem}.toml"
    escapes = not candidate.resolve().is_relative_to(root.resolve())
    if escapes or not candidate.is_file():
        known = [example_name(p) for p in example_config_paths()]
        near = difflib.get_close_matches(stem, known, n=3)
        hint = f" Did you mean: {', '.join(near)}?" if near else ""
        raise ValueError(
            f"no packaged example config named {stem!r}.{hint} "
            "Run `--list-examples` to see them all, or pass a path to your "
            "own file instead of an `example:` name."
        )
    return candidate


def resolve_config_spec(spec: str | None) -> str | None:
    """Map a ``--config`` value to something ``open()`` accepts: an
    ``example:NAME`` reference becomes the packaged demo's path, anything else
    (including ``None`` — "no ``--config`` given") passes through untouched.

    One hook so every downstream consumer of the config path — the loader, the
    ensemble per-system resolver, ``--doctor``, the live-tune write-back —
    sees a real path and needs no knowledge of the prefix."""
    if spec is None or not spec.startswith(EXAMPLE_PREFIX):
        return spec
    return str(resolve_example(spec[len(EXAMPLE_PREFIX) :]))


def legacy_data_root() -> Path | None:
    """The old repo-checkout data anchor (the package's parent directory),
    but only when it actually looks like the source checkout (a
    ``pyproject.toml`` sits there). Used *solely* by :func:`legacy_presets_dir`
    to detect preset files left at the old location for a one-time migration
    heads-up — there is no implicit migration. Returns None for an installed
    package (no repo checkout to migrate from)."""
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "pyproject.toml").is_file():
        return repo_root
    return None


def legacy_presets_dir() -> Path | None:
    """The old repo-checkout ``presets/`` dir when it still holds orphaned
    preset files (``*.json``) *and* the canonical :func:`presets_dir` does not
    yet exist — i.e. a source checkout predating the canonical data dir that
    was never migrated. Used solely to emit a one-time heads-up
    (:func:`c64cast.control.transport.warn_if_legacy_presets_orphaned`); there is no
    implicit migration. Returns None for an installed package, a clean
    checkout, or once the canonical dir exists (already migrated)."""
    root = legacy_data_root()
    if root is None:
        return None
    legacy = root / "presets"
    if not legacy.is_dir() or not any(legacy.rglob("*.json")):
        return None
    if presets_dir().exists():
        return None
    return legacy
