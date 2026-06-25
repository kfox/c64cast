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
from .config import LoadResult, validate_scene_cfg
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
    ("midi", "mido", "midi scenes"),
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
    if loaded.is_ensemble:
        out.extend(_validate_cross_system_orchestration(loaded))
    out.extend(_probe_extras())
    if probe_u64:
        out.extend(_probe_connectivity(loaded))

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
                out.append(
                    Diagnostic(
                        level="ok",
                        category="scene",
                        subject=subject,
                        message=f"{s.type}/{s.display}, {len(s.overlays)} overlay(s){role}",
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
                out.append(
                    Diagnostic(
                        level="warn",
                        category="connectivity",
                        subject=name,
                        message=f"DMA reachable at {url} but REST probe failed",
                        hint="REST is only used for reads + runners; writes "
                        "still work via DMA. Check Command Interface toggle.",
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
    category_order = ["environment", "scene", "orchestrator", "extras", "connectivity"]
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
