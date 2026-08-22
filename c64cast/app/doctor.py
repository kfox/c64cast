"""Configuration + environment diagnostics.

Collects every per-scene/per-overlay/per-orchestrator validation failure
across every system in the loaded config (instead of failing fast on the
first one), probes which optional install extras are importable, and
optionally pings each system's U64 to verify DMA-service reachability.
The `--doctor` CLI flag dispatches here and prints the resulting report.

The validation surface is shared with `scene_factory.build_scene` via
`scene_factory.validate_scene_cfg` — there is no parallel registry of probes.
Everything here only OBSERVES: the live REU/sampler auto-provisioning that
used to sit alongside the probes now lives in `hw_provision`, and the probes
import its `wants_reu`/`wants_sampler` predicates so the report and the
provisioner can't disagree about what a run needs.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

from c64cast.hw import hw_provision
from c64cast.hw.c64 import max_safe_sample_rate, nmi_rate_safety
from c64cast.sid import emusid_mixer

from .config import ColorCfg, Config, ConfigError, LoadResult, resolve_recording_path, scene_color
from .orchestrator import OrchestratorError
from .paths import expand_user
from .scene_factory import (
    cell_strategy_cfg_error,
    color_match_cfg_error,
    dither_cfg_error,
    motion_smoothing_cfg_error,
    resolve_cell_strategy,
    resolve_color_match,
    resolve_dither_method,
    resolve_scene_display,
    resolve_wled_broadcast,
    resolve_wled_listen,
    validate_control_cfg,
    validate_dac_bitmap_tempo_cfg,
    validate_dac_curve_cfg,
    validate_midi_control_cfg,
    validate_scene_cfg,
    validate_sid_model_cfg,
    validate_wled_cfg,
)
from .upgrade import install_root

log = logging.getLogger(__name__)

Level = Literal["ok", "warn", "error"]


@dataclass(frozen=True)
class Diagnostic:
    level: Level
    category: str  # "scene" | "orchestrator" | "extras" | "connectivity"
    subject: str  # "<system>/<scene-name>" or "<extras-name>"
    message: str
    hint: str | None = None


# (extras_name, top-level module name, one-line description of what uses it).
# Keep in sync with [project.optional-dependencies] in pyproject.toml.
_EXTRAS: tuple[tuple[str, str, str], ...] = (
    ("mic", "sounddevice", "[audio] enabled, mic capture"),
    ("video", "av", "video scenes, video interleaving"),
    ("control", "fastapi", "[control] enabled HTTP plane"),
    ("obs", "obsws_python", "obs_status overlay"),
    ("midi", "mido", "midi scenes; [midi_control] live control"),
    ("logging", "rich", "colored log output"),
    ("vision", "mediapipe", "[vision] enabled gesture control"),
    ("camera", "cv2_enumerate_cameras", "[video].device by name/VID:PID; --list-devices detail"),
    ("tr", "serial", "TeensyROM serial backend"),
    ("wizard", "questionary", "--init config wizard"),
    ("yt", "yt_dlp", "cast URL playback (YouTube et al.)"),
    ("wled", "zeroconf", "[wled].listen virtual WLED device"),
    # Probed on `websockets` rather than fastapi: `control` already covers
    # fastapi, and the state feed is the part that silently does nothing when
    # uvicorn has no WebSocket implementation to upgrade with.
    ("web", "websockets", "--serve web console host"),
)

# Hard dependencies (top-level module, what uses it). These are declared in
# [project].dependencies and MUST import — a missing one means the active
# interpreter isn't the synced project env (the classic "No module named cv2"
# time-sink: bare `python` resolving to a non-.venv interpreter, or a partially
# synced .venv).
_HARD_DEPS: tuple[tuple[str, str], ...] = (
    ("cv2", "opencv-python: video decode + palette quantize"),
    ("numpy", "array math everywhere"),
    ("requests", "U64 REST transport"),
    ("py65", "host-side SID emulator"),
)

# Repo root (parent of the package dir; this file sits two levels below the
# package). Used to locate the project .venv and run `uv lock --check` from
# the right directory. `install_root()` is the single home for this
# expression — `cli.py`'s `_version_text` and `upgrade.py`'s install
# detection both call it too, rather than each computing it independently.
_REPO_ROOT = install_root()


def _running_from_checkout() -> bool:
    """True when the package is being run out of its own source tree.

    For an installed package ``_REPO_ROOT`` is ``site-packages``, which
    has no ``pyproject.toml`` — so the dev-environment probes below (project
    .venv, uv.lock drift) have nothing to check and every answer they give is
    noise. Worse than noise, in the uv.lock case: ``uv lock --check`` exits
    nonzero for "no project found" exactly as it does for real drift, so an
    installed user was told their lockfile had drifted from a pyproject.toml
    they don't have.

    Same "a pyproject.toml here means a source checkout" test that
    :func:`paths.legacy_data_root` uses, kept local because ``paths`` is
    deliberately import-free at the bottom of the dependency graph.
    """
    return (_REPO_ROOT / "pyproject.toml").is_file()


def validate_load_result(
    loaded: LoadResult,
    *,
    probe_u64: bool = True,
    probe_environment: bool = True,
    probe_updates: bool = False,
) -> list[Diagnostic]:
    """Run every config + environment check and collect the results.

    `probe_environment=False` skips the installation-level checks (venv,
    hard deps, uv.lock, machine settings, data dirs, char ROM, extras) —
    real disk I/O that answers "is this machine set up right", not "is this
    config good to launch". Those facts don't change per config and don't
    change per click, so `config_store.validate_ref`'s pre-flight (run on
    every Start/Switch) skips them; `--doctor` still runs them every time.

    `probe_updates` is its own, separately-defaulted-off switch rather than
    folded into `probe_environment`: it is the one probe here that hits the
    network unconditionally (a PyPI query, not a U64), and it answers a
    question ("is a newer release out") that has nothing to do with whether
    *this* machine is set up right — so a caller that wants the offline
    installation checks doesn't have to also accept a network call to get
    them. Defaults to False so `config_store.validate_ref`'s pre-flight, and
    any other caller that doesn't ask for it, stays offline unchanged.

    Per-scene validation runs `validate_scene_cfg` inside try/except so a
    single broken scene doesn't hide the others. Cross-system orchestrator
    coverage (a conductor scene must have a same-name follower in every
    other system) is warn-level because the Playlist will fall back to the
    conductor's cfg — but that's rarely what the user actually wants.
    """
    out: list[Diagnostic] = []

    if probe_environment:
        out.extend(_probe_environment())
        out.extend(_probe_machine_settings())
        out.extend(_probe_data_dirs())
        out.extend(_probe_char_rom())
    if probe_updates:
        out.extend(_probe_updates())
    out.extend(_validate_unknown_keys(loaded))
    out.extend(_validate_schema_directive(loaded))
    out.extend(_validate_scenes(loaded))
    out.extend(_validate_audio_nmi_rate(loaded))
    out.extend(_validate_dac_curve_cfg(loaded))
    out.extend(_validate_dac_bitmap_tempo(loaded))
    out.extend(_validate_sid_model(loaded))
    out.extend(_validate_dither(loaded))
    out.extend(_validate_color_match(loaded))
    out.extend(_validate_cell_strategy(loaded))
    out.extend(_validate_motion_smoothing(loaded))
    out.extend(_validate_control(loaded))
    out.extend(_validate_midi_control(loaded))
    out.extend(_validate_wled(loaded))
    if loaded.is_ensemble:
        out.extend(_validate_cross_system_orchestration(loaded))
        out.extend(_validate_ensemble_recording_paths(loaded))
    out.extend(_probe_extras())

    # dac_curve resolution ("auto"/"calibrated" -> an actual table) is
    # hardware-identity-dependent (see _validate_dac_curve_resolution), so a
    # live per-system answer from _probe_connectivity (precise — reads the
    # live device identity) always wins over the offline guess. Only systems
    # that didn't get a live answer (skip-probe entirely, or that one
    # system's connectivity probe failed) fall back to the offline,
    # hedged report.
    connectivity: list[Diagnostic] = []
    live_dac_names: frozenset[str] = frozenset()
    if probe_u64:
        connectivity = _probe_connectivity(loaded)
        live_dac_names = frozenset(
            d.subject[: -len(" (DAC calibration)")]
            for d in connectivity
            if d.subject.endswith(" (DAC calibration)")
        )
    out.extend(_validate_dac_curve_resolution(loaded, skip_names=live_dac_names))
    out.extend(connectivity)

    return out


def _probe_environment() -> list[Diagnostic]:
    """Catch the dev-environment failure that costs the most time: the active
    interpreter isn't the synced project env, so a hard dependency (cv2, …)
    won't import. Reports the c64cast version and the interpreter, asserts
    every hard dep imports, and best-effort checks uv.lock vs pyproject.toml.
    Offline; runs in every doctor invocation (including `--skip-probe`)."""
    out: list[Diagnostic] = []

    # First line of any bug report. `__version__` reads installed metadata and
    # falls back to "0+unknown" in a source checkout that was never installed —
    # say so plainly rather than showing a bare sentinel nobody can interpret.
    from c64cast import UNINSTALLED_VERSION, __version__

    if __version__ == UNINSTALLED_VERSION:
        detail = f"{__version__} (not installed — running from a source checkout)"
    else:
        detail = __version__
    out.append(Diagnostic("ok", "environment", "c64cast version", detail))

    # Active interpreter vs the project .venv. Only flag a mismatch when a
    # project .venv actually exists — an installed package legitimately runs
    # from some other prefix and has nothing to compare against.
    venv = _REPO_ROOT / ".venv"
    if venv.exists():
        if Path(sys.prefix).resolve() == venv.resolve():
            out.append(
                Diagnostic("ok", "environment", "interpreter", f"project .venv ({sys.executable})")
            )
        else:
            out.append(
                Diagnostic(
                    "warn",
                    "environment",
                    "interpreter",
                    f"{sys.executable} is not the project .venv ({venv})",
                    hint=(
                        "Run via `uv run` / `make` (or let direnv+mise activate "
                        ".venv) so tools and the app use the synced project env."
                    ),
                )
            )
    else:
        out.append(Diagnostic("ok", "environment", "interpreter", sys.executable))

    # Hard deps must import. A miss here is the root of the cv2-missing sessions.
    for module, used_for in _HARD_DEPS:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            out.append(
                Diagnostic(
                    "error",
                    "environment",
                    module,
                    f"hard dependency not importable (used for: {used_for})",
                    hint="Env is out of sync — run `make sync` (uv sync --all-extras).",
                )
            )
        else:
            out.append(Diagnostic("ok", "environment", module, "importable"))

    out.extend(_probe_opencv_provider())

    if _running_from_checkout():
        out.extend(_probe_uv_lock())
    return out


def _probe_updates() -> list[Diagnostic]:
    """Best-effort PyPI check for a newer c64cast release. Reuses the
    ENVIRONMENT category so the row sits beside the `c64cast version` line it
    answers (:func:`_probe_environment`), rather than opening a category of
    its own for one row.

    Meaningless in a source checkout (there's no installed distribution to
    upgrade) — skip rather than query. Never `error`: a network hiccup here
    says nothing about whether the install itself is broken, so it can only
    ever `warn` (an update exists) or report `ok` (current, or the check
    itself couldn't run). See :func:`c64cast.app.upgrade.run_upgrade` for the
    command this Diagnostic's hint points at."""
    from c64cast import __version__

    from .upgrade import is_newer, latest_release

    if _running_from_checkout():
        return [Diagnostic("ok", "environment", "update check", "skipped (source checkout)")]

    remote = latest_release()
    if remote is None:
        return [Diagnostic("ok", "environment", "update check", "skipped (could not reach PyPI)")]

    newer = is_newer(remote, __version__)
    if newer is None:
        return [Diagnostic("ok", "environment", "update check", f"could not compare to {remote}")]
    if newer:
        return [
            Diagnostic(
                "warn",
                "environment",
                "update check",
                f"{remote} is available (running {__version__})",
                hint="c64cast --upgrade",
            )
        ]
    return [Diagnostic("ok", "environment", "update check", f"up to date ({__version__})")]


def _probe_opencv_provider() -> list[Diagnostic]:
    """Report which opencv build actually occupies the `cv2` namespace.

    Every opencv wheel — plain, contrib, headless — unpacks to the same
    `site-packages/cv2/`, and pip/uv let all of them install because they are
    different distributions. Only one set of files can survive, so a second
    one silently replaces the version this project pinned, and nothing in
    `uv.lock`, `uv pip list` or the dependency metadata says so. `[vision]`
    pulls mediapipe, which depends on opencv-contrib-python, so `[all]` is
    exactly where it happens.

    Reads cv2's own build stamp rather than the distribution metadata, because
    metadata reports what each wheel *claims* and the stamp reports what is
    actually on disk — which is the whole question here."""
    try:
        providers = sorted(importlib.metadata.packages_distributions().get("cv2", []))
    except Exception:  # metadata unreadable: nothing to say
        return []
    if not providers:
        return []

    build = ""
    flags: list[str] = []
    try:
        # Imported by name because cv2's stubs don't declare the `version`
        # submodule, even though every wheel generates one.
        version_mod: Any = importlib.import_module("cv2.version")
        build = str(version_mod.opencv_version)
        flags = [
            label
            for label, on in (
                ("contrib", bool(version_mod.contrib)),
                ("headless", bool(version_mod.headless)),
            )
            if on
        ]
    except Exception:  # not importable — the hard-dep probe above says so
        pass
    effective = f"{build} [{', '.join(flags)}]" if flags else build

    if len(providers) > 1:
        installed = ", ".join(f"{d} {_dist_version(d)}" for d in providers)
        return [
            Diagnostic(
                "warn",
                "environment",
                "opencv",
                f"{len(providers)} opencv distributions share the `cv2` "
                f"namespace ({installed}); whichever installed last wins"
                + (f", and that is {effective}" if build else ""),
                hint=(
                    "Expected with the `vision` extra (mediapipe depends on "
                    "opencv-contrib-python) and harmless — contrib is a "
                    "superset. Install without `vision` if you need the "
                    "pinned opencv-python to be the one that loads."
                ),
            )
        ]

    if "headless" in flags:
        return [
            Diagnostic(
                "warn",
                "environment",
                "opencv",
                f"{providers[0]} {effective} — a headless build has no GUI, "
                "so [preview] cannot open a window ([recording] still works)",
                hint="Install a non-headless opencv wheel to get the preview window.",
            )
        ]

    detail = f"{providers[0]} {effective}" if build else providers[0]
    return [Diagnostic("ok", "environment", "opencv", detail)]


def _dist_version(dist: str) -> str:
    try:
        return importlib.metadata.version(dist)
    except Exception:
        return "?"


def _probe_uv_lock() -> list[Diagnostic]:
    """Best-effort `uv lock --check` — warns when uv.lock has drifted from
    pyproject.toml (CI installs `--frozen`, so drift breaks CI). Skips cleanly
    when the uv CLI isn't on PATH. Only called from a source checkout — see
    :func:`_running_from_checkout`."""
    if shutil.which("uv") is None:
        return [Diagnostic("ok", "environment", "uv.lock", "skipped (uv not on PATH)")]
    try:
        r = subprocess.run(
            ["uv", "lock", "--check"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [Diagnostic("warn", "environment", "uv.lock", f"could not check ({e})")]
    if r.returncode == 0:
        return [Diagnostic("ok", "environment", "uv.lock", "up to date with pyproject.toml")]
    return [
        Diagnostic(
            "warn",
            "environment",
            "uv.lock",
            "out of date with pyproject.toml",
            hint="Run `uv lock`, then `make sync` (uv sync --all-extras).",
        )
    ]


def _probe_machine_settings() -> list[Diagnostic]:
    """Report the machine-settings file (:func:`paths.settings_path`): absent,
    present + which sections it sets, a parse failure, or a rejected
    ``[scenes]``/``[ensemble]`` section. Offline — part of the ENVIRONMENT
    section's one-stop "where everything lives" answer."""
    import tomllib

    from . import paths

    path = paths.settings_path()
    if not path.is_file():
        return [Diagnostic("ok", "environment", "machine settings", f"none ({path})")]
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [
            Diagnostic(
                "error",
                "environment",
                "machine settings",
                f"could not parse {path}: {e}",
                hint="Fix the TOML syntax, or move the file aside.",
            )
        ]
    banned = [s for s in ("scenes", "ensemble") if s in data]
    sections = sorted(s for s in data if s not in ("scenes", "ensemble"))
    detail = ", ".join(sections) if sections else "no recognized sections"
    out = [Diagnostic("ok", "environment", "machine settings", f"{path} — {detail}")]
    for b in banned:
        out.append(
            Diagnostic(
                "warn",
                "environment",
                "machine settings",
                f"[{b}] in {path} is ignored — machine settings hold "
                "cross-run defaults, not playlists",
            )
        )
    return out


def _probe_data_dirs() -> list[Diagnostic]:
    """Report the resolved data root (:func:`paths.data_root`) and controllers
    dir — the one-stop "where does everything live". There is no legacy-repo
    migration nudge here any more: stale files from the pre-canonical-data-dir
    layout are surfaced at use time instead — DAC calibration by
    ``dac_curve_resolve.resolve_dac_curve_for_backend`` (at curve resolution),
    orphaned presets by ``transport.warn_if_legacy_presets_orphaned`` (at
    preset-store load)."""
    from . import paths

    return [
        Diagnostic("ok", "environment", "data dir", str(paths.data_root())),
        Diagnostic("ok", "environment", "controllers dir", str(paths.controllers_dir())),
    ]


def _probe_char_rom() -> list[Diagnostic]:
    """Report the resolved character ROM and its verdict, or its absence.

    Absence is a *warning*, not an error: c64cast still runs, it just draws C64
    text in a cv2-rendered ASCII font instead of the real thing — which is
    exactly the "the scrolling text looks bad" report this whole path exists to
    answer, and it is invisible unless someone says so out loud."""
    from c64cast.hw import char_rom

    path = char_rom.resolve()
    if path is None:
        return [
            Diagnostic(
                "warn",
                "environment",
                "character ROM",
                f"not installed ({char_rom.installed_path()}) — C64 text renders "
                "in a built-in ASCII font, not the C64 one",
                hint=(
                    "Connect your C64 and run `c64cast --dump-char-rom` (a plain "
                    "run does it automatically), or `c64cast --install-char-rom PATH`."
                ),
            )
        ]
    try:
        result = char_rom.verify(path.read_bytes())
    except OSError as e:
        return [
            Diagnostic(
                "error",
                "environment",
                "character ROM",
                f"could not read {path}: {e}",
                hint="Re-dump it with `c64cast --dump-char-rom`.",
            )
        ]
    return [
        Diagnostic(
            "ok" if result.ok else "error",
            "environment",
            "character ROM",
            f"{path} — {result.describe()}",
            hint=(
                None
                if result.ok
                else "Re-dump it with `c64cast --dump-char-rom` — this file is not a charset."
            ),
        )
    ]


def _validate_unknown_keys(loaded: LoadResult) -> list[Diagnostic]:
    """Report every TOML key no dataclass field accepts as a warn-level row.

    These are collected during load (`config.UnknownKey`) rather than logged
    there, because a log line printed above the report is exactly where a
    misplaced key hides: it reads as preamble next to the formatted rows, and
    the run continues silently on defaults. Warn, not error — an ignored key
    still leaves a runnable config, and failing the whole report would make a
    stale key from an older schema unbootable."""
    out: list[Diagnostic] = []
    for rec in loaded.unknown_keys:
        subject = f"{rec.source}: [{rec.section}]" if rec.source else f"[{rec.section}]"
        out.append(
            Diagnostic(
                level="warn",
                category="config",
                subject=subject,
                message=f"unknown key {rec.key!r} — ignored, this setting has no effect",
                hint=rec.hint,
            )
        )
    return out


def _packaged_schema() -> Path:
    """This install's committed JSON Schema. Wrapped so both halves of the
    ``#:schema`` check reach it the same way, and so a test can point them at a
    fixture without also moving the example configs."""
    from . import paths

    return paths.packaged_schema_path()


def _schema_directive(path: str) -> str | None:
    """The ``#:schema`` value on line 1 of the config at `path`, or None when
    there isn't one (the directive is optional) or the file can't be read (the
    loader already failed louder than this check ever would)."""
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return None
    return first[len("#:schema ") :].strip() or None if first.startswith("#:schema ") else None


def _validate_schema_directive(loaded: LoadResult) -> list[Diagnostic]:
    """Report a ``#:schema`` first line that no longer describes this install.

    The line is what gives a TOML-aware editor completion and typo flagging, and
    it is the one thing an upgrade can leave behind: a config pointing at a
    *snapshot* of the schema (a version-pinned URL, or a copy inside an install
    that has since moved) keeps checking against the c64cast the reader had when
    they wrote it. Nothing breaks — the loader never reads this line — which is
    exactly why it needs saying out loud: the symptom is an editor quietly
    underlining a setting that works, or offering one that doesn't.

    Diagnosis only, deliberately. ``config_store`` carries a file's own directive
    across a save rather than regenerating it, because a team's shared or
    hand-picked schema is a legitimate answer; so is a pin, for someone holding a
    config to an older release on purpose. This says what it sees and prints the
    line to paste, and leaves the file alone.

    A config with **no** directive gets no row: it's an optional line, and a
    report that names every config lacking one is advertising, not diagnosis."""
    # Read late (not at module import) so a test can patch either one.
    from c64cast import __version__

    from . import config_serialize

    out: list[Diagnostic] = []
    for path in loaded.paths:
        if path is None:
            continue
        value = _schema_directive(path)
        if value is None:
            continue

        subject = f"{os.path.basename(path)} line 1"
        want = config_serialize.schema_directive_for(path)
        hint = f"Replace line 1 with `#:schema {want}`, or run `c64cast --print-schema-path`."

        pinned = config_serialize.pinned_url_version(value)
        if pinned is not None and pinned != __version__:
            out.append(
                Diagnostic(
                    level="warn",
                    category="config",
                    subject=subject,
                    message=f"#:schema is pinned to v{pinned} — your editor checks this "
                    f"file against c64cast {pinned}, not the {__version__} you run",
                    hint=hint,
                )
            )
            continue
        if pinned is not None:
            out.append(
                Diagnostic(
                    level="ok",
                    category="config",
                    subject=subject,
                    message=f"#:schema pinned to v{pinned} (this version)",
                )
            )
            continue
        if value.startswith(("http://", "https://")):
            # Somebody else's URL — a fork, a mirror, a team's copy. Nothing
            # here can tell whether it's right, and guessing would be noise.
            continue

        named = Path(os.path.join(os.path.dirname(os.path.abspath(path)), value))
        if named.name != _packaged_schema().name:
            # A deliberately hand-picked schema (`./house-style.schema.json`),
            # not a stale pointer at ours.
            continue
        out.extend(_compare_named_schema(named, subject, hint))
    return out


def _compare_named_schema(named: Path, subject: str, hint: str) -> list[Diagnostic]:
    """Judge a ``#:schema`` line that names a copy of *our* schema by content
    rather than by location: any copy identical to the one this install
    generates is doing its job, whichever tree it sits in, and a copy that
    differs is describing a different c64cast whatever it is called."""
    mine = _packaged_schema()
    try:
        theirs = named.read_text(encoding="utf-8")
    except OSError:
        return [
            Diagnostic(
                level="warn",
                category="config",
                subject=subject,
                message=f"#:schema names {named}, which isn't there — an install that "
                "moved, or an upgrade onto a new Python version",
                hint=hint,
            )
        ]
    if theirs != mine.read_text(encoding="utf-8"):
        return [
            Diagnostic(
                level="warn",
                category="config",
                subject=subject,
                message=f"#:schema names a schema that isn't the one this install "
                f"generates ({named}) — an editor will judge this file by it",
                hint=hint,
            )
        ]
    return [
        Diagnostic(
            level="ok",
            category="config",
            subject=subject,
            message="#:schema tracks this install",
        )
    ]


def _validate_scenes(loaded: LoadResult) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        for idx, s in enumerate(cfg.scenes):
            label = s.name or f"{s.type}#{idx}"
            subject = f"{name}/{label}"
            try:
                validate_scene_cfg(s, cfg, audio_enabled=cfg.audio.enabled)
            except OrchestratorError as e:
                out.append(
                    Diagnostic(
                        level="error", category="orchestrator", subject=subject, message=str(e)
                    )
                )
            except ValueError as e:
                out.append(
                    Diagnostic(level="error", category="scene", subject=subject, message=str(e))
                )
            else:
                role = " (follower-only)" if s.follower_only else ""
                extra = ""
                if s.type == "asid":
                    mode = s.asid_buffered_player
                    extra = (
                        ", buffered ring player (REU)"
                        if mode in ("auto", "on")
                        else ", coalesced flush"
                    )
                    if mode == "auto":
                        extra += " when REU present"
                display = resolve_scene_display(s.display, s.type)
                out.append(
                    Diagnostic(
                        level="ok",
                        category="scene",
                        subject=subject,
                        message=f"{s.type}/{display}, {len(s.overlays)} overlay(s){role}{extra}",
                    )
                )
    return out


def _validate_audio_nmi_rate(loaded: LoadResult) -> list[Diagnostic]:
    """Flag [audio].sample_rate values that overrun (error) or risk overrunning
    (warn) the $D418 NMI handler on each system's target standard. Offline —
    pure cycle-budget math via c64.nmi_rate_safety, no hardware needed."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        if not cfg.audio.enabled:
            continue
        system = cfg.ultimate64.system
        rate = cfg.audio.sample_rate
        level, message = nmi_rate_safety(system, rate)
        if level != "ok":
            out.append(
                Diagnostic(
                    level=level,
                    category="audio",
                    subject=f"{name}/sample_rate",
                    message=message,
                    hint=(
                        "Lower [audio].sample_rate — default 10500 is safe on NTSC + "
                        "PAL; NTSC tolerates ~11025, keep PAL <= ~10500."
                    ),
                )
            )
            continue
        # Adaptive compensation needs latch headroom (max_safe_rate above the
        # configured rate) to raise the NMI rate over bus-halt loss. Too little
        # → it can't fully cancel the video slowdown (acute on PAL's tighter
        # clock). Warn so the user lowers the rate or accepts residual slowness.
        if cfg.audio.nmi_rate_adaptive:
            headroom = max_safe_sample_rate(system) / rate - 1.0
            if headroom < 0.03:
                out.append(
                    Diagnostic(
                        level="warn",
                        category="audio",
                        subject=f"{name}/sample_rate",
                        message=(
                            f"nmi_rate_adaptive has only {headroom * 100:.1f}% NMI "
                            f"headroom at {rate} Hz on {system} — it can't fully "
                            f"compensate heavy-video slowdown."
                        ),
                        hint=(
                            f"Lower [audio].sample_rate (more headroom) — {system} "
                            f"max safe is ~{max_safe_sample_rate(system)} Hz."
                        ),
                    )
                )
    return out


def _validate_dac_curve_cfg(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [audio].dac_curve name or the dac_curve + digi_boost
    conflict per system. Pure config validation — no hardware/calibration
    involved — so it always runs, live or offline. Delegates to
    config.validate_dac_curve_cfg. See _validate_dac_curve_resolution for
    the (hardware-identity-dependent) "resolves to X" reporting."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_dac_curve_cfg(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="audio",
                    subject=f"{name}/dac_curve",
                    message=str(e),
                    hint="See [audio].dac_curve in the config reference / --describe section:audio.",
                )
            )
    return out


def _validate_dac_curve_resolution(
    loaded: LoadResult, *, skip_names: frozenset[str] = frozenset()
) -> list[Diagnostic]:
    """Report how a system-aware [audio].dac_curve ("auto"/"calibrated")
    resolves, for every system NOT in `skip_names` (those already got a
    precise LIVE answer from _probe_dac_calibration_status — see
    validate_load_result — so re-reporting an offline guess for them would
    just be redundant and potentially contradictory).

    This is inherently best-effort: it resolves with no live backend
    (be=None), so on the Ultimate / a serial TeensyROM (no
    dac_calibration_profile override) it can only use the offline fallback
    key, not the live device identity (unique_id / USB serial) a real run
    would use — see dac_calibration_store.offline_key_is_authoritative. A miss
    against that fallback key doesn't prove no calibration applies, so when
    calibration files exist on disk for this backend that the fallback key
    can't confirm or rule out, the message/error is hedged rather than
    asserting a possibly-wrong resolution."""
    from c64cast.audio import dac_calibration_store, dac_curve_resolve

    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        if name in skip_names:
            continue
        try:
            validate_dac_curve_cfg(cfg)
        except ConfigError:
            continue  # already reported by _validate_dac_curve_cfg
        if not cfg.audio.enabled or cfg.audio.dac_curve not in ("auto", "calibrated"):
            continue
        authoritative = dac_calibration_store.offline_key_is_authoritative(cfg)
        try:
            label, _ = dac_curve_resolve.resolve_dac_curve_for_backend(cfg)
            if not authoritative and not label.startswith("calibrated:"):
                on_disk = dac_calibration_store.list_calibration_files(cfg.hardware.backend)
                if on_disk:
                    out.append(
                        Diagnostic(
                            level="ok",
                            category="audio",
                            subject=f"{name}/dac_curve",
                            message=(
                                f"{cfg.audio.dac_curve!r} resolves to {label!r} offline "
                                f"(no calibration for this pass's fallback identity key); "
                                f"{len(on_disk)} calibration file(s) on disk for this "
                                "backend, so a live connection may resolve differently."
                            ),
                            hint="Run `--doctor` without `--skip-probe` (or a normal "
                            "run) to check the live device identity.",
                        )
                    )
                    continue
            out.append(
                Diagnostic(
                    level="ok",
                    category="audio",
                    subject=f"{name}/dac_curve",
                    message=f"{cfg.audio.dac_curve!r} resolves to {label!r} on this system.",
                )
            )
        except ValueError as e:
            if not authoritative:
                on_disk = dac_calibration_store.list_calibration_files(cfg.hardware.backend)
                if on_disk:
                    out.append(
                        Diagnostic(
                            level="warn",
                            category="audio",
                            subject=f"{name}/dac_curve",
                            message=(
                                f"no calibration for this pass's offline fallback "
                                f"identity key, but {len(on_disk)} calibration file(s) "
                                "exist on disk for this backend — cannot confirm "
                                "offline whether one applies to this device."
                            ),
                            hint="Run `--doctor` without `--skip-probe` (or a normal "
                            "run) to check the live device identity.",
                        )
                    )
                    continue
            out.append(
                Diagnostic(
                    level="error",
                    category="audio",
                    subject=f"{name}/dac_curve",
                    message=str(e),
                    hint="Run `c64cast -u <target> --calibrate-dac`, or set dac_curve = 'auto'.",
                )
            )
    return out


def _validate_dac_bitmap_tempo(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an out-of-range [audio].dac_bitmap_tempo_* fraction per system.
    Offline — delegates to config.validate_dac_bitmap_tempo_cfg."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_dac_bitmap_tempo_cfg(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="audio",
                    subject=f"{name}/dac_bitmap_tempo",
                    message=str(e),
                    hint="Measure with scripts/diags/mhires_tempo_clock_ab.py, or set to 1.0 (off).",
                )
            )
    return out


def _validate_sid_model(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [ultimate64].sid_model value per system. Offline —
    delegates to config.validate_sid_model_cfg."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_sid_model_cfg(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="audio",
                    subject=f"{name}/sid_model",
                    message=str(e),
                    hint="See [ultimate64].sid_model in the config reference / "
                    "--describe section:ultimate64.",
                )
            )
    return out


_DITHER_HINT = "See [color].dither in the config reference / --describe section:color."


def _validate_dither(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [color].dither name / out-of-range dither_strength on
    [color] and on every scene's own [scenes.color] override, and report how
    "auto" resolves per scene (see config.resolve_dither_method).

    Each scene is checked independently (rather than one whole-config
    validate_dither_cfg call) so a bad override on one scene reports an error
    for that scene alone, instead of also swallowing the resolution report for
    every other scene in the same system. Offline — delegates the actual
    check to config.dither_cfg_error."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        err = dither_cfg_error("[color]", cfg.color)
        if err:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/dither",
                    message=err,
                    hint=_DITHER_HINT,
                )
            )
        for i, s in enumerate(cfg.scenes):
            if s.type not in ("webcam", "video", "slideshow", "generative"):
                continue
            subject = f"{name}/{s.name or s.type}/dither"
            try:
                color = scene_color(cfg, s)
            except ValueError as e:
                out.append(
                    Diagnostic(
                        level="error",
                        category="color",
                        subject=subject,
                        message=str(e),
                        hint=_DITHER_HINT,
                    )
                )
                continue
            if s.color:
                scene_err = dither_cfg_error(f"[[scenes]][{i}].color", color)
                if scene_err:
                    out.append(
                        Diagnostic(
                            level="error",
                            category="color",
                            subject=subject,
                            message=scene_err,
                            hint=_DITHER_HINT,
                        )
                    )
                    continue
            if color.dither != "auto":
                continue
            resolved = resolve_dither_method(color.dither, s.type)
            override_note = " (per-scene [scenes.color] override)" if "dither" in s.color else ""
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=subject,
                    message=(
                        f"'auto' resolves to {resolved!r} for this {s.type} scene "
                        f"(strength {color.dither_strength}){override_note}."
                    ),
                )
            )
    return out


_COLOR_MATCH_HINT = "See [color].color_match in the config reference / --describe section:color."


def _validate_color_match(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [color].color_match value on [color] and on every
    scene's own [scenes.color] override, and report how "auto" resolves per
    scene's display mode (see config.resolve_color_match).

    Each scene is checked independently — see `_validate_dither` for why.
    Offline — delegates the actual check to config.color_match_cfg_error."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        err = color_match_cfg_error("[color]", cfg.color)
        if err:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/color_match",
                    message=err,
                    hint=_COLOR_MATCH_HINT,
                )
            )
        for i, s in enumerate(cfg.scenes):
            display = resolve_scene_display(s.display, s.type)
            if display in ("blank", "hires_edges"):
                continue  # these pick no colors — color_match is a no-op
            subject = f"{name}/{s.name or s.type}/color_match"
            try:
                color = scene_color(cfg, s)
            except ValueError as e:
                out.append(
                    Diagnostic(
                        level="error",
                        category="color",
                        subject=subject,
                        message=str(e),
                        hint=_COLOR_MATCH_HINT,
                    )
                )
                continue
            if s.color:
                scene_err = color_match_cfg_error(f"[[scenes]][{i}].color", color)
                if scene_err:
                    out.append(
                        Diagnostic(
                            level="error",
                            category="color",
                            subject=subject,
                            message=scene_err,
                            hint=_COLOR_MATCH_HINT,
                        )
                    )
                    continue
            if color.color_match != "auto":
                continue
            resolved = "perceptual" if resolve_color_match(color.color_match, display) else "rgb"
            override_note = (
                " (per-scene [scenes.color] override)" if "color_match" in s.color else ""
            )
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=subject,
                    message=f"'auto' resolves to {resolved!r} for this {display} scene"
                    f"{override_note}.",
                )
            )
    return out


_CELL_STRATEGY_HINT = (
    "See [color].cell_strategy in the config reference / --describe section:color."
)


def _validate_cell_strategy(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [color].cell_strategy value on [color] and on every
    scene's own [scenes.color] override, and report how "auto" resolves per
    scene (see config.resolve_cell_strategy). The knob only affects mhires
    with palette_mode=percell, so the resolution report is scoped to those
    scenes.

    Each scene is checked independently — see `_validate_dither` for why.
    Offline — delegates the actual check to config.cell_strategy_cfg_error."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        err = cell_strategy_cfg_error("[color]", cfg.color)
        if err:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/cell_strategy",
                    message=err,
                    hint=_CELL_STRATEGY_HINT,
                )
            )
        for i, s in enumerate(cfg.scenes):
            display = resolve_scene_display(s.display, s.type)
            if display != "mhires" or s.palette_mode != "percell":
                continue  # cell_strategy only affects mhires percell
            subject = f"{name}/{s.name or s.type}/cell_strategy"
            try:
                color = scene_color(cfg, s)
            except ValueError as e:
                out.append(
                    Diagnostic(
                        level="error",
                        category="color",
                        subject=subject,
                        message=str(e),
                        hint=_CELL_STRATEGY_HINT,
                    )
                )
                continue
            if s.color:
                scene_err = cell_strategy_cfg_error(f"[[scenes]][{i}].color", color)
                if scene_err:
                    out.append(
                        Diagnostic(
                            level="error",
                            category="color",
                            subject=subject,
                            message=scene_err,
                            hint=_CELL_STRATEGY_HINT,
                        )
                    )
                    continue
            if color.cell_strategy != "auto":
                continue
            resolved = resolve_cell_strategy(color.cell_strategy, s.type)
            override_note = (
                " (per-scene [scenes.color] override)" if "cell_strategy" in s.color else ""
            )
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=subject,
                    message=f"'auto' resolves to {resolved!r} for this {s.type} scene"
                    f"{override_note}.",
                )
            )
    return out


_MOTION_SMOOTHING_HINT = (
    "See [color].motion_smoothing in the config reference / --describe section:color."
)


def _validate_motion_smoothing(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an out-of-range [color].motion_smoothing on [color] and on every
    scene's own [scenes.color] override, and note it on the mhires percell
    scenes it affects.

    Each scene is checked independently — see `_validate_dither` for why.
    Offline — delegates the actual check to config.motion_smoothing_cfg_error."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        err = motion_smoothing_cfg_error("[color]", cfg.color)
        if err:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/motion_smoothing",
                    message=err,
                    hint=_MOTION_SMOOTHING_HINT,
                )
            )
        for i, s in enumerate(cfg.scenes):
            display = resolve_scene_display(s.display, s.type)
            if display != "mhires" or s.palette_mode != "percell":
                continue  # motion_smoothing only affects mhires percell
            subject = f"{name}/{s.name or s.type}/motion_smoothing"
            try:
                color = scene_color(cfg, s)
            except ValueError as e:
                out.append(
                    Diagnostic(
                        level="error",
                        category="color",
                        subject=subject,
                        message=str(e),
                        hint=_MOTION_SMOOTHING_HINT,
                    )
                )
                continue
            if s.color:
                scene_err = motion_smoothing_cfg_error(f"[[scenes]][{i}].color", color)
                if scene_err:
                    out.append(
                        Diagnostic(
                            level="error",
                            category="color",
                            subject=subject,
                            message=scene_err,
                            hint=_MOTION_SMOOTHING_HINT,
                        )
                    )
                    continue
            if color.motion_smoothing == ColorCfg().motion_smoothing:
                continue  # shipped default — nothing noteworthy
            override_note = (
                " (per-scene [scenes.color] override)" if "motion_smoothing" in s.color else ""
            )
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=subject,
                    message=(
                        f"{color.motion_smoothing} (higher = less flicker / more "
                        "after-image, lower = crisper motion) for this mhires percell "
                        f"scene{override_note}."
                    ),
                )
            )
    return out


def _validate_control(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unauthenticated [control] plane bound to a network address.
    Process-wide (like [midi_control]), so this validates loaded.master_control
    once rather than looping per system. Offline — delegates to
    scene_factory.validate_control_cfg."""
    try:
        validate_control_cfg(loaded.master_control)
    except ConfigError as e:
        return [
            Diagnostic(
                level="error",
                category="control",
                subject="control",
                message=str(e),
                hint="See [control] in the config reference / --describe section:control.",
            )
        ]
    return []


def _validate_midi_control(loaded: LoadResult) -> list[Diagnostic]:
    """Flag a malformed [midi_control] section. Process-wide (like
    [control]), so this validates loaded.master_midi_control once rather
    than looping per system. Offline — delegates to
    config.validate_midi_control_cfg."""
    try:
        validate_midi_control_cfg(loaded.master_midi_control)
    except ConfigError as e:
        return [
            Diagnostic(
                level="error",
                category="midi_control",
                subject="midi_control",
                message=str(e),
                hint="See [midi_control] in the config reference / --describe section:midi_control.",
            )
        ]
    if loaded.master_midi_control.enabled:
        return [
            Diagnostic(
                level="ok",
                category="midi_control",
                subject="midi_control",
                message=f"{len(loaded.master_midi_control.cc_map)} cc_map entries configured.",
            )
        ]
    return []


def _validate_wled(loaded: LoadResult) -> list[Diagnostic]:
    """Flag a malformed [wled] section and report each resolved endpoint when
    enabled: the Mode 3 broadcast target (audio-sync out) and the Mode 1 listen
    bind (virtual WLED device / control surface in). Per-system, offline —
    delegates bounds/warnings to config.validate_wled_cfg."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_wled_cfg(cfg)
            broadcast_on, b_host, b_port = resolve_wled_broadcast(cfg)
            listen_on, l_host, l_port = resolve_wled_listen(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="wled",
                    subject=f"{name}/wled",
                    message=str(e),
                    hint="See [wled] in the config reference / --describe section:wled.",
                )
            )
            continue
        if broadcast_on:
            kind = "multicast" if b_host == "239.0.0.1" else "unicast"
            target = f"{kind} {b_host}:{b_port}"
            has_sid = any(
                s.type == "waveform" or (s.type == "generative" and s.audio_source == "sid")
                for s in cfg.scenes
            )
            out.append(
                Diagnostic(
                    level="ok" if has_sid else "warn",
                    category="wled",
                    subject=f"{name}/wled broadcast",
                    message=(
                        f"broadcasting Audio Sync to {target} at {cfg.wled.rate_hz:.0f} Hz"
                        if has_sid
                        else f"enabled ({target}) but no SID-driven scene to broadcast — "
                        "nothing will be sent"
                    ),
                )
            )
        if listen_on:
            out.append(
                Diagnostic(
                    level="ok",
                    category="wled",
                    subject=f"{name}/wled listen",
                    message=(
                        f"virtual WLED device '{cfg.wled.name}' serving the WLED JSON "
                        f"API on {l_host}:{l_port} (needs the 'wled' extra)"
                    ),
                )
            )
    return out


def _validate_cross_system_orchestration(loaded: LoadResult) -> list[Diagnostic]:
    """Each `orchestrate=true` scene must have a same-name follower in
    every other system. If not, the Playlist falls back to building the
    follower from the conductor's cfg — usually surprising."""
    out: list[Diagnostic] = []
    # name -> set of system names that have a scene with that name
    coverage: dict[str, set[str]] = {}
    for sys_name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        for s in cfg.scenes:
            if s.name:
                coverage.setdefault(s.name, set()).add(sys_name)

    all_systems = set(loaded.names)
    for sys_name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        for s in cfg.scenes:
            if not s.orchestrate or not s.name:
                continue
            present = coverage.get(s.name, set())
            missing = all_systems - present
            if missing:
                out.append(
                    Diagnostic(
                        level="warn",
                        category="orchestrator",
                        subject=f"{sys_name}/{s.name}",
                        message=(
                            f"conductor scene has no same-name follower in: "
                            f"{', '.join(sorted(missing))}. Followers will be "
                            "built from the conductor's cfg instead."
                        ),
                        hint=(
                            f'Add a `[[scenes]]` with `name = "{s.name}"` to '
                            "each missing system's TOML to control its appearance."
                        ),
                    )
                )
    return out


def _validate_ensemble_recording_paths(loaded: LoadResult) -> list[Diagnostic]:
    """Two recording systems must not resolve to the same output file.

    `resolve_recording_path` only disambiguates systems that left `path` at
    the default, so spelling out one shared path across two per-system TOMLs
    still points two cv2.VideoWriters at one file — which produces a single
    truncated stream, with no error from either writer.

    Two names for one file have to compare equal or the check misses exactly
    the case it exists for, so paths are normalized the way the filesystem
    would read them: expanded, made absolute against the cwd the run will use,
    and `normcase`'d (identity on POSIX; on Windows it also folds the
    separators, which matters because `expanduser` leaves `~/x` as
    `C:\\Users\\me/x`)."""
    # display path per normalized key, plus the systems that resolved to it
    by_path: dict[str, tuple[str, list[str]]] = {}
    for sys_name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        if not cfg.recording.enabled:
            continue
        resolved = os.path.abspath(
            expand_user(resolve_recording_path(cfg.recording, sys_name, is_ensemble=True))
        )
        _, names = by_path.setdefault(os.path.normcase(resolved), (resolved, []))
        names.append(sys_name)

    return [
        Diagnostic(
            level="error",
            category="recording",
            subject=display,
            message=(
                f"{len(names)} systems record to this one file "
                f"({', '.join(sorted(names))}); only one stream survives."
            ),
            hint=(
                "Give each system's [recording] its own `path`, or delete the "
                "key so each derives one from its system name."
            ),
        )
        for _, (display, names) in sorted(by_path.items())
        if len(names) > 1
    ]


def _probe_extras() -> list[Diagnostic]:
    out: list[Diagnostic] = []
    # An installed user has no project to `uv sync`; they re-run the tool
    # install (same reasoning as the checkout-gated .venv / uv.lock probes
    # above). `[all]` rather than the one missing extra because extras do not
    # accumulate — installing `c64cast[midi]` over `c64cast[video]` would trade
    # one missing feature for another.
    hint = (
        "uv sync --all-extras"
        if _running_from_checkout()
        else 'uv tool install --force "c64cast[all]"'
    )
    for extra, module, used_for in _EXTRAS:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            out.append(
                Diagnostic(
                    level="warn",
                    category="extras",
                    subject=extra,
                    message=f"not installed (used for: {used_for})",
                    hint=hint,
                )
            )
        else:
            out.append(
                Diagnostic(
                    level="ok", category="extras", subject=extra, message=f"installed ({module})"
                )
            )
    return out


# --doctor connectivity hints, hoisted so the probe code reads as logic and a
# wording tweak is one edit (the same prose used to be inlined per Diagnostic).
_HINT_DMA_SERVICE = (
    "Enable F2 -> Network Settings -> Ultimate DMA Service. "
    "If a password is set, supply it via "
    "C64CAST_DMA_PASSWORD or [ultimate64].dma_password."
)
_HINT_TR_CONNECT = (
    "Check the USB data cable to the TR's micro-USB-B port "
    "(transport = serial) or 'Enable TCP Listener' + the "
    "host IP (transport = tcp)."
)
_HINT_REST_RUNNER = (
    "The SID player and .prg/.crt launcher use the "
    "Ultimate's REST run_prg endpoint. Enable the "
    "Ultimate's web/remote-control service (F2 -> "
    "Network Settings), or use only DMA-rendered scenes "
    "(video/slideshow/webcam/blank)."
)
_HINT_REST_OPTIONAL = (
    "REST powers reads (keyboard control), machine "
    "reset, and program launch; writes still work via "
    "DMA so DMA-rendered scenes play. Enable the "
    "Ultimate's web/remote-control service (F2 -> "
    "Network Settings) if you need those."
)


def _probe_connectivity(loaded: LoadResult) -> list[Diagnostic]:
    """Try `Ultimate64API(...)` once per system. Catches SocketDMAError
    so doctor mode completes even when no U64 is powered on. Also probes
    REU enable status when the per-system config opts into a REU-staged
    path (mic, video audio, or char-mode video) — those silently
    produce silent audio / garbled video when REU is disabled at the U64.
    """
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        out.extend(_probe_one_system(name, cfg))
    return out


def _probe_one_system(name: str, cfg: Config) -> list[Diagnostic]:
    """Connect one system's backend, probe it, and run the per-service
    probes that apply. Connection failures come back as diagnostics, not
    exceptions, so one dead system doesn't hide the others' reports."""
    from c64cast.hw.backend import make_backend
    from c64cast.hw.socket_dma import SocketDMAError
    from c64cast.hw.teensyrom_dma import TRError

    url = cfg.ultimate64.url
    try:
        api = make_backend(cfg)
    except SocketDMAError as e:
        return [
            Diagnostic(
                level="error",
                category="connectivity",
                subject=name,
                message=f"DMA connect to {url} failed: {e}",
                hint=_HINT_DMA_SERVICE,
            )
        ]
    except TRError as e:
        return [
            Diagnostic(
                level="error",
                category="connectivity",
                subject=name,
                message=f"TeensyROM connect failed: {e}",
                hint=_HINT_TR_CONNECT,
            )
        ]
    try:
        status = api.probe()
        if cfg.hardware.backend == "teensyrom":
            return _probe_tr_reachability(name, cfg, api, status)
        if status is None:
            return [_rest_down_diagnostic(name, cfg, url)]
        # REST just answered the probe; refine the optimistic capability
        # flags so the per-service probes below judge the device's actual
        # config surface (U2+: no multi-SID categories), like a real run.
        api.refine_capabilities()
        return _probe_u64_services(name, cfg, api, url, status)
    finally:
        api.close()


def _probe_tr_reachability(
    name: str, cfg: Config, api: object, status: str | None
) -> list[Diagnostic]:
    """The TeensyROM has no REST surface; probe() is the ping/FW line. It
    also has no REU, so the REST REU/SID-enable probes don't apply — instead
    just flag a REU opt-in as ignored."""
    if status is None:
        return [
            Diagnostic(
                level="warn",
                category="connectivity",
                subject=name,
                message="TeensyROM transport reachable but ping failed",
                hint="Writes may still work; check the firmware version.",
            )
        ]
    out = [
        Diagnostic(
            level="ok",
            category="connectivity",
            subject=name,
            message=f"TeensyROM reachable ({status})",
        )
    ]
    out.extend(_probe_reu_unavailable(name, cfg, api))
    return out


def _rest_down_diagnostic(name: str, cfg: Config, url: str) -> Diagnostic:
    """DMA answered but REST didn't. SID playback + .prg/.crt launch start via
    the REST run_prg endpoint, so a config that needs the runner turns this
    into an error (those scenes cannot run at all); otherwise it's a warning
    (writes still work via DMA, so DMA-rendered scenes play)."""
    wants_runner, runner_reasons = _wants_rest_runner(cfg)
    if wants_runner:
        return Diagnostic(
            level="error",
            category="connectivity",
            subject=name,
            message=(
                f"DMA reachable at {url} but REST probe failed "
                f"— scenes that launch a program cannot start "
                f"({', '.join(runner_reasons)})"
            ),
            hint=_HINT_REST_RUNNER,
        )
    return Diagnostic(
        level="warn",
        category="connectivity",
        subject=name,
        message=f"DMA reachable at {url} but REST probe failed",
        hint=_HINT_REST_OPTIONAL,
    )


def _probe_u64_services(
    name: str, cfg: Config, api: object, url: str, status: str
) -> list[Diagnostic]:
    """Both links up: report OK, then run the live per-service probes. REU
    status when the config opts into a REU-staged path; SID enable state when
    the config drives the SID (catches the U2+ "emulated SID disabled" case
    where every tune is silent while video + the oscilloscope still work);
    the Ultimate Audio sampler when video audio will use it; DAC calibration
    live (the offline _validate_dac_curve check can't know which physical SID
    socket is mapped to $D400 — this one is precise); and SID model
    autoconfig live (offline validation can only check the config names a
    known value; this reports what's actually socketed right now)."""
    out = [
        Diagnostic(
            level="ok",
            category="connectivity",
            subject=name,
            message=f"DMA + REST reachable at {url} ({status})",
        )
    ]
    out.extend(_probe_system_mode(name, cfg, api))
    out.extend(_probe_reu_status(name, cfg, api))
    out.extend(_probe_sid_status(name, cfg, api))
    out.extend(_probe_sampler_status(name, cfg, api))
    out.extend(_probe_dac_calibration_status(name, cfg, api))
    out.extend(_probe_sid_autoconfig_status(name, cfg, api))
    return out


def _probe_reu_unavailable(name: str, cfg: Config, api: object) -> list[Diagnostic]:
    """On a backend with no REU (e.g. TeensyROM), report that a config's
    REU-staged opt-in is ignored. session.build_stack coerces these off to the
    host-DMA paths, so this is informational, not a failure."""
    wants, reasons = hw_provision.wants_reu(cfg)
    if not wants or getattr(api, "profile", None) is None or api.profile.supports_reu:  # type: ignore[attr-defined]
        return []
    return [
        Diagnostic(
            level="warn",
            category="connectivity",
            subject=f"{name} (REU)",
            message=f"config requests REU ({', '.join(reasons)}) but this backend has no REU",
            hint="The opt-in is ignored — the host-DMA NMI DAC / host-DMA "
            "video paths are used instead. Remove the flag to silence this.",
        )
    ]


# The emulated-SID enable state. The category is registered by U2/U2+/U2+L
# firmware only (the U64's internal SID lives elsewhere and is normally on) —
# the probe below already stays quiet when the fields are absent, which is
# exactly what a U64 answers. Canonical names live in
# c64cast/sid/emusid_mixer.py, the module that drives this surface.
_AUDIO_CONFIG_CATEGORY = emusid_mixer.CAT_EMUSID
_SID_LEFT_FIELD = emusid_mixer.ITEM_ENABLE["emusid1"]
_SID_RIGHT_FIELD = emusid_mixer.ITEM_ENABLE["emusid2"]


def _wants_sid_audio(cfg: Config) -> tuple[bool, list[str]]:
    """Return (wants_sid, reasons). Any of these means c64cast will try to
    produce sound through the C64 SID ($D4xx): global audio streaming (the
    4-bit DAC / video audio), or any waveform/midi scene (which DMA a
    SID player and drive the chip even when [audio].enabled is false)."""
    reasons: list[str] = []
    if cfg.audio.enabled:
        reasons.append("[audio].enabled = true")
    types = {s.type for s in cfg.scenes}
    if "waveform" in types:
        reasons.append("waveform (SID oscilloscope) scene(s)")
    if "midi" in types:
        reasons.append("midi scene(s)")
    return bool(reasons), reasons


def _wants_rest_runner(cfg: Config) -> tuple[bool, list[str]]:
    """Return (wants, reasons). True when the config has a scene that STARTS
    via the Ultimate's REST `run_prg`/`run_crt` endpoint — SID playback
    (`run_sid_player`) or a native .prg/.crt launcher (`launch_program`).
    Those scenes cannot start at all when REST is down, so on the Ultimate a
    failed REST probe with any of them present is an error, not a warning.

    Video / slideshow / webcam / blank / midi / generative-without-SID scenes
    paint entirely over DMA (writes) and keep working without REST, so they do
    NOT escalate the probe failure. (`reset()` is also REST-only on the
    Ultimate, but it is caught + non-fatal — the picture still paints — so it
    is not on its own grounds for an error.) TR is handled on its own branch;
    its SID player + launcher use pure-DMA vector-swap / LaunchFile, not REST.
    """
    reasons: list[str] = []
    types = {s.type for s in cfg.scenes}
    if "waveform" in types:
        reasons.append("waveform (SID player via run_prg) scene(s)")
    if "launcher" in types:
        reasons.append("launcher (.prg/.crt via run_prg) scene(s)")
    # A generative SourceScene with audio_source = "sid" kicks run_sid_player
    # the same way a waveform scene does (see scenes.py SourceScene.setup).
    if any(s.type == "generative" and s.audio_source == "sid" for s in cfg.scenes):
        reasons.append("generative scene with audio_source = 'sid' (run_prg)")
    return bool(reasons), reasons


def _probe_sid_status(name: str, cfg: Config, api: object) -> list[Diagnostic]:
    """If the config will drive the SID, check the Ultimate's emulated-SID
    enable state via REST. On a U64 the internal SID is normally on; on a
    U2+ the emulated SID that snoops $D400 ships *disabled*, which makes
    every tune silent — and because video (DMA) and the host-emulated
    oscilloscope both keep working, the failure is easy to misread as a
    c64cast bug. Returns an empty list when no SID audio is requested.
    Emits:
      * ok   — at least one SID (Left/Right) enabled
      * warn — both disabled while the config drives the SID
      * warn — REST query failed / unexpected shape
    A warn (not error) because a physical SID chip can still produce sound
    with the emulated SIDs off.
    """
    wants, reasons = _wants_sid_audio(cfg)
    if not wants:
        return []

    subject = f"{name} (SID)"
    reason_str = ", ".join(reasons)

    section, _data, err = hw_provision.fetch_config_section(
        api, _AUDIO_CONFIG_CATEGORY, field_hint=_SID_LEFT_FIELD
    )
    if err is not None:
        return [
            Diagnostic(
                level="warn",
                category="connectivity",
                subject=subject,
                message=f"REST query for SID status failed: {err}",
                hint=(
                    f"Cannot confirm the SID is enabled. Config drives the SID "
                    f"({reason_str}). If audio is silent, check F2 -> "
                    "Audio Output Settings -> SID Left / SID Right."
                ),
            )
        ]

    left = section.get(_SID_LEFT_FIELD)
    right = section.get(_SID_RIGHT_FIELD)
    # Neither field present → a firmware/variant we don't recognize. Stay
    # quiet rather than emit a misleading warning.
    if left is None and right is None:
        return []

    if left == "Enabled" or right == "Enabled":
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message=f"SID enabled (Left={left}, Right={right}) ({reason_str})",
            )
        ]

    return [
        Diagnostic(
            level="warn",
            category="connectivity",
            subject=subject,
            message=(
                f"both SIDs disabled (Left={left!r}, Right={right!r}) but "
                f"config drives the SID ({reason_str}). The Ultimate's "
                "emulated SID won't sound $D400 writes — every tune is "
                "silent unless a physical SID chip is producing the audio."
            ),
            hint=(
                "On the Ultimate: F2 Menu -> Audio Output Settings -> "
                "SID Left -> Enabled (keep 'SID Left Base = Snoop $D400', "
                "Vol EmuSid1 above OFF). A U64's internal SID is on by default; "
                "a U2+ ships its emulated SID disabled. A working physical SID "
                "chip can sound without this."
            ),
        )
    ]


def _probe_system_mode(name: str, cfg: Config, api: object) -> list[Diagnostic]:
    """Compare `[ultimate64].system` against the machine's live System Mode.

    This one field sets the CPU clock, the frame rate, the DAC NMI latches and
    the SID PLAY rate all at once, and a wrong value is silent — everything
    just runs at the other standard's numbers. So report it wherever the
    machine can answer:
      * ok    — "auto" (it will be read at run time), or an explicit value that
                matches
      * error — an explicit value that disagrees with the machine
      * (quiet) — a backend with no System Mode surface, or an unreadable one
    """
    subject = f"{name} (system)"
    live = hw_provision.read_system_timing(api)
    if live is None:
        return []
    configured = cfg.ultimate64.system
    if configured == "auto":
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message=f"system = auto resolves to {live} on this machine",
            )
        ]
    if configured.upper() == live:
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message=f"system = {configured} matches the machine",
            )
        ]
    return [
        Diagnostic(
            level="error",
            category="connectivity",
            subject=subject,
            message=(
                f"[ultimate64].system = {configured} but the machine is running {live} timing"
            ),
            hint="Frame rate, CPU clock, DAC NMI rate and SID PLAY rate are all "
            'computed from this. Set system = "auto" to read it from the '
            "machine, or change one of the two to agree.",
        )
    ]


def _probe_reu_status(name: str, cfg: Config, api: object) -> list[Diagnostic]:
    """If the config wants REU, check the U64's REU setting via REST.
    Returns an empty list when REU isn't requested. Emits:
      * ok    — REU enabled, with the configured size
      * error — REU disabled (the staged-path opt-ins won't work)
      * warn  — REST query failed; can't tell either way
    """
    wants, reasons = hw_provision.wants_reu(cfg)
    if not wants:
        return []

    subject = f"{name} (REU)"
    reason_str = ", ".join(reasons)

    section, data, err = hw_provision.fetch_config_section(
        api, hw_provision.REU_CONFIG_CATEGORY, field_hint=hw_provision.REU_ENABLED_FIELD
    )
    if err is not None:
        return [
            Diagnostic(
                level="warn",
                category="connectivity",
                subject=subject,
                message=f"REST query for REU status failed: {err}",
                hint=(
                    f"Cannot confirm REU is enabled. Config requests REU "
                    f"({reason_str}). If audio is silent / video is garbled, "
                    "check F2 -> C64 and Cartridge Settings -> "
                    "RAM Expansion Unit on the U64."
                ),
            )
        ]
    if not section:
        return [
            Diagnostic(
                level="warn",
                category="connectivity",
                subject=subject,
                message=f"REU config endpoint returned unexpected shape: {type(data).__name__}",
                hint="Likely a U64 firmware mismatch — c64cast expects "
                "Ultimate firmware 3.x+. Check the firmware version.",
            )
        ]

    enabled = section.get(hw_provision.REU_ENABLED_FIELD)
    size = section.get(hw_provision.REU_SIZE_FIELD, "?")
    if enabled == "Enabled":
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message=f"REU enabled, size {size} ({reason_str})",
            )
        ]
    # REU is off. When [ultimate64].auto_reu is on (the default), the run
    # provisions it live at startup (provision_reu) — so this isn't an error,
    # just an informational "will be auto-enabled". It's a hard error only when
    # the user has opted out of auto-provisioning. (We reach here only on a
    # REST-reachable Ultimate, so supports_reu is implied.)
    auto_reu = cfg.ultimate64.auto_reu
    if auto_reu:
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message=(
                    f"REU is {enabled!r}, but [ultimate64].auto_reu will enable "
                    f"it (size {hw_provision.REU_PROVISION_SIZE}) live for this run ({reason_str})."
                ),
                hint=(
                    "Auto-provision is volatile (reverts on power-cycle) and "
                    "restored at teardown. Set [ultimate64].auto_reu = false to "
                    "manage the REU yourself in the F2 menu."
                ),
            )
        ]
    return [
        Diagnostic(
            level="error",
            category="connectivity",
            subject=subject,
            message=(
                f"REU is {enabled!r} but config requests REU ({reason_str}) and "
                "[ultimate64].auto_reu is off. REU-staged audio/video paths fail "
                "silently when REU is off: audio plays silence, video stays "
                "unchanged."
            ),
            hint=(
                "Set [ultimate64].auto_reu = true to enable it automatically, or "
                "on the U64: F2 Menu -> C64 and Cartridge Settings -> "
                "RAM Expansion Unit -> Enabled (size 16 MB). Save and reboot. "
                "Alternatively, turn off the REU opt-in in your TOML."
            ),
        )
    ]


def _probe_sampler_status(name: str, cfg: Config, api: object) -> list[Diagnostic]:
    """If the config will use the Ultimate Audio sampler for video audio, check
    the U64's sampler state via REST. Returns an empty list when not wanted.
    Emits:
      * ok    — sampler mapped + audible (high-fidelity path ready), OR mapped
                off / muted but the run will auto-enable it live, OR backend is
                'auto' on hardware without the feature (falls back to the DAC)
      * warn  — REST query failed, or an explicit 'sampler' on a no-sampler backend
      * error — explicit 'sampler' but the U64 firmware lacks the feature
    """
    wants, reasons = hw_provision.wants_sampler(cfg)
    if not wants:
        return []

    subject = f"{name} (Ultimate Audio sampler)"
    reason_str = ", ".join(reasons)
    backend = cfg.audio.backend
    supports = bool(getattr(getattr(api, "profile", None), "supports_sampler", False))

    if not supports:
        # A non-sampler backend (TeensyROM): 'auto' silently uses the DAC; an
        # explicit 'sampler' can't be honored.
        if backend == "sampler":
            return [
                Diagnostic(
                    level="warn",
                    category="connectivity",
                    subject=subject,
                    message="[audio].backend = 'sampler' but this backend has no "
                    "FPGA sampler — video audio uses the 4-bit DAC.",
                    hint="Set [audio].backend = 'dac' or 'auto' for this backend.",
                )
            ]
        return []

    state = hw_provision.read_sampler_config(api)
    if state.present is None:
        return [
            Diagnostic(
                level="warn",
                category="connectivity",
                subject=subject,
                message="REST query for the Ultimate Audio sampler state failed.",
                hint=f"Config will use the sampler ({reason_str}). If video audio is "
                "silent, check F2 -> C64 and Cartridge Settings -> Map Ultimate "
                "Audio $DF20-DFFF, and Vol Sampler L/R under F2 -> Audio Mixer "
                "(U64) / Audio Output Settings (U2+).",
            )
        ]
    if not state.present:
        if backend == "sampler":
            return [
                Diagnostic(
                    level="error",
                    category="connectivity",
                    subject=subject,
                    message="[audio].backend = 'sampler' but this U64 firmware does "
                    "not expose the Ultimate Audio sampler.",
                    hint="Update the U64 firmware, or set [audio].backend = 'dac' / "
                    "'auto' (auto falls back to the 4-bit DAC).",
                )
            ]
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message="firmware has no Ultimate Audio sampler; [audio].backend = "
                "auto falls back to the 4-bit DAC.",
            )
        ]

    audible = any(v != hw_provision.SAMPLER_VOL_OFF for v in state.volumes.values())
    if state.map_enabled and audible:
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message=f"Ultimate Audio mapped + audible — high-fidelity video "
                f"audio ({reason_str}).",
            )
        ]
    off_bits = []
    if not state.map_enabled:
        off_bits.append("$DF20 I/O map disabled")
    if not audible:
        off_bits.append("Sampler mixer channels OFF")
    return [
        Diagnostic(
            level="ok",
            category="connectivity",
            subject=subject,
            message=f"{' + '.join(off_bits)}; will be enabled live for this run ({reason_str}).",
            hint="Auto-enable is volatile (reverts on power-cycle) and restored at "
            "teardown. Set [audio].backend = 'dac' to use the 4-bit DAC instead.",
        )
    ]


def _wants_dac_calibration_check(cfg: Config) -> bool:
    """The run wants a DAC calibration check when audio is enabled and
    [audio].dac_curve is a system-aware curve ('auto' or 'calibrated')."""
    if not cfg.audio.enabled:
        return False
    return cfg.audio.dac_curve in ("auto", "calibrated")


def _probe_dac_calibration_status(name: str, cfg: Config, api: object) -> list[Diagnostic]:
    """If [audio].dac_curve is 'auto'/'calibrated', report the LIVE-resolved
    calibration: which key/file applies and whether it actually matches
    what's currently mapped to $D400 (a live SID-addressing read — the
    offline _validate_dac_curve check can't do this, so it's only
    approximate). Emits:
      * ok    — resolves to a calibrated table, or 'auto' cleanly falls back
                to the baked/linear default
      * error — [audio].dac_curve = 'calibrated' but no matching table
    """
    if not _wants_dac_calibration_check(cfg):
        return []
    from c64cast.audio import dac_calibration_store, dac_curve_resolve

    subject = f"{name} (DAC calibration)"
    curve = cfg.audio.dac_curve
    try:
        label, table = dac_curve_resolve.resolve_dac_curve_for_backend(
            cfg,
            be=api,  # type: ignore[arg-type]
        )
    except ValueError as e:
        return [
            Diagnostic(
                level="error",
                category="connectivity",
                subject=subject,
                message=str(e),
                hint="Run `c64cast -u <target> --calibrate-dac`, or set "
                "[audio].dac_curve = 'auto'.",
            )
        ]
    key = dac_calibration_store.resolve_calibration_key(cfg, api)  # type: ignore[arg-type]
    if table is not None:
        message = f"[audio].dac_curve = {curve!r} resolves to {label!r} (key {key!r})."
    else:
        message = f"no calibration applies right now (key {key!r}); resolves to {label!r}."
    return [Diagnostic(level="ok", category="connectivity", subject=subject, message=message)]


def _wants_sid_autoconfig_check(cfg: Config) -> bool:
    """The run wants a SID model autoconfig check when [ultimate64].sid_model
    isn't 'off' and a scene will actually drive the SID player — a waveform
    scene, or a generative scene with audio_source = 'sid'
    (SidFileAudioSource; see sid_autoconfig.py's two call sites)."""
    if cfg.ultimate64.sid_model == "off":
        return False
    for s in cfg.scenes:
        if s.type == "waveform":
            return True
        if s.type == "generative" and s.audio_source == "sid":
            return True
    return False


def _probe_sid_autoconfig_status(name: str, cfg: Config, api: object) -> list[Diagnostic]:
    """If [ultimate64].sid_model isn't 'off' and the config drives the SID
    player, report the resolved mode + what's currently socketed. Since
    doctor has no tune loaded, this can only report live socket/model
    detection — not a per-chip plan, which needs a header (see
    sid_autoconfig.apply_sid_autoconfig, run once a tune is actually
    playing). Emits:
      * ok   — mode + detected socket models
      * warn — REST query failed"""
    if not _wants_sid_autoconfig_check(cfg):
        return []
    from c64cast.sid import sid_hw_config

    subject = f"{name} (SID model autoconfig)"
    sid_model = cfg.ultimate64.sid_model
    try:
        socket1, socket2 = sid_hw_config.detect_socket_models(api)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001 — best-effort, matches sid_hw_config's own philosophy
        return [
            Diagnostic(
                level="warn",
                category="connectivity",
                subject=subject,
                message=f"REST query for socket model detection failed: {e}",
                hint=f"[ultimate64].sid_model = {sid_model!r}; cannot confirm what's socketed.",
            )
        ]
    detected = ", ".join(
        f"socket {n}={model or 'none'}" for n, model in ((1, socket1), (2, socket2))
    )
    return [
        Diagnostic(
            level="ok",
            category="connectivity",
            subject=subject,
            message=f"[ultimate64].sid_model = {sid_model!r}; detected {detected}.",
        )
    ]


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

_LEVEL_ORDER = {"error": 0, "warn": 1, "ok": 2}
_LEVEL_GLYPH = {"ok": "[ ok ]", "warn": "[WARN]", "error": "[ERR ]"}


def print_report(diagnostics: list[Diagnostic], file: IO[str] | None = None) -> int:
    """Print a grouped report and return an exit code (0 if no errors,
    1 if any error-level Diagnostic)."""
    out = file if file is not None else sys.stdout

    by_category: dict[str, list[Diagnostic]] = {}
    for d in diagnostics:
        by_category.setdefault(d.category, []).append(d)

    # Stable ordering by category, then error > warn > ok within each.
    category_order = [
        "environment",
        "config",
        "scene",
        "audio",
        "color",
        "recording",
        "control",
        "midi_control",
        "wled",
        "orchestrator",
        "extras",
        "connectivity",
    ]
    # Anything with a category not named above still has to reach the user;
    # dropping it would make a new probe look like it passed.
    category_order += sorted(set(by_category) - set(category_order))
    for cat in category_order:
        rows = by_category.get(cat)
        if not rows:
            continue
        print(f"\n{cat.upper()}", file=out)
        print("-" * len(cat), file=out)
        rows.sort(key=lambda d: (_LEVEL_ORDER[d.level], d.subject))
        for d in rows:
            print(f"{_LEVEL_GLYPH[d.level]} {d.subject}: {d.message}", file=out)
            if d.hint:
                print(f"       hint: {d.hint}", file=out)

    n_err = sum(1 for d in diagnostics if d.level == "error")
    n_warn = sum(1 for d in diagnostics if d.level == "warn")
    n_ok = sum(1 for d in diagnostics if d.level == "ok")
    print(f"\nsummary: {n_ok} ok, {n_warn} warn, {n_err} error", file=out)
    return 1 if n_err else 0
