"""Command-line entry point.

CLI flags layer on top of a TOML config (``--config PATH`` or
``./c64cast.toml``). Precedence: built-in defaults < config file < CLI.
Every overridable option uses ``default=None`` so the merge step can tell
"user didn't pass it" from "user passed the default".
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from . import (
    __version__,
    dac_calibration,
    orchestrators,  # noqa: F401 — registers built-in orchestrator subclasses
)
from . import config as cfgmod
from ._native_io import silence_native_stderr
from .api import SocketDMAError
from .audio import AUDIO_AVAILABLE, AudioStreamer
from .backend import C64Backend, make_backend
from .ensemble import Ensemble, SystemStack
from .interstitial import default_factory as interstitial_factory
from .keyboard import CommodoreKeyPoller
from .playlist import Playlist
from .profiler import FrameProfiler, NullProfiler, set_profiler
from .teensyrom_dma import TRError
from .video import WebcamSource
from .vision import MediaPipeHandRecognizer, VisionController

if TYPE_CHECKING:
    from .framebuffer import Framebuffer
    from .preview import PreviewWindow, StreamRecorder

log = logging.getLogger("c64cast")


class StackBuildError(Exception):
    """Raised by build_stack when a per-system stack cannot be constructed.
    The user-facing diagnostic has already been logged; this just carries
    the exit code main() should return."""

    def __init__(self, exit_code: int):
        super().__init__(f"stack build failed (exit code {exit_code})")
        self.exit_code = exit_code


class _CliUsageError(Exception):
    """A CLI-usage mistake (conflicting flags, a bad connection target).
    main() logs the message and returns exit code 2."""


def build_parser() -> argparse.ArgumentParser:
    # Pull defaults from the config dataclasses so help text stays in sync
    # with the actual fallback values. CLI options use default=None at the
    # argparse layer so merge_cli() can distinguish "not provided" from
    # "explicitly set to the default"; the `(default: ...)` shown in --help
    # is the value the merge cascade lands on when nothing overrides it.
    u64_def = cfgmod.Ultimate64Cfg()
    video_def = cfgmod.VideoCfg()
    audio_def = cfgmod.AudioCfg()
    vision_def = cfgmod.VisionCfg()
    playlist_def = cfgmod.PlaylistCfg()
    debug_def = cfgmod.DebugCfg()

    p = argparse.ArgumentParser(
        prog="c64cast",
        description="C64 AV streamer framework (Ultimate 64)",
    )

    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--config", default=None, help="Path to TOML config (default: ./c64cast.toml if it exists)"
    )

    p.add_argument(
        "inputs",
        nargs="*",
        metavar="MEDIA",
        help="Quick-playback media: files, directories, globs, or URLs played "
        "in order, once (no loop unless --loop). Each maps to a scene by kind: "
        "video->video, .sid->waveform, image->slideshow, .prg/.crt->launcher, "
        "URL->video. Omit to run from --config / ./c64cast.toml / defaults. "
        "Mutually exclusive with --config.",
    )

    conn = p.add_argument_group("connection")
    conn.add_argument(
        "-u",
        "--url",
        default=None,
        metavar="TARGET",
        help="Connection target selecting the hardware backend + endpoint "
        f"(default: $C64CAST_URL, else {u64_def.url}). Schemes: u64://HOST or "
        "http(s)://HOST (Ultimate 64 / II+); tr:// (TeensyROM+ USB serial, "
        "auto-detected), tr:///dev/cu.usbmodemXYZ or tr://COM3 (serial device), "
        "tr://HOST (TeensyROM+ TCP). Rare knobs as query params, e.g. "
        "u64://host?dma_port=64 or tr://host?tcp_port=2113.",
    )
    conn.add_argument(
        "-s",
        "--system",
        choices=["NTSC", "PAL"],
        default=None,
        help=f"Target system timing (default: {u64_def.system})",
    )

    quick = p.add_argument_group("quick playback (with MEDIA args)")
    quick.add_argument(
        "--display",
        default=None,
        help="VIC-II display mode for quick-playback video/slideshow scenes (default: mhires).",
    )
    quick.add_argument(
        "-t",
        "--duration",
        type=float,
        default=None,
        help="Seconds for quick-playback scenes that honor it (waveform/slideshow).",
    )

    v = p.add_argument_group("video input")
    v.add_argument(
        "-d",
        "--device",
        type=int,
        default=None,
        help=f"Webcam device index, -1 = system default (default: {video_def.device})",
    )

    a = p.add_argument_group("audio")
    a.add_argument(
        "--audio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Stream audio to the 4-bit SID volume DAC; --no-audio mutes "
        f"(default: {audio_def.enabled})",
    )
    a.add_argument(
        "-D",
        "--audio-device",
        type=int,
        default=None,
        help=f"Audio input device index, -1 = system default microphone (default: {audio_def.device})",
    )
    a.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=None,
        help=f"Audio sample rate in Hz (default: {audio_def.sample_rate})",
    )
    a.add_argument(
        "-m",
        "--mic-sensitivity",
        type=float,
        default=None,
        help=f"Microphone input gain multiplier (default: {audio_def.mic_sensitivity})",
    )
    a.add_argument(
        "-n",
        "--noise-gate",
        type=float,
        default=None,
        help=f"Threshold below which mic input is muted (default: {audio_def.noise_gate})",
    )

    vis = p.add_argument_group("vision input")
    vis.add_argument(
        "--vision",
        action="store_true",
        default=None,
        help="Enable webcam hand-gesture control "
        "(pinch=pause/resume, swipe=skip, open-hand=cycle); "
        f"needs the 'vision' extra (default: {vision_def.enabled})",
    )
    vis.add_argument(
        "--vision-model",
        default=None,
        help=f"Path to the MediaPipe HandLandmarker .task model (default: {vision_def.model_path})",
    )

    pl = p.add_argument_group("playlist")
    pl.add_argument(
        "--videos",
        default=None,
        help=f"Directory containing videos "
        f"({', '.join(cfgmod.VIDEO_EXTS)}) "
        f"(default: {playlist_def.videos_dir})",
    )
    pl.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Loop the playlist after the last scene finishes "
        "(--no-loop = exit after one pass; useful for "
        f'"play one video and quit") (default: {playlist_def.loop})',
    )

    intro = p.add_argument_group("introspection")
    intro.add_argument("--list-scenes", action="store_true", help="List scene types and exit")
    intro.add_argument("--list-overlays", action="store_true", help="List overlays and exit")
    intro.add_argument("--list-modes", action="store_true", help="List display modes and exit")
    intro.add_argument(
        "--describe",
        metavar="NAME",
        default=None,
        help="Describe a scene/overlay/section/mode and exit. "
        "Prefix to disambiguate: scene:, overlay:, "
        "section:, mode: (e.g. --describe overlay:clock)",
    )
    intro.add_argument(
        "--compat",
        action="store_true",
        help="Print the overlay × display-mode compatibility matrix and exit",
    )
    intro.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the JSON Schema for the TOML config and exit "
        "(point your editor's `#:schema` at it for autocomplete)",
    )
    intro.add_argument(
        "--init",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Interactively build a config file (needs the "
        "'wizard' extra). Optional PATH sets the output "
        "file (default ./c64cast.toml)",
    )

    debug = p.add_argument_group("debug")
    debug.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=None,
        help="Increase log verbosity (default: INFO; -v enables DEBUG)",
    )
    debug.add_argument(
        "--heartbeat",
        type=float,
        default=None,
        help=f"Health heartbeat interval in seconds, 0 disables (default: {debug_def.heartbeat})",
    )
    debug.add_argument(
        "--skip-probe",
        action="store_true",
        default=None,
        help=f"Skip the startup U64 reachability probe (default: {debug_def.skip_probe})",
    )
    debug.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio and video input devices and exit",
    )
    debug.add_argument(
        "--doctor",
        action="store_true",
        help="Validate the whole config (all scenes/overlays at "
        "once), check optional extras + probe each U64, then "
        "exit. Add --skip-probe for a fast, offline, "
        "hardware-free config check.",
    )
    debug.add_argument(
        "--calibrate-dac",
        action="store_true",
        help="Measure the connected SID's Mahoney 8-bit $D418 DAC transfer curve "
        "(requires a capture device — Cam Link — on the SID audio output) and save "
        "a per-system calibrated table, then exit. Playback with [audio].dac_curve "
        "= 'auto' (the default) then uses it automatically. Most valuable for "
        "physical 6581/8580 chips and SID replacements, which vary chip-to-chip.",
    )
    debug.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Mirror log output to PATH (useful for headless runs)",
    )
    debug.add_argument(
        "--profile",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Emit per-scene frame timing summaries (cpu_render "
        "/ compose / push / wait, plus DMA writes/bytes per "
        f"frame) (default: {debug_def.profile})",
    )
    debug.add_argument(
        "--profile-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"Seconds between profiler summary lines (default: {debug_def.profile_interval})",
    )
    debug.add_argument(
        "--frame-numbers",
        action="store_true",
        default=None,
        help="Overlay playback timecode + source frame number on "
        "video frames (debug aid for locating flashing "
        f"frames) (default: {debug_def.frame_numbers})",
    )
    return p


def configure_logging(verbosity: int, log_file: str | None = None) -> None:
    """Wire up the root logger.

    Terminal: RichHandler (color + columns) when `rich` is installed; plain
    StreamHandler otherwise. File: when `log_file` is given, also append to
    that path with a verbose plain-text format. Safe to call more than once
    — clears any existing handlers first so a re-call (e.g. after config
    load) doesn't double up."""
    # Default level is INFO so the user sees lifecycle messages (scene
    # transitions, audio bring-up, keypress detection, resets) without
    # needing -v. -v / -vv bumps to DEBUG.
    level = logging.INFO
    if verbosity >= 1:
        level = logging.DEBUG

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    try:
        # rich is an optional [logging] extra; pyright doesn't see it unless installed.
        from rich.logging import RichHandler  # pyright: ignore[reportMissingImports]

        terminal: logging.Handler = RichHandler(
            level=level,
            show_path=False,
            rich_tracebacks=True,
            log_time_format="%H:%M:%S",
        )
        terminal.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    except ImportError:
        terminal = logging.StreamHandler()
        terminal.setLevel(level)
        terminal.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
        )
    root.addHandler(terminal)

    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
        except OSError as e:
            # Don't let a bad --log-file path kill the run; surface and
            # continue with just the terminal handler.
            log.warning("could not open log file %s: %s", log_file, e)
        else:
            fh.setLevel(level)
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                )
            )
            root.addHandler(fh)


def _log_dma_setup_error(cfg: cfgmod.Config, e: SocketDMAError, *, role: str) -> None:
    """Emit a multi-line, user-actionable error covering both the
    'service disabled' and 'auth' cases. The role label disambiguates
    the render vs audio sockets in the log so the user knows which one
    failed if only one of them does."""
    log.error(
        "Could not open the U64 Socket DMA %s socket at %s:%d.",
        role,
        cfg.ultimate64.url,
        cfg.ultimate64.dma_port,
    )
    log.error("Underlying error: %s", e)
    log.error("Check, in order:")
    log.error("  1. F2 Menu -> Network Settings -> Ultimate DMA Service -> Enabled")
    log.error("  2. F2 Menu -> Network Settings -> Command Interface -> Enabled")
    log.error(
        "     (both toggles must be on; the second one gates command "
        "dispatch even when the listening socket is open)"
    )
    log.error(
        "  3. If a network password is set on the U64, supply it via the "
        "C64CAST_DMA_PASSWORD env var or [ultimate64] dma_password."
    )
    log.error("Save and reboot the U64 after changing either toggle.")


def list_devices() -> int:
    print("Audio input devices (use with -D / --audio-device):")
    if AUDIO_AVAILABLE:
        import sounddevice as sd

        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None
        any_input = False
        for idx, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] <= 0:
                continue
            any_input = True
            marker = " *" if idx == default_in else "  "
            print(
                f" {marker}[{idx}] {d['name']} "
                f"({d['max_input_channels']}ch @ {int(d['default_samplerate'])} Hz)"
            )
        if not any_input:
            print("    (no input-capable audio devices found)")
    else:
        print("    (sounddevice not installed)")

    print()
    print("Video input devices (use with -d / --device):")
    import cv2

    found = []
    # Probing past the highest valid index makes OpenCV (and the AVFoundation
    # / FFmpeg backends underneath it) print to stderr at the C level. Mute
    # those for the duration of the probe via fd-level redirection.
    sys.stdout.flush()
    with silence_native_stderr():
        for idx in range(8):
            cap = cv2.VideoCapture(idx)
            try:
                if cap is not None and cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    found.append((idx, w, h))
            finally:
                if cap is not None:
                    cap.release()
    if found:
        for idx, w, h in found:
            print(f"   [{idx}] {w}x{h}")
    else:
        print("    (no webcams responded to OpenCV probe)")

    if sys.platform == "darwin":
        # Prefer the jq pipeline when jq is on PATH — it collapses
        # system_profiler's verbose multi-line dump into a clean
        # `index:name` listing that lines up with AVFoundation's (and
        # therefore OpenCV's) device enumeration. Falls back to the raw
        # dump when jq isn't installed.
        cmd = (
            [
                "sh",
                "-c",
                "system_profiler -json SPCameraDataType 2>/dev/null | "
                "jq -r '.SPCameraDataType[]._name' | nl -v0 -w1 -s:",
            ]
            if shutil.which("jq")
            else ["system_profiler", "SPCameraDataType"]
        )
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            print()
            print("macOS cameras (system_profiler SPCameraDataType):")
            for line in out.stdout.splitlines():
                if line.strip():
                    print(f"    {line.rstrip()}")
    return 0


def _resolve_reu_available(cfg: cfgmod.Config, api: C64Backend) -> bool:
    """Decide whether [video].use_reu_staged = "auto" should enable REU
    bank-swap staging for this system, by asking the U64 if its REU is on.

    Returns False (auto → host-DMA everywhere) unless the setting is literally
    "auto" AND the backend has a REU AND a probe is allowed AND the firmware
    reports the REU Enabled. Any uncertainty (explicit setting, --skip-probe,
    no-REU backend, failed query, REU disabled) degrades to host-DMA so video
    never silently freezes. Logs the verdict so the chosen path is visible."""
    if cfg.video.use_reu_staged != "auto":
        return False  # explicit true/false ignores the probe entirely
    if not api.profile.supports_reu:
        return False  # backend (e.g. TeensyROM) has no REU to stage into
    if cfg.debug.skip_probe:
        log.info(
            "[video].use_reu_staged = auto, but --skip-probe is set — "
            "keeping video on the host-DMA path (REU undetected)."
        )
        return False
    from . import doctor

    enabled = doctor.reu_is_enabled(api)
    if enabled:
        log.info(
            "[video].use_reu_staged = auto: U64 REU is enabled — "
            "double-buffering bitmap (hires/mhires) scenes via REU "
            "bank-swap; char modes stay on host-DMA."
        )
        return True
    if enabled is None:
        log.warning(
            "[video].use_reu_staged = auto: could not read the U64 REU "
            "state — keeping video on the host-DMA path."
        )
    else:
        log.info(
            "[video].use_reu_staged = auto: U64 REU is disabled — "
            "keeping video on the host-DMA path (enable it at F2 -> C64 "
            "and Cartridge Settings -> RAM Expansion Unit to "
            "double-buffer bitmap scenes)."
        )
    return False


def _resolve_sampler_available(cfg: cfgmod.Config, api: C64Backend) -> bool:
    """Decide whether the U64 "Ultimate Audio" FPGA PCM sampler should back
    video-scene audio for this system, by asking the U64 whether it's exposed +
    routed (mirrors `_resolve_reu_available`).

    Returns False (→ the 4-bit DAC) unless [audio].backend is auto/sampler AND
    the backend has the sampler (supports_sampler) AND a probe is allowed AND
    the firmware reports it available. `provision_sampler` runs BEFORE this in
    build_stack, so a box this run just enabled reads available. Any uncertainty
    (forced dac, --skip-probe, no-sampler backend, failed query, mapped-off)
    degrades to the DAC so audio is never silently silent."""
    if cfg.audio.backend == "dac":
        return False  # forced DAC ignores the probe entirely
    if not api.profile.supports_sampler:
        return False  # backend (e.g. TeensyROM) has no FPGA sampler
    if cfg.debug.skip_probe:
        log.info(
            "[audio].backend = %s, but --skip-probe is set — using the 4-bit "
            "DAC for video audio (sampler undetected).",
            cfg.audio.backend,
        )
        return False
    from . import doctor

    avail = doctor.sampler_is_available(api)
    if avail:
        log.info(
            "[audio].backend = %s: Ultimate Audio sampler available — "
            "high-fidelity video audio (FPGA PCM, off the C64 bus).",
            cfg.audio.backend,
        )
        return True
    if avail is None:
        log.warning(
            "[audio].backend = %s: could not read the Ultimate Audio sampler "
            "state — using the 4-bit DAC for video audio.",
            cfg.audio.backend,
        )
    else:
        log.info(
            "[audio].backend = %s: Ultimate Audio sampler not available "
            "(map disabled / mixer muted / firmware lacks it) — using the "
            "4-bit DAC for video audio.",
            cfg.audio.backend,
        )
    return False


def _coerce_reu_for_backend(cfg: cfgmod.Config, api: C64Backend) -> None:
    """Disable the REU-staged audio/video opt-ins when the backend has no REU.

    A backend without an REU (e.g. TeensyROM — no REUWRITE opcode) can't run
    the REU-staged paths; `[audio].use_reu_pump` / an explicit
    `[video].use_reu_staged = true` would otherwise reach `reu_write` and raise.
    Coerce them off (in place — config dataclasses are mutable) so the host-DMA
    NMI DAC / host-DMA video paths are used instead. `use_reu_staged = "auto"`
    already self-heals via `_resolve_reu_available` (which returns False when
    `not supports_reu`), so only the explicit opt-ins need handling here."""
    if api.profile.supports_reu:
        return
    if cfg.audio.use_reu_pump:
        log.warning(
            "[audio].use_reu_pump needs an REU; the %s backend has none — "
            "using the host-DMA NMI DAC path instead",
            cfg.hardware.backend,
        )
        cfg.audio.use_reu_pump = False
    if cfg.video.use_reu_staged is True:  # explicit true (auto self-heals)
        log.warning(
            "[video].use_reu_staged = true needs an REU; the %s backend has "
            "none — using the host-DMA video path instead",
            cfg.hardware.backend,
        )
        cfg.video.use_reu_staged = False


def build_stack(
    cfg: cfgmod.Config,
    name: str,
    args: argparse.Namespace,
    *,
    stop_event: threading.Event,
    profiler: FrameProfiler | NullProfiler,
    is_ensemble: bool = False,
    config_path: str | None = None,
) -> SystemStack:
    """Construct one system's full runtime stack (api + audio + source +
    playlist + preview/recording). Raises StackBuildError on any failure
    that should terminate the process; the user-facing message is logged
    before the raise. The caller is responsible for tearing down whatever
    stacks succeeded if a later one fails.

    `is_ensemble=True` propagates into `scenes_from_config` so live
    scenes (webcam, blank) are built with audio suppressed — the
    ensemble audio lock arbitrates which system drives the SID."""
    # Only open the camera when a scene actually needs it. Skipping the open
    # otherwise means a "blank" or "waveform"-only playlist won't fail on a
    # box without a webcam (or one whose OS-level camera permission is denied,
    # which is the typical macOS first-run snag in IDE-launched runs).
    # The shared camera broker feeds both webcam scenes and the (always-on)
    # vision controller, so open it if either wants it.
    needs_webcam = any(s.type == "webcam" for s in cfg.scenes)
    needs_camera = needs_webcam or cfg.vision.enabled
    source: WebcamSource | None = None
    if needs_camera:
        try:
            source = WebcamSource(cfg.video.device)
        except RuntimeError as e:
            log.error("%s", e)
            raise StackBuildError(1) from e
    else:
        log.debug("no webcam or vision scenes — skipping video device init")

    try:
        api = make_backend(cfg)
    except SocketDMAError as e:
        _log_dma_setup_error(cfg, e, role="render")
        if source is not None:
            source.release()
        raise StackBuildError(4) from e
    except TRError as e:
        log.error(
            "TeensyROM connect failed (%s): %s. Check the cable / "
            "serial port (transport=serial) or 'Enable TCP Listener' "
            "+ host (transport=tcp).",
            name,
            e,
        )
        if source is not None:
            source.release()
        raise StackBuildError(4) from e

    if not cfg.debug.skip_probe:
        status = api.probe()
        if status is None:
            log.error(
                "Could not reach the C64 hardware (%s backend) — check "
                "power, connection, and config. (use --skip-probe to "
                "bypass)",
                cfg.hardware.backend,
            )
            api.close()
            if source is not None:
                source.release()
            raise StackBuildError(2)
        log.info("%s reachable: %s", cfg.hardware.backend, status)

    # Drop REU-staged opt-ins on a backend with no REU, before the AudioStreamer
    # + scenes are built (so the host-DMA paths are used instead).
    _coerce_reu_for_backend(cfg, api)

    # Auto-provision the U64 REU (enable + size to 16 MB, live + volatile) for
    # runs that hard-require it, so the REU-staged audio/video paths "just work"
    # without the manual F2 enable step. No-op unless [ultimate64].auto_reu is
    # on, the backend has an REU, a probe is allowed, and the config hard-needs
    # the REU (see doctor.provision_reu). Runs BEFORE _resolve_reu_available so
    # that probe sees the now-enabled REU; restored at teardown (teardown_stack).
    from . import doctor as _doctor

    reu_restore = _doctor.provision_reu(api, cfg)
    # Auto-enable the Ultimate Audio sampler (map $DF20 + unmute Sampler mixer,
    # live + volatile) when a video scene will use it. Runs BEFORE
    # _resolve_sampler_available so the probe sees it on; restored at teardown.
    sampler_restore = _doctor.provision_sampler(api, cfg)

    # Resolve the system-aware [audio].dac_curve ("auto"/"calibrated") to a
    # concrete (label, table) for this backend + any per-unit calibration.
    dac_curve_label, dac_table = dac_calibration.resolve_dac_curve_for_backend(cfg)
    if cfg.audio.enabled and dac_curve_label != cfg.audio.dac_curve:
        log.info("audio: dac_curve %s → %s", cfg.audio.dac_curve, dac_curve_label)

    audio = (
        AudioStreamer(
            api,
            cfg.audio.sample_rate,
            cfg.ultimate64.system,
            dither=cfg.audio.dither,
            digi_boost=cfg.audio.digi_boost,
            dac_curve=dac_curve_label,
            dac_table=dac_table,
            sid_filter_cutoff=cfg.audio.sid_filter_cutoff,
            use_reu_pump=cfg.audio.use_reu_pump,
            reu_pump_governor=cfg.audio.reu_pump_governor,
            host_dma_servo=cfg.audio.host_dma_servo,
            nmi_rate_adaptive=cfg.audio.nmi_rate_adaptive,
            dsp_params=cfg.dsp.to_params(),
        )
        if cfg.audio.enabled
        else None
    )

    reu_available = _resolve_reu_available(cfg, api)
    sampler_available = _resolve_sampler_available(cfg, api)
    playlist_scenes = cfgmod.scenes_from_config(
        cfg,
        api,
        audio,
        source,
        is_ensemble=is_ensemble,
        reu_available=reu_available,
        sampler_available=sampler_available,
    )

    # The system video rate (60 NTSC / 50 PAL) is resolved into the
    # backend's profile by make_backend; a per-variant `max_fps` cap (None
    # for the Ultimate) clamps it. Today this resolves identically to the
    # old `60 if NTSC else 50`.
    target_fps = api.profile.default_fps
    if api.profile.max_fps is not None:
        target_fps = min(target_fps, api.profile.max_fps)

    log.info("%s: reset + run BASIC clear loop", cfg.hardware.backend)
    api.reset()
    time.sleep(1)
    api.run_basic_clear_loop()
    api.disable_case_switch()

    # The Commodore-key poller reads $028D over the wire. A backend that can't
    # read C64 memory (an older TeensyROM firmware without ReadC64Mem) has no
    # physical-keyboard control — skip the poller; the HTTP control plane is
    # the read-free equivalent. (The Ultimate and cycle-clean TR+ both read.)
    key_poller = CommodoreKeyPoller(api, name=name) if api.profile.supports_read else None
    if key_poller is None:
        log.info(
            "%s: physical-keyboard control unavailable (no memory read) "
            "— use the control plane for pause/resume/skip",
            name,
        )

    # Optional: webcam hand-gesture control. Reads the shared camera (not C64
    # memory), so it works on any backend. A missing mediapipe dep / model
    # file degrades to "no gesture control" rather than killing the stream.
    vision_controller: VisionController | None = None
    if cfg.vision.enabled:
        assert source is not None  # needs_camera guaranteed it above
        try:
            recognizer = MediaPipeHandRecognizer(
                cfg.vision.model_path,
                num_hands=cfg.vision.num_hands,
                min_detection_confidence=cfg.vision.min_detection_confidence,
                min_tracking_confidence=cfg.vision.min_tracking_confidence,
            )
            vision_controller = VisionController(
                source,
                recognizer,
                poll_interval_s=cfg.vision.poll_interval_s,
                hold_threshold_s=cfg.vision.hold_threshold_s,
                gesture_cooldown_s=cfg.vision.gesture_cooldown_s,
                gesture_dwell_s=cfg.vision.gesture_dwell_s,
                pinch_threshold=cfg.vision.pinch_threshold,
                swipe_velocity=cfg.vision.swipe_velocity,
                mirror=cfg.vision.mirror,
                name=name,
            )
            log.info("%s: vision gesture control enabled", name)
        except RuntimeError as e:
            log.error("vision control disabled: %s", e)

    # Optional: local preview window + stream recorder. Both share a
    # Framebuffer that shadows U64 memory writes via api listeners.
    framebuffer: Framebuffer | None = None
    preview_window: PreviewWindow | None = None
    recorder: StreamRecorder | None = None
    if cfg.preview.enabled or cfg.recording.enabled:
        from .framebuffer import Framebuffer as _FB

        framebuffer = _FB(charset_path=cfg.preview.charset_path)
        api.add_write_listener(framebuffer.on_write)
    if cfg.preview.enabled:
        assert framebuffer is not None
        try:
            from .preview import PreviewWindow as _PW

            preview_window = _PW(framebuffer, fps=cfg.preview.fps, scale=cfg.preview.scale)
            preview_window.start()
        except RuntimeError as e:
            log.error("preview disabled: %s", e)
    if cfg.recording.enabled:
        assert framebuffer is not None
        try:
            from .preview import StreamRecorder as _SR

            recorder = _SR(
                framebuffer,
                cfg.recording.path,
                fps=cfg.recording.fps,
                scale=cfg.recording.scale,
                fourcc=cfg.recording.fourcc,
            )
            recorder.start()
        except RuntimeError as e:
            log.error("recording disabled: %s", e)

    playlist = Playlist(
        playlist_scenes,
        api,
        target_fps,
        heartbeat_interval=cfg.debug.heartbeat,
        stop_event=stop_event,
        interstitial_factory=interstitial_factory(api, cfg.interstitial),
        key_poller=key_poller,
        vision_controller=vision_controller,
        profiler=profiler,
        name=name,
        loop=cfg.playlist.loop,
        fade_duration_s=cfg.playlist.fade_duration_s,
        audio=audio,
        audio_calibration=(
            {
                "petscii": cfg.audio.pitch_mult_petscii,
                "hires": cfg.audio.pitch_mult_hires,
                "mhires": cfg.audio.pitch_mult_mhires,
                "mcm": cfg.audio.pitch_mult_mcm,
                "blank": cfg.audio.pitch_mult_blank,
            }
            if cfg.audio.enabled
            else None
        ),
        menu_cfg=cfg.menu,
        config=cfg,
        config_path=config_path,
    )

    return SystemStack(
        name=name,
        cfg=cfg,
        api=api,
        audio=audio,
        source=source,
        playlist=playlist,
        key_poller=key_poller,
        vision_controller=vision_controller,
        reu_available=reu_available,
        reu_restore=reu_restore,
        sampler_available=sampler_available,
        sampler_restore=sampler_restore,
        framebuffer=framebuffer,
        preview_window=preview_window,
        recorder=recorder,
    )


def teardown_stack(stack: SystemStack) -> None:
    """Bring one system's stack down cleanly. Each step is independently
    try/except'd so one failure doesn't strand the rest. Order matters:
    stop audio before the final reset so the NMI timer isn't firing into
    a buffer we're about to clear; preview/recording come down first so
    they don't try to render after the API is closed."""
    from . import doctor as _doctor

    for label, fn in (
        ("preview shutdown", lambda: stack.preview_window.stop() if stack.preview_window else None),
        ("recording stop", lambda: stack.recorder.stop() if stack.recorder else None),
        ("audio shutdown", lambda: stack.audio.close() if stack.audio else None),
        (
            "vision controller stop",
            lambda: stack.vision_controller.stop() if stack.vision_controller else None,
        ),
        # Restore any REU config we auto-provisioned, while the REST session is
        # still open (no-op when nothing was changed; volatile regardless).
        ("REU restore", lambda: _doctor.restore_reu(stack.api, stack.reu_restore)),
        # Same for the Ultimate Audio sampler map/mixer auto-provisioning.
        ("sampler restore", lambda: _doctor.restore_sampler(stack.api, stack.sampler_restore)),
        ("U64 reset", stack.api.reset),
        ("API close", stack.api.close),
        ("camera release", lambda: stack.source.release() if stack.source else None),
    ):
        try:
            fn()
        except Exception:
            log.exception("[%s] %s failed", stack.name, label)


# CLI flags that don't make sense in ensemble mode (they pick a single
# system's hardware; the per-system TOML is the right place to set them).
_PER_SYSTEM_CLI_FLAGS: tuple[tuple[str, str], ...] = (
    ("url", "--url"),
    ("device", "--device"),
)


def _run_playlists(stacks: list[SystemStack], stop_event: threading.Event) -> None:
    """Run every stack's playlist on its own worker thread. Block on join.
    Ctrl+C in the main thread sets stop_event so every playlist exits its
    run loop on the next iteration; each thread gets up to 5s to drain
    before we move on and log it as stuck."""
    threads = [
        threading.Thread(target=s.playlist.run, name=f"playlist-{s.name}", daemon=False)
        for s in stacks
    ]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info("interrupted; stopping %d system(s)", len(stacks))
        stop_event.set()
        for t in threads:
            t.join(timeout=5)
            if t.is_alive():
                log.error("[%s] did not exit within 5s; abandoning", t.name)


def run_introspection(args: argparse.Namespace) -> int | None:
    """Handle the config-introspection commands (--list-*, --describe,
    --compat, --print-schema). Returns an exit code when one fired, else None
    so main() continues to the normal run path. These need no config file or
    hardware."""
    from . import introspect

    if args.list_scenes:
        print(introspect.render_list_scenes())
        return 0
    if args.list_overlays:
        print(introspect.render_list_overlays())
        return 0
    if args.list_modes:
        print(introspect.render_list_modes())
        return 0
    if args.compat:
        print(introspect.render_compat())
        return 0
    if args.describe is not None:
        print(introspect.render_describe(args.describe))
        return 0
    if args.print_schema:
        import json

        from . import schema

        print(json.dumps(schema.build_schema(), indent=2))
        return 0
    if args.init is not None:
        from . import wizard

        result = wizard.run_init(args.init or None)
        if result is None:
            return 2  # cancelled, or the 'wizard' extra is missing
        out_path, launch = result
        if launch:
            # Fall through to the normal run path against the file we just
            # wrote (returning None lets main() continue to load_master).
            args.config = out_path
            return None
        return 0
    return None


def _resolve_configs(args: argparse.Namespace) -> tuple[cfgmod.LoadResult, list[cfgmod.Config]]:
    """Produce the per-system configs to run, from one of two front doors:

    * **Quick playback** — positional ``MEDIA`` args build an in-memory,
      single-system config (no TOML on disk), one scene per argument.
      Mutually exclusive with ``--config``.
    * **Config-driven** — ``--config`` / ``./c64cast.toml`` / built-in
      defaults, with CLI flags merged on top.

    The scheme-aware ``-u/--url`` target (or ``$C64CAST_URL``) is applied to the
    single system's connection fields in both single-system paths; in ensemble
    mode connection comes from the per-system TOMLs (per-system identity), so a
    CLI target there is rejected. Raises ``ConfigError`` (exit 5), or
    ``_CliUsageError`` / ``ValueError`` / ``RuntimeError`` (exit 2)."""
    if args.inputs:
        if args.config:
            raise _CliUsageError(
                "positional MEDIA arguments and --config are mutually exclusive "
                "— pass media for quick playback, or --config for a TOML playlist."
            )
        from . import quickcast

        cfg = quickcast.build_config(args)
        loaded = cfgmod.LoadResult(
            cfgs=[cfg],
            names=["cast"],
            paths=[None],
            is_ensemble=False,
            master_control=cfg.control,
            master_midi_control=cfg.midi_control,
        )
        return loaded, [cfg]

    loaded = cfgmod.load_master(args.config)

    # CLI flags apply to every per-system config. In ensemble mode reject the
    # flags that pick one system's hardware — `[ultimate64].url` (the -u target)
    # and `[video].device` are per-system identity and must come from the TOMLs.
    if loaded.is_ensemble:
        offending = [
            flag for dest, flag in _PER_SYSTEM_CLI_FLAGS if getattr(args, dest, None) is not None
        ]
        if offending:
            raise cfgmod.ConfigError(
                f"ensemble mode (`[ensemble]` in {args.config}) is incompatible "
                f"with per-system CLI flags: {', '.join(offending)}. Move these "
                "values into the per-system TOMLs."
            )

    cfgs = [cfgmod.merge_cli(c, args) for c in loaded.cfgs]
    # Scheme-aware connection target overrides the single system's connection
    # fields (env honored as a fallback). Ensemble systems keep their TOML
    # identity — the per-system-flag guard above already rejected a CLI target.
    if not loaded.is_ensemble:
        target = args.url or os.environ.get("C64CAST_URL")
        if target:
            from .connect import apply_to_config, parse_connection_uri

            apply_to_config(cfgs[0], parse_connection_uri(target))
    return loaded, cfgs


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_devices:
        # Logging at default level; list-devices skips config load entirely.
        configure_logging(args.verbose or 0, args.log_file)
        return list_devices()

    # Introspection commands describe the config surface itself — no config
    # file, no hardware. Dispatch before load_master so they work anywhere.
    intro_rc = run_introspection(args)
    if intro_rc is not None:
        return intro_rc

    try:
        loaded, cfgs = _resolve_configs(args)
    except cfgmod.ConfigError as e:
        # Logging may not be set up yet (verbose/log_file live in [debug]).
        # Set up a minimal default handler so the error reaches the user
        # whether or not they passed -v.
        configure_logging(args.verbose or 0, args.log_file)
        log.error("%s", e)
        return 5
    except (_CliUsageError, ValueError, RuntimeError) as e:
        configure_logging(args.verbose or 0, args.log_file)
        log.error("%s", e)
        return 2
    # Logging is process-wide; use the first stack's debug settings (they
    # already share defaults via the master cascade unless explicitly
    # overridden).
    configure_logging(cfgs[0].debug.verbose, cfgs[0].debug.log_file)

    # Quick-playback feedback: warn when pointing at the built-in default
    # (no target given), and log where + which backend we resolved.
    if args.inputs:
        if not (args.url or os.environ.get("C64CAST_URL")):
            log.warning(
                "no connection target given (-u/--url or C64CAST_URL) — using "
                "the built-in default %s. Point at your hardware with e.g. "
                "-u u64://192.168.2.64 or -u tr://.",
                cfgs[0].ultimate64.url,
            )
        log.info(
            "cast: %d scene(s) on the %s backend",
            len(cfgs[0].scenes),
            cfgs[0].hardware.backend,
        )

    if args.calibrate_dac:
        # Measure the connected SID's Mahoney $D418 transfer curve + persist a
        # per-system calibrated table, then exit. Single-system operation.
        cfg = cfgs[0]
        if len(cfgs) > 1:
            log.warning(
                "--calibrate-dac operates on one system; calibrating the first (%s)",
                loaded.names[0],
            )
        if not AUDIO_AVAILABLE:
            log.error(
                "--calibrate-dac needs audio capture (sounddevice). Install the "
                "'mic' extra: uv sync --extra mic"
            )
            return 3
        dev = (
            args.audio_device if args.audio_device is not None and args.audio_device >= 0 else None
        )
        be = make_backend(cfg)
        try:
            dac_calibration.run_calibration(be, cfg, device=dev, log_fn=lambda m: log.info("%s", m))
        except dac_calibration.CaptureUnavailableError as e:
            log.error("%s", e)
            return 3
        finally:
            be.close()
        return 0

    if args.doctor:
        # Doctor uses the merged configs so CLI flags (e.g. --skip-probe) and
        # the C64CAST_DMA_PASSWORD env var take effect on the probe.
        from .doctor import print_report, validate_load_result

        merged = cfgmod.LoadResult(
            cfgs=cfgs,
            names=loaded.names,
            paths=loaded.paths,
            is_ensemble=loaded.is_ensemble,
            master_control=loaded.master_control,
            master_midi_control=loaded.master_midi_control,
        )
        diagnostics = validate_load_result(merged, probe_u64=not cfgs[0].debug.skip_probe)
        return print_report(diagnostics)

    for cfg in cfgs:
        if cfg.audio.enabled and not AUDIO_AVAILABLE:
            log.error(
                "audio enabled but sounddevice is not installed. Install "
                "with `uv sync --extra mic` (or `pip install c64cast[mic]`), "
                "or set [audio].enabled = false in your "
                "config. Aborting so you don't run with broken audio for "
                "the whole session."
            )
            return 3
        # Reject a sample rate that would overrun the NMI DAC handler on the
        # target system (broken/pitch-dropped audio) before the playlist runs.
        try:
            cfgmod.validate_nmi_sample_rate(cfg)
            cfgmod.validate_sampler_cfg(cfg)
            cfgmod.validate_dac_curve_cfg(cfg)
            cfgmod.validate_dac_bitmap_tempo_cfg(cfg)
        except cfgmod.ConfigError as e:
            log.error("%s", e)
            return 5

    # Install the profiler (or NullProfiler if disabled) before constructing
    # the Playlists so the module-global accessor is correct for the first
    # frame's sub-stage timings inside _render_with_overlays. The profiler
    # is process-wide today (per-scene timings will mix across systems in
    # ensemble mode — a future enhancement could split it per-system).
    if cfgs[0].debug.profile:
        profiler: FrameProfiler | NullProfiler = FrameProfiler(
            interval=cfgs[0].debug.profile_interval
        )
        log.info("profiler enabled (interval %.1fs)", cfgs[0].debug.profile_interval)
    else:
        profiler = NullProfiler()
    set_profiler(profiler)

    # Allocate the Ensemble first (multi-system only) so each stack's
    # Playlist receives the shared stop_event at construction time.
    if loaded.is_ensemble:
        ensemble: Ensemble | None = Ensemble(stacks=[], stop_event=threading.Event())
        stop_event = ensemble.stop_event
    else:
        ensemble = None
        stop_event = threading.Event()

    stacks: list[SystemStack] = []
    try:
        for cfg, name, sub_path in zip(cfgs, loaded.names, loaded.paths, strict=True):
            stacks.append(
                build_stack(
                    cfg,
                    name,
                    args,
                    stop_event=stop_event,
                    profiler=profiler,
                    is_ensemble=loaded.is_ensemble,
                    config_path=sub_path,
                )
            )
    except StackBuildError as e:
        # Tear down whatever we did manage to build before bailing.
        for st in reversed(stacks):
            teardown_stack(st)
        return e.exit_code

    if ensemble is not None:
        ensemble.stacks = stacks
        ensemble.populate_broadcast_events()
        # Per-stack ensemble plumbing: wire the playlist to its ensemble,
        # its broadcast events, and a follower-scene factory that closes
        # over the stack's api/audio/source/cfg (the playlist can't build
        # follower scenes itself without those references). The factory
        # captures via lambda default-arg to avoid the late-binding loop
        # bug.
        for st, cfg in zip(stacks, cfgs, strict=True):
            st.playlist.ensemble = ensemble
            st.playlist._broadcast_interrupt = ensemble.broadcast_interrupt[st.name]
            st.playlist._broadcast_resume = ensemble.broadcast_resume[st.name]
            st.playlist.build_follower_scene = lambda scene_cfg, _st=st, _cfg=cfg: (
                cfgmod.build_scene(
                    scene_cfg,
                    _cfg,
                    _st.api,
                    _st.audio,
                    _st.source,
                    is_ensemble=True,
                    reu_available=_st.reu_available,
                    sampler_available=_st.sampler_available,
                )
            )

    control_server = None
    midi_control_listener = None

    # SIGTERM → graceful shutdown. SIGINT continues to raise KeyboardInterrupt
    # via the default handler so the user can still Ctrl+C interactively.
    # SIGHUP → reload TOML config (only the [interstitial] + [playlist] +
    # [[scenes]] sections take effect; [audio], [video], [ultimate64] are
    # set at startup and reloading them would require restarting threads).
    def _on_sigterm(_signum, _frame):
        log.info("SIGTERM received; stopping")
        stop_event.set()

    def _on_sighup(_signum, _frame):
        log.info("SIGHUP received; reloading config for %d system(s)", len(stacks))
        # Each per-system TOML reloads independently from the path it was
        # originally loaded from. The master itself isn't re-read (system
        # list + master defaults are set at startup); add/remove of systems
        # requires a restart. Single-system mode just reloads args.config.
        for st, sub_path in zip(stacks, loaded.paths, strict=True):
            if sub_path is None:
                continue  # no file to reload (defaults-only single-system)
            try:
                new_cfg = cfgmod.load(sub_path)
                new_cfg = cfgmod.merge_cli(new_cfg, args)
                new_scenes = cfgmod.scenes_from_config(
                    new_cfg,
                    st.api,
                    st.audio,
                    st.source,
                    is_ensemble=loaded.is_ensemble,
                    reu_available=st.reu_available,
                    sampler_available=st.sampler_available,
                )
                new_factory = interstitial_factory(st.api, new_cfg.interstitial)
                st.playlist.request_reload(new_scenes, new_factory)
            except cfgmod.ConfigError as e:
                log.error("[%s] SIGHUP reload failed; keeping current playlist. %s", st.name, e)
            except Exception:
                log.exception("[%s] SIGHUP reload failed; keeping current playlist", st.name)

    signal.signal(signal.SIGTERM, _on_sigterm)
    if hasattr(signal, "SIGHUP"):  # Windows lacks SIGHUP
        signal.signal(signal.SIGHUP, _on_sighup)

    try:
        # Optional FastAPI control plane. One server for the whole ensemble;
        # endpoints take ?system=NAME (defaults to all systems in multi
        # mode, to the sole system in single mode).
        control_cfg = loaded.master_control if loaded.is_ensemble else cfgs[0].control
        if control_cfg.enabled:
            try:
                from .control_plane import start_control_server

                # Per-system reload closures. Default-arg `st=st, p=p`
                # captures by value to avoid the late-binding bug where
                # every lambda would see the last loop iteration's st.
                config_loaders = {
                    st.name: (
                        lambda st=st, p=p: cfgmod.scenes_from_config(
                            cfgmod.merge_cli(cfgmod.load(p), args),
                            st.api,
                            st.audio,
                            st.source,
                            is_ensemble=loaded.is_ensemble,
                            reu_available=st.reu_available,
                            sampler_available=st.sampler_available,
                        )
                    )
                    for st, p in zip(stacks, loaded.paths, strict=True)
                    if p is not None
                }
                interstitial_factories = {
                    st.name: (
                        lambda st=st, p=p: interstitial_factory(st.api, cfgmod.load(p).interstitial)
                    )
                    for st, p in zip(stacks, loaded.paths, strict=True)
                    if p is not None
                }
                control_server = start_control_server(
                    control_cfg.host,
                    control_cfg.port,
                    playlists={st.name: st.playlist for st in stacks},
                    config_loaders=config_loaders,
                    interstitial_factories=interstitial_factories,
                )
            except RuntimeError as e:
                log.error("control plane disabled: %s", e)

        # Optional MIDI control surface for live performance. One listener
        # for the whole ensemble (like [control]); MIDI channel selects the
        # target system. See midi_control.py's module docstring for the
        # latency rationale.
        midi_cfg = loaded.master_midi_control if loaded.is_ensemble else cfgs[0].midi_control
        if midi_cfg.enabled:
            try:
                cfgmod.validate_midi_control_cfg(midi_cfg)
                from .midi_control import build_midi_control_listener

                midi_control_listener = build_midi_control_listener(
                    playlists={st.name: st.playlist for st in stacks},
                    cfg=midi_cfg,
                )
                midi_control_listener.start()
            except (cfgmod.ConfigError, RuntimeError, ValueError) as e:
                log.error("MIDI control disabled: %s", e)

        _run_playlists(stacks, stop_event)
    finally:
        # Stop input surfaces before tearing down what they act on — same
        # ordering the keyboard/vision controllers already follow.
        if midi_control_listener is not None:
            try:
                midi_control_listener.stop()
            except Exception:
                log.exception("MIDI control shutdown failed")
        if control_server is not None:
            try:
                control_server.stop()
            except Exception:
                log.exception("control plane shutdown failed")
        for st in reversed(stacks):
            teardown_stack(st)

    for st in stacks:
        log.info("[%s] %s stats: %s", st.name, st.api.profile.name, st.api.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
