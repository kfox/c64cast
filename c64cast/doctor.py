"""Configuration + environment diagnostics.

Collects every per-scene/per-overlay/per-orchestrator validation failure
across every system in the loaded config (instead of failing fast on the
first one), probes which optional install extras are importable, and
optionally pings each system's U64 to verify DMA-service reachability.
The `--doctor` CLI flag dispatches here and prints the resulting report.

The validation surface is shared with `config.build_scene` via
`config.validate_scene_cfg` — there is no parallel registry of probes.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from .c64 import max_safe_sample_rate, nmi_rate_safety
from .config import (
    ColorCfg,
    ConfigError,
    LoadResult,
    resolve_cell_strategy,
    resolve_color_match,
    resolve_dither_method,
    resolve_scene_display,
    resolve_wled_broadcast,
    resolve_wled_listen,
    validate_cell_strategy_cfg,
    validate_color_match_cfg,
    validate_dac_bitmap_tempo_cfg,
    validate_dac_curve_cfg,
    validate_dither_cfg,
    validate_midi_control_cfg,
    validate_motion_smoothing_cfg,
    validate_scene_cfg,
    validate_sid_model_cfg,
    validate_wled_cfg,
)
from .orchestrator import OrchestratorError

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
    ("preview", "pygame", "[preview] enabled local window"),
    ("control", "fastapi", "[control] enabled HTTP plane"),
    ("obs", "obsws_python", "obs_status overlay"),
    ("midi", "mido", "midi scenes; [midi_control] live control"),
    ("logging", "rich", "colored log output"),
    ("vision", "mediapipe", "[vision] enabled gesture control"),
    ("tr", "serial", "TeensyROM serial backend"),
    ("wizard", "questionary", "--init config wizard"),
    ("yt", "yt_dlp", "cast URL playback (YouTube et al.)"),
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

# Repo root (parent of the package dir). Used to locate the project .venv and
# run `uv lock --check` from the right directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def validate_load_result(loaded: LoadResult, *, probe_u64: bool = True) -> list[Diagnostic]:
    """Run every config + environment check and collect the results.

    Per-scene validation runs `validate_scene_cfg` inside try/except so a
    single broken scene doesn't hide the others. Cross-system orchestrator
    coverage (a conductor scene must have a same-name follower in every
    other system) is warn-level because the Playlist will fall back to the
    conductor's cfg — but that's rarely what the user actually wants.
    """
    out: list[Diagnostic] = []

    out.extend(_probe_environment())
    out.extend(_validate_scenes(loaded))
    out.extend(_validate_audio_nmi_rate(loaded))
    out.extend(_validate_dac_curve_cfg(loaded))
    out.extend(_validate_dac_bitmap_tempo(loaded))
    out.extend(_validate_sid_model(loaded))
    out.extend(_validate_dither(loaded))
    out.extend(_validate_color_match(loaded))
    out.extend(_validate_cell_strategy(loaded))
    out.extend(_validate_motion_smoothing(loaded))
    out.extend(_validate_midi_control(loaded))
    out.extend(_validate_wled(loaded))
    if loaded.is_ensemble:
        out.extend(_validate_cross_system_orchestration(loaded))
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
    won't import. Reports the interpreter, asserts every hard dep imports, and
    best-effort checks uv.lock vs pyproject.toml. Offline; runs in every doctor
    invocation (including `--skip-probe`)."""
    out: list[Diagnostic] = []

    # Active interpreter vs the project .venv. Only flag a mismatch when a
    # project .venv actually exists — a pip-installed package legitimately runs
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

    out.extend(_probe_uv_lock())
    return out


def _probe_uv_lock() -> list[Diagnostic]:
    """Best-effort `uv lock --check` — warns when uv.lock has drifted from
    pyproject.toml (CI installs `--frozen`, so drift breaks CI). Skips cleanly
    when the uv CLI isn't on PATH."""
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
    would use — see dac_calibration.offline_key_is_authoritative. A miss
    against that fallback key doesn't prove no calibration applies, so when
    calibration files exist on disk for this backend that the fallback key
    can't confirm or rule out, the message/error is hedged rather than
    asserting a possibly-wrong resolution."""
    from . import dac_calibration

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
        authoritative = dac_calibration.offline_key_is_authoritative(cfg)
        try:
            label, _ = dac_calibration.resolve_dac_curve_for_backend(cfg)
            if not authoritative and not label.startswith("calibrated:"):
                on_disk = dac_calibration.list_calibration_files(cfg.hardware.backend)
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
                on_disk = dac_calibration.list_calibration_files(cfg.hardware.backend)
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


def _validate_dither(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [color].dither name / out-of-range dither_strength, and
    report how "auto" resolves per scene (see config.resolve_dither_method).
    Offline — delegates to config.validate_dither_cfg."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_dither_cfg(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/dither",
                    message=str(e),
                    hint="See [color].dither in the config reference / --describe section:color.",
                )
            )
            continue
        if cfg.color.dither != "auto":
            continue
        for s in cfg.scenes:
            if s.type not in ("webcam", "video", "slideshow", "generative"):
                continue
            resolved = resolve_dither_method(cfg.color.dither, s.type)
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=f"{name}/{s.name or s.type}/dither",
                    message=(
                        f"'auto' resolves to {resolved!r} for this {s.type} scene "
                        f"(strength {cfg.color.dither_strength})."
                    ),
                )
            )
    return out


def _validate_color_match(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [color].color_match value, and report how "auto" resolves
    per scene's display mode (see config.resolve_color_match). Offline —
    delegates to config.validate_color_match_cfg."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_color_match_cfg(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/color_match",
                    message=str(e),
                    hint="See [color].color_match in the config reference / "
                    "--describe section:color.",
                )
            )
            continue
        if cfg.color.color_match != "auto":
            continue
        for s in cfg.scenes:
            display = resolve_scene_display(s.display, s.type)
            if display in ("blank", "hires_edges"):
                continue  # these pick no colors — color_match is a no-op
            resolved = (
                "perceptual" if resolve_color_match(cfg.color.color_match, display) else "rgb"
            )
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=f"{name}/{s.name or s.type}/color_match",
                    message=f"'auto' resolves to {resolved!r} for this {display} scene.",
                )
            )
    return out


def _validate_cell_strategy(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an unknown [color].cell_strategy value, and report how "auto"
    resolves per scene (see config.resolve_cell_strategy). The knob only affects
    mhires with palette_mode=percell, so the resolution report is scoped to
    those scenes. Offline — delegates to config.validate_cell_strategy_cfg."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_cell_strategy_cfg(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/cell_strategy",
                    message=str(e),
                    hint="See [color].cell_strategy in the config reference / "
                    "--describe section:color.",
                )
            )
            continue
        if cfg.color.cell_strategy != "auto":
            continue
        for s in cfg.scenes:
            display = resolve_scene_display(s.display, s.type)
            if display != "mhires" or s.palette_mode != "percell":
                continue  # cell_strategy only affects mhires percell
            resolved = resolve_cell_strategy(cfg.color.cell_strategy, s.type)
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=f"{name}/{s.name or s.type}/cell_strategy",
                    message=f"'auto' resolves to {resolved!r} for this {s.type} scene.",
                )
            )
    return out


def _validate_motion_smoothing(loaded: LoadResult) -> list[Diagnostic]:
    """Flag an out-of-range [color].motion_smoothing, and note it on the mhires
    percell scenes it affects. Offline — delegates to
    config.validate_motion_smoothing_cfg."""
    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        try:
            validate_motion_smoothing_cfg(cfg)
        except ConfigError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="color",
                    subject=f"{name}/motion_smoothing",
                    message=str(e),
                    hint="See [color].motion_smoothing in the config reference / "
                    "--describe section:color.",
                )
            )
            continue
        if cfg.color.motion_smoothing == ColorCfg().motion_smoothing:
            continue  # shipped default — nothing noteworthy
        for s in cfg.scenes:
            display = resolve_scene_display(s.display, s.type)
            if display != "mhires" or s.palette_mode != "percell":
                continue  # motion_smoothing only affects mhires percell
            out.append(
                Diagnostic(
                    level="ok",
                    category="color",
                    subject=f"{name}/{s.name or s.type}/motion_smoothing",
                    message=(
                        f"{cfg.color.motion_smoothing} (higher = less flicker / more "
                        "after-image, lower = crisper motion) for this mhires percell scene."
                    ),
                )
            )
    return out


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


def _probe_extras() -> list[Diagnostic]:
    out: list[Diagnostic] = []
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
                    hint="uv sync --all-extras",
                )
            )
        else:
            out.append(
                Diagnostic(
                    level="ok", category="extras", subject=extra, message=f"installed ({module})"
                )
            )
    return out


def _probe_connectivity(loaded: LoadResult) -> list[Diagnostic]:
    """Try `Ultimate64API(...)` once per system. Catches SocketDMAError
    so doctor mode completes even when no U64 is powered on. Also probes
    REU enable status when the per-system config opts into a REU-staged
    path (mic, video audio, or char-mode video) — those silently
    produce silent audio / garbled video when REU is disabled at the U64.
    """
    from .backend import make_backend
    from .socket_dma import SocketDMAError
    from .teensyrom_dma import TRError

    out: list[Diagnostic] = []
    for name, cfg in zip(loaded.names, loaded.cfgs, strict=True):
        is_tr = cfg.hardware.backend == "teensyrom"
        url = cfg.ultimate64.url
        try:
            api = make_backend(cfg)
        except SocketDMAError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="connectivity",
                    subject=name,
                    message=f"DMA connect to {url} failed: {e}",
                    hint=(
                        "Enable F2 -> Network Settings -> Ultimate DMA Service. "
                        "If a password is set, supply it via "
                        "C64CAST_DMA_PASSWORD or [ultimate64].dma_password."
                    ),
                )
            )
            continue
        except TRError as e:
            out.append(
                Diagnostic(
                    level="error",
                    category="connectivity",
                    subject=name,
                    message=f"TeensyROM connect failed: {e}",
                    hint=(
                        "Check the USB data cable to the TR's micro-USB-B port "
                        "(transport = serial) or 'Enable TCP Listener' + the "
                        "host IP (transport = tcp)."
                    ),
                )
            )
            continue
        try:
            status = api.probe()
            if is_tr:
                # The TeensyROM has no REST surface; probe() is the ping/FW line.
                # It also has no REU, so the REST REU/SID-enable probes below
                # don't apply — instead just flag a REU opt-in as ignored.
                if status is None:
                    out.append(
                        Diagnostic(
                            level="warn",
                            category="connectivity",
                            subject=name,
                            message="TeensyROM transport reachable but ping failed",
                            hint="Writes may still work; check the firmware version.",
                        )
                    )
                else:
                    out.append(
                        Diagnostic(
                            level="ok",
                            category="connectivity",
                            subject=name,
                            message=f"TeensyROM reachable ({status})",
                        )
                    )
                    out.extend(_probe_reu_unavailable(name, cfg, api))
            elif status is None:
                wants_runner, runner_reasons = _wants_rest_runner(cfg)
                if wants_runner:
                    # SID playback + .prg/.crt launch start via the REST
                    # run_prg endpoint, so a dead REST link means these scenes
                    # cannot run at all — an error, not a warning.
                    out.append(
                        Diagnostic(
                            level="error",
                            category="connectivity",
                            subject=name,
                            message=(
                                f"DMA reachable at {url} but REST probe failed "
                                f"— scenes that launch a program cannot start "
                                f"({', '.join(runner_reasons)})"
                            ),
                            hint="The SID player and .prg/.crt launcher use the "
                            "Ultimate's REST run_prg endpoint. Enable the "
                            "Ultimate's web/remote-control service (F2 -> "
                            "Network Settings), or use only DMA-rendered scenes "
                            "(video/slideshow/webcam/blank).",
                        )
                    )
                else:
                    out.append(
                        Diagnostic(
                            level="warn",
                            category="connectivity",
                            subject=name,
                            message=f"DMA reachable at {url} but REST probe failed",
                            hint="REST powers reads (keyboard control), machine "
                            "reset, and program launch; writes still work via "
                            "DMA so DMA-rendered scenes play. Enable the "
                            "Ultimate's web/remote-control service (F2 -> "
                            "Network Settings) if you need those.",
                        )
                    )
            else:
                out.append(
                    Diagnostic(
                        level="ok",
                        category="connectivity",
                        subject=name,
                        message=f"DMA + REST reachable at {url} ({status})",
                    )
                )
                # Probe REU status when the config opts into any REU-staged
                # path. Skipped when REST already failed (the REU probe
                # would just fail with the same error).
                out.extend(_probe_reu_status(name, cfg, api))
                # Probe SID enable state when the config drives the SID.
                # Catches the U2+ "emulated SID disabled" case where every
                # tune is silent while video + the oscilloscope still work.
                out.extend(_probe_sid_status(name, cfg, api))
                # Probe the Ultimate Audio sampler state when the config will
                # use it for video audio (backend auto/sampler + video scene).
                out.extend(_probe_sampler_status(name, cfg, api))
                # Probe DAC calibration status live — the offline
                # _validate_dac_curve check can't know which physical SID
                # socket is currently mapped to $D400, so it's approximate;
                # this one is precise.
                out.extend(_probe_dac_calibration_status(name, cfg, api))
                # Probe SID model autoconfig status live — offline validation
                # can only check [ultimate64].sid_model is a known value; this
                # reports what's actually socketed right now.
                out.extend(_probe_sid_autoconfig_status(name, cfg, api))
        finally:
            api.close()
    return out


def _probe_reu_unavailable(name: str, cfg: object, api: object) -> list[Diagnostic]:
    """On a backend with no REU (e.g. TeensyROM), report that a config's
    REU-staged opt-in is ignored. cli.build_stack coerces these off to the
    host-DMA paths, so this is informational, not a failure."""
    wants, reasons = _wants_reu(cfg)
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


def _wants_reu(cfg: object) -> tuple[bool, list[str]]:
    """Return (wants_reu, list of reasons). Reasons name which config flags
    flipped the want, so the doctor message can point the user at the right
    place to either turn the REU on at the U64 or flip the flag off."""
    reasons: list[str] = []
    # `cfg` is a config.Config; importing the type at module top would
    # introduce a circular doctor↔config import, so duck-type.
    audio = getattr(cfg, "audio", None)
    video = getattr(cfg, "video", None)
    if audio is not None and getattr(audio, "use_reu_pump", False):
        reasons.append("[audio].use_reu_pump = true")
    # Only an EXPLICIT `use_reu_staged = true` is a hard REU requirement. The
    # default "auto" is self-healing (config.resolve_use_reu_staged falls back
    # to host-DMA when REU is off), so it must NOT make the doctor demand REU —
    # `is True` excludes both the "auto" string and any other truthy value.
    if video is not None and getattr(video, "use_reu_staged", False) is True:
        reasons.append("[video].use_reu_staged = true")
    # The Ultimate Audio sampler streams its PCM ring out of REU SDRAM, so a run
    # that will use it needs the REU enabled + sized. Provisioning it also makes
    # "auto" video resolve to the tear-free REU bank-swap path — and since the
    # sampler runs off the C64 bus with no $0314 IRQ, REU-staged video and the
    # sampler coexist cleanly (no NMI/IRQ contention). Forward ref to
    # _wants_sampler (both are module-level; resolved at call time).
    wants_samp, _ = _wants_sampler(cfg)
    if wants_samp:
        reasons.append("[audio].backend sampler (REU-backed PCM ring)")
    # A buffered ASID scene streams frame-slots out of a REU ring, so a run with
    # one (asid_buffered_player auto/on) needs the REU enabled + sized. "auto"
    # only turns on where an REU exists — and provision_reu is itself gated on
    # supports_reu — so both auto and on are a genuine want here (unlike video's
    # self-healing use_reu_staged = "auto").
    scenes = getattr(cfg, "scenes", None) or []
    if any(
        getattr(s, "type", None) == "asid"
        and getattr(s, "asid_buffered_player", "auto") in ("auto", "on")
        for s in scenes
    ):
        reasons.append("[[scenes]] asid with asid_buffered_player (REU ring player)")
    return bool(reasons), reasons


# The Ultimate REST API returns the "RAM Expansion Unit" setting under
# this category path. Both the U64 and U2+ use the same category name.
_REU_CONFIG_CATEGORY = "C64 and Cartridge Settings"
_REU_ENABLED_FIELD = "RAM Expansion Unit"
_REU_SIZE_FIELD = "REU Size"

# The firmware's "REU Size" enum labels (1541ultimate software/io/c64/c64.cc
# reu_size[]) → capacity in bytes. Used to (a) decide whether the U64's current
# REU is large enough for c64cast's staged offsets and (b) pick the size to
# provision. c64cast's highest REU offset is the video staging region near
# 14 MB (modes.REU_VIDEO_BITMAP_COLOR_BASE = $E13000); the audio mic ring sits
# near 1 MB. 16 MB covers every offset and is FPGA-backed (free), so the
# provisioner always sizes to the max when it enables the REU.
_REU_SIZE_BYTES: dict[str, int] = {
    "128 KB": 128 << 10,
    "256 KB": 256 << 10,
    "512 KB": 512 << 10,
    "1 MB": 1 << 20,
    "2 MB": 2 << 20,
    "4 MB": 4 << 20,
    "8 MB": 8 << 20,
    "16 MB": 16 << 20,
}
_REU_PROVISION_SIZE = "16 MB"


def read_reu_config(api: object) -> tuple[bool | None, str | None]:
    """Read the U64's REU state over REST. Returns ``(enabled, size_label)``.

    ``enabled`` is True/False, or None when the query failed or the field was
    absent (an unrecognized firmware shape) — i.e. "can't tell". ``size_label``
    is the raw "REU Size" string (e.g. ``"2 MB"``) or None. Reuses the shared
    `_fetch_config_section` normalizer so it tracks firmware response-shape
    variants identically to `reu_is_enabled`."""
    section, _data, err = _fetch_config_section(
        api, _REU_CONFIG_CATEGORY, field_hint=_REU_ENABLED_FIELD
    )
    if err is not None or not section:
        return None, None
    enabled_raw = section.get(_REU_ENABLED_FIELD)
    enabled = None if enabled_raw is None else (enabled_raw == "Enabled")
    size_raw = section.get(_REU_SIZE_FIELD)
    size = size_raw if isinstance(size_raw, str) else None
    return enabled, size


def provision_reu(api: object, cfg: object) -> dict[str, str] | None:
    """Auto-enable + size the U64 REU for a run that needs it — LIVE + VOLATILE.

    Returns the original ``{field: value}`` to hand back to `restore_reu` at
    teardown, or None when nothing was changed (so a no-op is cheap to detect).
    Gated entirely here so `cli.build_stack` can call it unconditionally:

      * ``[ultimate64].auto_reu`` must be on (default true),
      * the backend must have an REU (``profile.supports_reu`` — Ultimate only),
      * a probe must be allowed (not ``--skip-probe`` — we never write config we
        can't first read back to restore),
      * the config must HARD-require the REU (`_wants_reu`: ``use_reu_pump`` or
        an explicit ``use_reu_staged = true`` — the same condition that makes
        `_probe_reu_status` demand the REU). The default ``use_reu_staged =
        "auto"`` is left alone: it self-heals to the host-DMA double-buffer
        path (also tear-free) without mutating the user's machine config.

    Enables the REU if off and grows it to 16 MB if smaller. The change is NOT
    saved to flash, so it reverts on the next power-cycle even if teardown's
    restore never runs. Best-effort: a REST failure logs a warning and returns
    whatever was changed so far (so teardown still restores it)."""
    if not getattr(getattr(cfg, "ultimate64", None), "auto_reu", False):
        return None
    profile = getattr(api, "profile", None)
    if profile is None or not getattr(profile, "supports_reu", False):
        return None
    if getattr(getattr(cfg, "debug", None), "skip_probe", False):
        return None
    wants, reasons = _wants_reu(cfg)
    if not wants:
        return None

    import requests

    enabled, cur_size = read_reu_config(api)
    if enabled is None:
        log.warning(
            "auto_reu: config needs the REU (%s) but the U64's REU state could "
            "not be read — leaving it unchanged.",
            ", ".join(reasons),
        )
        return None

    restore: dict[str, str] = {}
    if not enabled:
        try:
            api.put_config_item(_REU_CONFIG_CATEGORY, _REU_ENABLED_FIELD, "Enabled")  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("auto_reu: could not enable the U64 REU over REST: %s", e)
            return restore or None
        restore[_REU_ENABLED_FIELD] = "Disabled"

    cur_bytes = _REU_SIZE_BYTES.get(cur_size or "", 0)
    if cur_bytes < _REU_SIZE_BYTES[_REU_PROVISION_SIZE]:
        try:
            api.put_config_item(  # type: ignore[attr-defined]
                _REU_CONFIG_CATEGORY, _REU_SIZE_FIELD, _REU_PROVISION_SIZE
            )
        except requests.RequestException as e:
            log.warning("auto_reu: could not set REU size to %s: %s", _REU_PROVISION_SIZE, e)
        else:
            if cur_size is not None:
                restore[_REU_SIZE_FIELD] = cur_size

    if restore:
        log.info(
            "auto_reu: U64 REU enabled (size %s) for this run (%s) — live, "
            "volatile (reverts on power-cycle), restored at teardown.",
            _REU_PROVISION_SIZE,
            ", ".join(reasons),
        )
    return restore or None


def restore_reu(api: object, restore: dict[str, str] | None) -> None:
    """Put the REU config fields changed by `provision_reu` back to their
    original values (called once per stack at teardown). No-op when nothing was
    provisioned. Best-effort — a failed restore just logs (the change was
    volatile anyway, so a power-cycle clears it)."""
    if not restore:
        return

    import requests

    for fieldname, value in restore.items():
        try:
            api.put_config_item(_REU_CONFIG_CATEGORY, fieldname, value)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("auto_reu: could not restore U64 %s = %s: %s", fieldname, value, e)
        else:
            log.info("auto_reu: restored U64 %s = %s", fieldname, value)


def reu_is_enabled(api: object) -> bool | None:
    """Query the Ultimate's REU enable state over REST.

    Returns True/False when the firmware reports it, or None when the query
    failed or the response shape was unrecognized. Used by cli.build_stack to
    resolve the [video].use_reu_staged "auto" setting — a None (can't tell) is
    treated as "not available" there so auto degrades to host-DMA rather than
    staging into a REU that might be off (which would silently freeze video)."""
    section, _data, err = _fetch_config_section(
        api, _REU_CONFIG_CATEGORY, field_hint=_REU_ENABLED_FIELD
    )
    if err is not None or not section:
        return None
    return section.get(_REU_ENABLED_FIELD) == "Enabled"


# ---- Ultimate Audio FPGA PCM sampler ($DF20-$DFFF) ----------------------
# The $DF20 I/O map lives in "C64 and Cartridge Settings"; the stereo mixer
# routing/level in "Audio Mixer". The presence of these config keys is how we
# detect that the firmware exposes the sampler at all (sampler.py).
_SAMPLER_MAP_CATEGORY = _REU_CONFIG_CATEGORY  # "C64 and Cartridge Settings"
_SAMPLER_MAP_FIELD = "Map Ultimate Audio $DF20-DFFF"
_SAMPLER_MIXER_CATEGORY = "Audio Mixer"
_SAMPLER_VOL_FIELDS = ("Vol Sampler L", "Vol Sampler R")
# The mixer volume enum's audible "0 dB" label. The firmware's volumes[] table
# (u64_config.cc) stores it with a LEADING SPACE (" 0 dB", index 24); the REST
# GET returns it verbatim and the PUT expects the same label, so match it.
_SAMPLER_VOL_AUDIBLE = " 0 dB"
_SAMPLER_VOL_OFF = "OFF"
# Composite restore-key separator: provision_sampler spans two config
# categories (map vs mixer), so the restore dict keys are "category\x1ffield".
_RESTORE_SEP = "\x1f"


def read_sampler_config(
    api: object,
) -> tuple[bool | None, bool | None, dict[str, str]]:
    """Read the U64's Ultimate Audio sampler state over REST.

    Returns ``(present, map_enabled, volumes)``:
      * ``present`` — True if the firmware exposes the sampler config keys (it
        has the feature), False if absent, None if the REST query failed.
      * ``map_enabled`` — the $DF20 I/O-map enable (None when not present).
      * ``volumes`` — current ``{field: value}`` for the Sampler mixer channels
        (for restore). Reuses `_fetch_config_section` so it tracks firmware
        response-shape variants identically to the REU/SID probes."""
    cart, _d1, err1 = _fetch_config_section(
        api, _SAMPLER_MAP_CATEGORY, field_hint=_SAMPLER_MAP_FIELD
    )
    mixer, _d2, err2 = _fetch_config_section(
        api, _SAMPLER_MIXER_CATEGORY, field_hint=_SAMPLER_VOL_FIELDS[0]
    )
    if err1 is not None or err2 is not None:
        return None, None, {}
    map_raw = cart.get(_SAMPLER_MAP_FIELD)
    present = (map_raw is not None) and all(f in mixer for f in _SAMPLER_VOL_FIELDS)
    if not present:
        return False, None, {}
    volumes: dict[str, str] = {}
    for field in _SAMPLER_VOL_FIELDS:
        v = mixer.get(field)
        if isinstance(v, str):
            volumes[field] = v
    return True, (map_raw == "Enabled"), volumes


def sampler_is_available(api: object) -> bool | None:
    """True iff the firmware exposes the Ultimate Audio sampler AND it is
    currently usable (the $DF20 I/O map is enabled and at least one Sampler
    mixer channel is not OFF). None when the REST query failed; False when the
    feature is absent / mapped-off / muted.

    Used by `cli._resolve_sampler_available` to resolve [audio].backend — None
    or False degrades to the 4-bit DAC. Run AFTER `provision_sampler` so a box
    this run just enabled reads as available."""
    present, map_enabled, volumes = read_sampler_config(api)
    if present is None:
        return None
    if not present:
        return False
    audible = any(v != _SAMPLER_VOL_OFF for v in volumes.values())
    return bool(map_enabled) and audible


def _wants_sampler(cfg: object) -> tuple[bool, list[str]]:
    """Return (wants_sampler, reasons). The run wants the sampler when audio is
    enabled, [audio].backend is auto/sampler (not the forced DAC), and there's a
    video scene to play through it (the only scene type wired to the sampler)."""
    reasons: list[str] = []
    # Duck-type to avoid a circular doctor<->config import (see _wants_reu).
    audio = getattr(cfg, "audio", None)
    if audio is None or not getattr(audio, "enabled", False):
        return False, reasons
    backend = getattr(audio, "backend", "auto")
    if backend not in ("auto", "sampler"):
        return False, reasons
    scenes = getattr(cfg, "scenes", None) or []
    if any(getattr(s, "type", None) == "video" for s in scenes):
        reasons.append(f"[audio].backend = {backend!r} + video scene(s)")
    return bool(reasons), reasons


def provision_sampler(api: object, cfg: object) -> dict[str, str] | None:
    """Auto-enable the Ultimate Audio sampler for a run that will use it —
    LIVE + VOLATILE (mirrors `provision_reu`). Enables the $DF20 I/O map if off
    and unmutes the Sampler mixer channels if OFF, capturing the originals for
    `restore_sampler` at teardown. Returns the restore dict (composite keys
    ``"category\\x1ffield" -> original``) or None when nothing was changed.

    Gated on ``profile.supports_sampler`` + not ``--skip-probe`` + `_wants_sampler`.
    The change is NOT saved to flash, so it reverts on power-cycle even if the
    restore is missed. Best-effort: a REST failure logs and returns what changed
    so far (so teardown still restores it)."""
    profile = getattr(api, "profile", None)
    if profile is None or not getattr(profile, "supports_sampler", False):
        return None
    if getattr(getattr(cfg, "debug", None), "skip_probe", False):
        return None
    wants, reasons = _wants_sampler(cfg)
    if not wants:
        return None

    import requests

    present, map_enabled, volumes = read_sampler_config(api)
    if present is None:
        log.warning(
            "sampler: config wants the Ultimate Audio sampler (%s) but its state "
            "could not be read — leaving it unchanged.",
            ", ".join(reasons),
        )
        return None
    if not present:
        # Firmware doesn't expose the sampler; resolve falls back to the DAC.
        return None

    restore: dict[str, str] = {}
    if not map_enabled:
        try:
            api.put_config_item(_SAMPLER_MAP_CATEGORY, _SAMPLER_MAP_FIELD, "Enabled")  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not enable %s over REST: %s", _SAMPLER_MAP_FIELD, e)
            return restore or None
        restore[f"{_SAMPLER_MAP_CATEGORY}{_RESTORE_SEP}{_SAMPLER_MAP_FIELD}"] = "Disabled"

    for fieldname, cur in volumes.items():
        if cur != _SAMPLER_VOL_OFF:
            continue
        try:
            api.put_config_item(_SAMPLER_MIXER_CATEGORY, fieldname, _SAMPLER_VOL_AUDIBLE)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not unmute %s: %s", fieldname, e)
        else:
            restore[f"{_SAMPLER_MIXER_CATEGORY}{_RESTORE_SEP}{fieldname}"] = cur

    if restore:
        log.info(
            "sampler: Ultimate Audio enabled for this run (%s) — live, volatile "
            "(reverts on power-cycle), restored at teardown.",
            ", ".join(reasons),
        )
    return restore or None


def restore_sampler(api: object, restore: dict[str, str] | None) -> None:
    """Put the sampler config fields changed by `provision_sampler` back to
    their originals at teardown. No-op when nothing was provisioned. Best-effort
    — a failed restore just logs (the change was volatile anyway)."""
    if not restore:
        return

    import requests

    for key, value in restore.items():
        category, _, fieldname = key.partition(_RESTORE_SEP)
        try:
            api.put_config_item(category, fieldname, value)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not restore %s = %s: %s", fieldname, value, e)
        else:
            log.info("sampler: restored %s = %s", fieldname, value)


# The Ultimate's emulated-SID enable lives here. Both U64 and U2+ expose it.
_AUDIO_CONFIG_CATEGORY = "Audio Output Settings"
_SID_LEFT_FIELD = "SID Left"
_SID_RIGHT_FIELD = "SID Right"


def _wants_sid_audio(cfg: object) -> tuple[bool, list[str]]:
    """Return (wants_sid, reasons). Any of these means c64cast will try to
    produce sound through the C64 SID ($D4xx): global audio streaming (the
    4-bit DAC / video audio), or any waveform/midi scene (which DMA a
    SID player and drive the chip even when [audio].enabled is false)."""
    reasons: list[str] = []
    # Duck-type to avoid a circular doctor<->config import (see _wants_reu).
    audio = getattr(cfg, "audio", None)
    if audio is not None and getattr(audio, "enabled", False):
        reasons.append("[audio].enabled = true")
    scenes = getattr(cfg, "scenes", None) or []
    types = {getattr(s, "type", None) for s in scenes}
    if "waveform" in types:
        reasons.append("waveform (SID oscilloscope) scene(s)")
    if "midi" in types:
        reasons.append("midi scene(s)")
    return bool(reasons), reasons


def _wants_rest_runner(cfg: object) -> tuple[bool, list[str]]:
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
    # Duck-type to avoid a circular doctor<->config import (see _wants_reu).
    scenes = getattr(cfg, "scenes", None) or []
    types = {getattr(s, "type", None) for s in scenes}
    if "waveform" in types:
        reasons.append("waveform (SID player via run_prg) scene(s)")
    if "launcher" in types:
        reasons.append("launcher (.prg/.crt via run_prg) scene(s)")
    # A generative SourceScene with audio_source = "sid" kicks run_sid_player
    # the same way a waveform scene does (see scenes.py SourceScene.setup).
    if any(
        getattr(s, "type", None) == "generative" and getattr(s, "audio_source", None) == "sid"
        for s in scenes
    ):
        reasons.append("generative scene with audio_source = 'sid' (run_prg)")
    return bool(reasons), reasons


def _fetch_config_section(
    api: object,
    category: str,
    *,
    field_hint: str,
) -> tuple[dict[str, object], object, Exception | None]:
    """GET /v1/configs/<category> from the Ultimate and normalize the reply
    to its settings dict. Returns (section, raw_data, None) on success —
    `section` is {} when the response shape is unrecognized, which each probe
    treats on its own (SID stays quiet, REU warns). Returns ({}, None, exc)
    when the REST query itself failed, so the caller can build a probe-
    specific warning.

    Firmware 3.x returns
        {category: {<setting>: <value>, ...}, "errors": []};
    older / variant firmwares may return the section dict directly or as a
    single-item list. `field_hint` (a field expected in the flat shape) lets
    us recognize the direct-dict variant. This normalizer is firmware-coupled
    — single-sourced here so a response-shape change is a one-place fix (it
    previously lived, identically, in both probes).
    """
    from urllib.parse import quote

    import requests

    try:
        # `api` is a real Ultimate64API; reuse its REST session + base URL.
        base_url = api.base_url  # type: ignore[attr-defined]
        session = api.session  # type: ignore[attr-defined]
        url = f"{base_url}/v1/configs/{quote(category)}"
        r = session.get(url, timeout=3.0)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return {}, None, e

    section: dict[str, object] = {}
    if isinstance(data, dict):
        nested = data.get(category)
        if isinstance(nested, dict):
            section = nested
        elif field_hint in data:
            section = data
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        section = data[0]
    return section, data, None


def _probe_sid_status(name: str, cfg: object, api: object) -> list[Diagnostic]:
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

    section, _data, err = _fetch_config_section(
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


def _probe_reu_status(name: str, cfg: object, api: object) -> list[Diagnostic]:
    """If the config wants REU, check the U64's REU setting via REST.
    Returns an empty list when REU isn't requested. Emits:
      * ok    — REU enabled, with the configured size
      * error — REU disabled (the staged-path opt-ins won't work)
      * warn  — REST query failed; can't tell either way
    """
    wants, reasons = _wants_reu(cfg)
    if not wants:
        return []

    subject = f"{name} (REU)"
    reason_str = ", ".join(reasons)

    section, data, err = _fetch_config_section(
        api, _REU_CONFIG_CATEGORY, field_hint=_REU_ENABLED_FIELD
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

    enabled = section.get(_REU_ENABLED_FIELD)
    size = section.get(_REU_SIZE_FIELD, "?")
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
    auto_reu = bool(getattr(getattr(cfg, "ultimate64", None), "auto_reu", False))
    if auto_reu:
        return [
            Diagnostic(
                level="ok",
                category="connectivity",
                subject=subject,
                message=(
                    f"REU is {enabled!r}, but [ultimate64].auto_reu will enable "
                    f"it (size {_REU_PROVISION_SIZE}) live for this run ({reason_str})."
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


def _probe_sampler_status(name: str, cfg: object, api: object) -> list[Diagnostic]:
    """If the config will use the Ultimate Audio sampler for video audio, check
    the U64's sampler state via REST. Returns an empty list when not wanted.
    Emits:
      * ok    — sampler mapped + audible (high-fidelity path ready), OR mapped
                off / muted but the run will auto-enable it live, OR backend is
                'auto' on hardware without the feature (falls back to the DAC)
      * warn  — REST query failed, or an explicit 'sampler' on a no-sampler backend
      * error — explicit 'sampler' but the U64 firmware lacks the feature
    """
    wants, reasons = _wants_sampler(cfg)
    if not wants:
        return []

    subject = f"{name} (Ultimate Audio sampler)"
    reason_str = ", ".join(reasons)
    backend = getattr(getattr(cfg, "audio", None), "backend", "auto")
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

    present, map_enabled, volumes = read_sampler_config(api)
    if present is None:
        return [
            Diagnostic(
                level="warn",
                category="connectivity",
                subject=subject,
                message="REST query for the Ultimate Audio sampler state failed.",
                hint=f"Config will use the sampler ({reason_str}). If video audio is "
                "silent, check F2 -> C64 and Cartridge Settings -> Map Ultimate "
                "Audio $DF20-DFFF, and F2 -> Audio Mixer -> Vol Sampler L/R.",
            )
        ]
    if not present:
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

    audible = any(v != _SAMPLER_VOL_OFF for v in volumes.values())
    if map_enabled and audible:
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
    if not map_enabled:
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


def _wants_dac_calibration_check(cfg: object) -> bool:
    """The run wants a DAC calibration check when audio is enabled and
    [audio].dac_curve is a system-aware curve ('auto' or 'calibrated')."""
    audio = getattr(cfg, "audio", None)
    if audio is None or not getattr(audio, "enabled", False):
        return False
    return getattr(audio, "dac_curve", "auto") in ("auto", "calibrated")


def _probe_dac_calibration_status(name: str, cfg: object, api: object) -> list[Diagnostic]:
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
    from . import dac_calibration

    subject = f"{name} (DAC calibration)"
    curve = getattr(getattr(cfg, "audio", None), "dac_curve", "auto")
    try:
        label, table = dac_calibration.resolve_dac_curve_for_backend(
            cfg,  # type: ignore[arg-type]
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
    key = dac_calibration.resolve_calibration_key(cfg, api)  # type: ignore[arg-type]
    if table is not None:
        message = f"[audio].dac_curve = {curve!r} resolves to {label!r} (key {key!r})."
    else:
        message = f"no calibration applies right now (key {key!r}); resolves to {label!r}."
    return [Diagnostic(level="ok", category="connectivity", subject=subject, message=message)]


def _wants_sid_autoconfig_check(cfg: object) -> bool:
    """The run wants a SID model autoconfig check when [ultimate64].sid_model
    isn't 'off' and a scene will actually drive the SID player — a waveform
    scene, or a generative scene with audio_source = 'sid'
    (SidFileAudioSource; see sid_autoconfig.py's two call sites)."""
    ultimate64 = getattr(cfg, "ultimate64", None)
    if ultimate64 is None or getattr(ultimate64, "sid_model", "off") == "off":
        return False
    scenes = getattr(cfg, "scenes", None) or []
    for s in scenes:
        if getattr(s, "type", None) == "waveform":
            return True
        if getattr(s, "type", None) == "generative" and getattr(s, "audio_source", None) == "sid":
            return True
    return False


def _probe_sid_autoconfig_status(name: str, cfg: object, api: object) -> list[Diagnostic]:
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
    from . import sid_hw_config

    subject = f"{name} (SID model autoconfig)"
    sid_model = getattr(getattr(cfg, "ultimate64", None), "sid_model", "auto")
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
        "scene",
        "audio",
        "color",
        "midi_control",
        "wled",
        "orchestrator",
        "extras",
        "connectivity",
    ]
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
