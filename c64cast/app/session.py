"""The playlist session: build it, run it, tear it down.

Split out of :mod:`c64cast.app.cli` so a session's lifecycle is callable
independently of the one-shot CLI. ``cli`` composes these five steps in order
and maps their exceptions back to exit codes:

* :func:`validate_configs` — pure, hardware-free config checks.
* :func:`build_session` — open every system's hardware and build its stack.
* :func:`start_services` — the optional control plane / MIDI / WLED surfaces.
* :func:`run_foreground` — run the playlists to completion.
* :func:`teardown_session` — bring it all down, in the order teardown needs.

Signals are deliberately *not* installed here: ``signal.signal`` raises off
the main thread, so a caller that starts a session from a worker must install
its own handlers (see ``cli._run_session``) — but the handler *shape* they
install is shared via :func:`make_stop_signal_handler`.
"""

from __future__ import annotations

import argparse
import logging
import math
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from c64cast.audio import dac_curve_resolve
from c64cast.audio.audio import AUDIO_AVAILABLE, AudioStreamer
from c64cast.control.keyboard import CommodoreKeyPoller
from c64cast.control.vision import MediaPipeHandRecognizer, VisionController
from c64cast.hw import char_rom, hw_provision
from c64cast.hw.api import SocketDMAError
from c64cast.hw.backend import C64Backend, make_backend
from c64cast.hw.teensyrom_dma import TRError
from c64cast.scenes.interstitial import default_factory as interstitial_factory
from c64cast.scenes.scenes import Scene
from c64cast.video.video import WebcamSource

from . import config as cfgmod
from . import paths, scene_factory
from .ensemble import Ensemble, SystemStack
from .orchestrator import OrchestratorError
from .playlist import FollowerSceneFactory, Playlist
from .profiler import FrameProfiler, NullProfiler, set_profiler

if TYPE_CHECKING:
    from c64cast.video.framebuffer import Framebuffer
    from c64cast.video.preview import PreviewWindow, StreamRecorder

log = logging.getLogger("c64cast")


class StackBuildError(Exception):
    """Raised by build_stack when a per-system stack cannot be constructed.
    The user-facing diagnostic has already been logged; this just carries
    the exit code main() should return."""

    def __init__(self, exit_code: int):
        super().__init__(f"stack build failed (exit code {exit_code})")
        self.exit_code = exit_code


class SessionConfigError(Exception):
    """Raised by validate_configs when a config can't be run. Like
    StackBuildError it carries the exit code and its diagnostic is already
    logged — but it is raised before any hardware is touched, which is what
    lets a caller reject a config without disturbing a running session.

    ``detail`` is the same message validate_configs just logged, carried
    along so a caller with no access to the log (the web console) can still
    tell the user what was wrong."""

    def __init__(self, exit_code: int, detail: str = ""):
        super().__init__(detail or f"config validation failed (exit code {exit_code})")
        self.exit_code = exit_code
        self.detail = detail


class _Stoppable(Protocol):
    """The only thing teardown_session needs from the three optional
    process-wide surfaces (control plane, MIDI listener, WLED device).

    They are typed as this rather than ``Any`` because session.py is one of
    the modules ``mypy --strict`` covers: under ``Any`` a rename of ``stop()``
    on any of the three type-checks clean and fails at shutdown, which is the
    one moment where a failure costs the run its final reset."""

    def stop(self) -> None: ...


@dataclass
class Session:
    """One playlist session: every system's stack plus the process-wide
    surfaces that span them. Built by build_session, run by run_foreground,
    brought down by teardown_session."""

    args: argparse.Namespace
    loaded: cfgmod.LoadResult
    cfgs: list[cfgmod.Config]
    stacks: list[SystemStack]
    ensemble: Ensemble | None
    stop_event: threading.Event
    profiler: FrameProfiler | NullProfiler
    threads: list[threading.Thread] = field(default_factory=list)
    control_server: _Stoppable | None = None
    midi_control_listener: _Stoppable | None = None
    wled_device_server: _Stoppable | None = None
    # True for the one-shot CLI, False for a long-lived host that owns
    # sessions. Gates the surfaces that assume a terminal and a process to
    # themselves: the live-tune input() prompt and the in-session control
    # plane (whose port a host would already be holding).
    interactive: bool = True
    # Monotonic across sessions in one process, so a log line or a state
    # push can say which run it belongs to.
    generation: int = 0


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
    log.error("  1. Menu -> F2 -> Network Settings -> Ultimate DMA Service -> Enabled")
    log.error("  2. Menu -> F2 -> Memory Configuration -> Command Interface -> Enabled")
    log.error(
        "     (both toggles must be on; the second one gates command "
        "dispatch even when the listening socket is open)"
    )
    log.error(
        "  3. If a network password is set on the U64, supply it via the "
        "C64CAST_DMA_PASSWORD env var or [ultimate64] dma_password."
    )
    log.error("Save and reboot the U64 after changing either toggle.")


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

    enabled = hw_provision.reu_is_enabled(api)
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

    avail = hw_provision.sampler_is_available(api)
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


def _coerce_reu_for_transport(cfg: cfgmod.Config, midi_cfg: cfgmod.MidiControlCfg) -> None:
    """Disable [audio].use_reu_pump on `cfg` when `midi_cfg` has any
    transport.* action mapped (MIDI live-tune Phase 2 — DJ-style seek/pause/
    loop control of a playing video).

    `midi_cfg` is passed explicitly rather than read off `cfg.midi_control`
    because [midi_control] is process-wide, not per-system-cascaded (like
    [control] — see validate_midi_control_cfg's docstring): in ensemble mode
    the config that actually drives the listener is `loaded.master_midi_control`,
    not any per-system `cfg.midi_control`. The caller resolves the right one
    (mirrors the `loaded.master_midi_control if loaded.is_ensemble else
    cfgs[0].midi_control` pattern used to build the real listener).

    The REU-pump audio path pre-decodes a video's entire soundtrack up front
    and streams it from a C64-side ring on its own clock, independent of
    which video frame is currently displayed — it has no notion of "splice
    to a new position." The transport escape valve (VideoScene._touch_transport
    -> AVFileSource.set_muted) only reaches the host-DMA audio path (it drops
    packets AVFileSource would otherwise emit), so a REU-pumped soundtrack
    would keep playing on its own untouched timeline while the video jumps
    around on a seek/loop/pause. Coerce it off (in place) so the host-DMA NMI
    DAC path is used instead — same "force off + log" shape as
    _coerce_reu_for_backend, but for an incompatible *feature combination*
    rather than a missing capability.

    Must run before the shared AudioStreamer is constructed (see
    build_stack) — use_reu_pump is a constructor arg baked into that instance,
    not something a later per-scene build_scene call can retroactively
    change. [video].use_reu_staged (the REU bank-swap BITMAP push) is a
    separate, orthogonal mechanism — it only affects how the current frame
    reaches C64 memory, not which frame is current, so it's unaffected by
    transport and left as configured."""
    if not midi_cfg.enabled:
        return
    if not any(
        isinstance(entry, dict) and str(entry.get("action", "")).startswith("transport.")
        for entry in midi_cfg.cc_map
    ):
        return
    if cfg.audio.use_reu_pump:
        log.info(
            "[audio].use_reu_pump is incompatible with [midi_control] transport.* "
            "actions (no seek/splice support) — using the host-DMA NMI DAC "
            "path instead"
        )
        cfg.audio.use_reu_pump = False


def _open_backend(cfg: cfgmod.Config, name: str) -> C64Backend:
    """Connect the hardware backend and (unless --skip-probe) verify it is
    reachable. Raises StackBuildError with the exit code build_stack's caller
    expects. It releases only what it opened itself (the backend, when the
    probe fails); everything build_stack had already acquired comes down
    through build_stack's own unwind ladder."""
    try:
        api = make_backend(cfg)
    except SocketDMAError as e:
        _log_dma_setup_error(cfg, e, role="render")
        raise StackBuildError(4) from e
    except TRError as e:
        log.error(
            "TeensyROM connect failed (%s): %s. Check the cable / "
            "serial port (transport=serial) or 'Enable TCP Listener' "
            "+ host (transport=tcp).",
            name,
            e,
        )
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
            raise StackBuildError(2)
        log.info("%s reachable: %s", cfg.hardware.backend, status)
        if identity := api.describe_device():
            log.info("connected device: %s", identity)
        # Reachability just proved; one cheap REST call downgrades capability
        # flags the family profile claims optimistically (U2+: no multi-SID
        # config surface). Under --skip-probe the flags stay optimistic and
        # the per-call error handling absorbs any missing surface, as before.
        api.refine_capabilities()
    hw_provision.resolve_system(cfg, api)
    # Before anything renders: every color decision in the run is a distance
    # measured against these 16.
    hw_provision.resolve_palette(cfg, api)
    return api


def _build_audio(cfg: cfgmod.Config, api: C64Backend) -> AudioStreamer | None:
    """The shared $D418 DAC streamer, or None with audio disabled. Resolves
    the system-aware [audio].dac_curve ("auto"/"calibrated") to a concrete
    (label, table) for this backend + any per-unit calibration first."""
    dac_curve_label, dac_table = dac_curve_resolve.resolve_dac_curve_for_backend(cfg, be=api)
    if cfg.audio.enabled and dac_curve_label != cfg.audio.dac_curve:
        log.info("audio: dac_curve %s → %s", cfg.audio.dac_curve, dac_curve_label)
    if not cfg.audio.enabled:
        return None
    return AudioStreamer(
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


def _build_input_controls(
    cfg: cfgmod.Config, api: C64Backend, source: WebcamSource | None, name: str
) -> tuple[CommodoreKeyPoller | None, VisionController | None]:
    """The two physical control surfaces: the Commodore-key poller (needs a
    read-capable backend) and the optional webcam gesture controller."""
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
        assert source is not None  # needs_camera guaranteed it in build_stack
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
    return key_poller, vision_controller


def _detach_framebuffer(api: C64Backend, framebuffer: Framebuffer | None) -> None:
    """Stop shadowing DMA writes into ``framebuffer``. A no-op when there is
    none, and idempotent, so both the build-failure path and teardown_stack
    can call it."""
    if framebuffer is not None:
        api.remove_write_listener(framebuffer.on_write)


def _build_preview_and_recording(
    cfg: cfgmod.Config, api: C64Backend, name: str, *, is_ensemble: bool
) -> tuple[Framebuffer | None, PreviewWindow | None, StreamRecorder | None]:
    """Optional local preview window + stream recorder. Both share a
    Framebuffer that shadows U64 memory writes via api listeners.

    A framebuffer with no surviving consumer is detached again before
    returning: the write listener costs a shadow-memory update on every DMA
    write for the rest of the run, and nothing else in the tree reads it."""
    framebuffer: Framebuffer | None = None
    preview_window: PreviewWindow | None = None
    recorder: StreamRecorder | None = None
    if cfg.preview.enabled or cfg.recording.enabled:
        from c64cast.video.framebuffer import Framebuffer as _FB

        framebuffer = _FB(charset_path=cfg.preview.charset_path)
        api.add_write_listener(framebuffer.on_write)
    if cfg.preview.enabled:
        assert framebuffer is not None
        from c64cast.video.preview import PreviewWindow as _PW

        # Constructed here but not opened: the window has to be created and
        # serviced on the main thread (see preview.py), which happens in
        # _pump_previews_until_done once the playlist threads are running.
        # HighGUI keys windows by title, so an ensemble needs one title per
        # system to get one window per system rather than N systems fighting
        # over a single window.
        preview_window = _PW(
            framebuffer,
            fps=cfg.preview.fps,
            scale=cfg.preview.scale,
            title=f"c64cast preview - {name}" if is_ensemble else "c64cast preview",
        )
    if cfg.recording.enabled:
        assert framebuffer is not None
        try:
            from c64cast.video.preview import StreamRecorder as _SR

            recorder = _SR(
                framebuffer,
                paths.expand_user(
                    cfgmod.resolve_recording_path(cfg.recording, name, is_ensemble=is_ensemble)
                ),
                fps=cfg.recording.fps,
                scale=cfg.recording.scale,
                fourcc=cfg.recording.fourcc,
            )
            recorder.start()
        except RuntimeError as e:
            log.error("recording disabled: %s", e)
    if preview_window is None and recorder is None:
        _detach_framebuffer(api, framebuffer)
        framebuffer = None
    return framebuffer, preview_window, recorder


def _release_step(name: str, label: str, fn: Callable[[], object]) -> None:
    """Run one release step, logging and swallowing a failure so it can't
    strand the steps under it. Shared by teardown_stack and by build_stack's
    failure unwind, which releases the same resources in the same way."""
    try:
        fn()
    except Exception:
        log.exception("[%s] %s failed", name, label)


def build_stack(
    cfg: cfgmod.Config,
    name: str,
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

    Every resource is registered on one unwind ladder as it is acquired, so
    a failure anywhere in the build releases exactly what came up — in
    reverse of acquisition, one guarded step each, the way teardown_stack
    releases them on the success path. On success the ladder is popped and
    the returned SystemStack owns them instead."""
    with ExitStack() as unwind:
        stack = _acquire_stack(
            unwind,
            cfg,
            name,
            stop_event=stop_event,
            profiler=profiler,
            is_ensemble=is_ensemble,
            config_path=config_path,
        )
        unwind.pop_all()
    return stack


def _acquire_stack(
    unwind: ExitStack,
    cfg: cfgmod.Config,
    name: str,
    *,
    stop_event: threading.Event,
    profiler: FrameProfiler | NullProfiler,
    is_ensemble: bool,
    config_path: str | None,
) -> SystemStack:
    """build_stack's body, with every acquisition registered on ``unwind``.

    Split out so the ladder can be a `with` block one level up: this half
    only has to remember to register what it opens, and nothing has to
    remember the release order.

    `is_ensemble=True` propagates into `scenes_from_config` so live
    scenes (webcam, blank) are built with audio suppressed — the
    ensemble audio lock arbitrates which system drives the SID."""

    def release_on_failure(label: str, fn: Callable[[], object]) -> None:
        unwind.callback(_release_step, name, label, fn)

    # Only open the camera when a scene actually needs it. Skipping the open
    # otherwise means a "blank" or "waveform"-only playlist won't fail on a
    # box without a webcam (or one whose OS-level camera permission is denied,
    # which is the typical macOS first-run snag in IDE-launched runs).
    # The shared camera broker feeds both webcam scenes and the (always-on)
    # vision controller, so open it if either wants it.
    # A [[performance.clips]] table counts too: `type` defaults to "webcam"
    # there, so a clip grid can hold webcam clips with no webcam [[scenes]]
    # entry at all — and the clip build factory below closes over `source`,
    # so a None there means that pad raises at launch and dies silently in
    # PerformanceSession's background build.
    needs_webcam = any(s.type == "webcam" for s in cfg.scenes) or any(
        cfgmod.clip_scene_type(c) == "webcam" for c in cfg.performance.clips
    )
    needs_camera = needs_webcam or cfg.vision.enabled
    source: WebcamSource | None = None
    if needs_camera:
        try:
            source = WebcamSource(cfg.video.device)
        except RuntimeError as e:
            log.error("%s", e)
            raise StackBuildError(1) from e
        release_on_failure("camera release", source.release)
    else:
        log.debug("no webcam or vision scenes — skipping video device init")

    api = _open_backend(cfg, name)
    release_on_failure("API close", api.close)

    # Drop REU-staged opt-ins on a backend with no REU, before the AudioStreamer
    # + scenes are built (so the host-DMA paths are used instead).
    _coerce_reu_for_backend(cfg, api)

    # Auto-provision the U64 REU (enable + size to 16 MB, live + volatile) for
    # runs that hard-require it, so the REU-staged audio/video paths "just work"
    # without the manual F2 enable step. No-op unless [ultimate64].auto_reu is
    # on, the backend has an REU, a probe is allowed, and the config hard-needs
    # the REU (see hw_provision.provision_reu). Runs BEFORE _resolve_reu_available
    # so that probe sees the now-enabled REU; restored at teardown (teardown_stack).
    reu_restore = hw_provision.provision_reu(api, cfg)
    release_on_failure("REU restore", lambda: hw_provision.restore_reu(api, reu_restore))
    # Auto-enable the Ultimate Audio sampler (map $DF20 + unmute Sampler mixer,
    # live + volatile) when a video scene will use it. Runs BEFORE
    # _resolve_sampler_available so the probe sees it on; restored at teardown.
    sampler_restore = hw_provision.provision_sampler(api, cfg)
    release_on_failure(
        "sampler restore", lambda: hw_provision.restore_sampler(api, sampler_restore)
    )
    # Video output: the opt-in System Mode retime ([ultimate64].sid_video_mode,
    # which fixes SID PITCH) plus the HDMI upscaler that keeps capture working
    # across it ([ultimate64].hdmi_scan_resolution). Resolved once per run —
    # every switch changes the HDMI output mode and costs the capture device a
    # re-lock. The C64 reset that follows makes the KERNAL re-run its PAL/NTSC
    # autodetect against the new timing; it also has to happen before any scene
    # has painted, which is why this sits here.
    video_output_restore = hw_provision.provision_video_output(api, cfg)
    release_on_failure(
        "video output restore",
        lambda: hw_provision.restore_video_output(api, video_output_restore),
    )
    if video_output_restore is not None and api.profile.supports_reset:
        api.reset()

    audio = _build_audio(cfg, api)
    if audio is not None:
        release_on_failure("audio shutdown", audio.close)

    reu_available = _resolve_reu_available(cfg, api)
    sampler_available = _resolve_sampler_available(cfg, api)
    try:
        playlist_scenes = scene_factory.scenes_from_config(
            cfg,
            api,
            audio,
            source,
            is_ensemble=is_ensemble,
            reu_available=reu_available,
            sampler_available=sampler_available,
        )
    except (ValueError, RuntimeError) as e:
        log.error("%s", e)
        raise StackBuildError(3) from e

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

    # First run against this machine: read its character ROM so C64 text
    # renders in the real C64 font. Here because the machine is idle and
    # nothing has painted yet — the Ultimate's dump soft-resets and puts the
    # clear loop back itself. Best-effort and never fatal; a no-op once cached.
    char_rom.ensure_installed(api, cfg)

    api.disable_case_switch()

    key_poller, vision_controller = _build_input_controls(cfg, api, source, name)
    if vision_controller is not None:
        release_on_failure("vision controller stop", vision_controller.stop)

    framebuffer, preview_window, recorder = _build_preview_and_recording(
        cfg, api, name, is_ensemble=is_ensemble
    )
    release_on_failure("framebuffer detach", lambda: _detach_framebuffer(api, framebuffer))
    if recorder is not None:
        release_on_failure("recording stop", recorder.stop)
    if preview_window is not None:
        release_on_failure("preview shutdown", preview_window.close)

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
        performance=cfg.performance,
    )

    # Clip-launch build factory (Live-performance Phase 2): turn a
    # [[performance.clips]] dict into a Scene, closing over this stack's
    # api/audio/source/cfg (the playlist can't build scenes itself), mirroring
    # the ensemble `build_follower_scene` wiring. The PerformanceSession calls
    # this on a background thread during the count-in; setup() runs later on the
    # playlist thread at the swap.
    playlist.build_performance_scene = _performance_scene_factory(
        cfg,
        api,
        audio,
        source,
        reu_available=reu_available,
        sampler_available=sampler_available,
        is_ensemble=is_ensemble,
    )

    # Vision performance mode (Live DJ/VJ Phase 6): route hand gestures to the
    # clip-launch grid instead of transport. Bound after the playlist exists so
    # the controller can reach pl.performance / pl.toggle_effect_layer.
    if vision_controller is not None and cfg.vision.performance:
        vision_controller.bind_performance(playlist)
        log.info("%s: vision gestures routed to performance grid", name)

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
        video_output_restore=video_output_restore,
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
    steps: tuple[tuple[str, Callable[[], object]], ...] = (
        (
            "preview shutdown",
            lambda: stack.preview_window.close() if stack.preview_window else None,
        ),
        ("recording stop", lambda: stack.recorder.stop() if stack.recorder else None),
        # Both consumers are down, so stop shadowing DMA writes into the
        # framebuffer — every write for the rest of the process would
        # otherwise still pay for a buffer nothing reads.
        (
            "framebuffer detach",
            lambda: _detach_framebuffer(stack.api, stack.framebuffer),
        ),
        ("audio shutdown", lambda: stack.audio.close() if stack.audio else None),
        (
            "vision controller stop",
            lambda: stack.vision_controller.stop() if stack.vision_controller else None,
        ),
        # Restore any REU config we auto-provisioned, while the REST session is
        # still open (no-op when nothing was changed; volatile regardless).
        ("REU restore", lambda: hw_provision.restore_reu(stack.api, stack.reu_restore)),
        # Same for the Ultimate Audio sampler map/mixer auto-provisioning.
        ("sampler restore", lambda: hw_provision.restore_sampler(stack.api, stack.sampler_restore)),
        # Same for an opt-in System Mode / scan-resolution switch. Before the
        # reset below, so the KERNAL re-autodetects against the restored timing.
        (
            "video output restore",
            lambda: hw_provision.restore_video_output(stack.api, stack.video_output_restore),
        ),
        ("U64 reset", stack.api.reset),
        ("API close", stack.api.close),
        ("camera release", lambda: stack.source.release() if stack.source else None),
    )
    for label, fn in steps:
        _release_step(stack.name, label, fn)


# How long the headless join parks per poll. Short enough that Ctrl+C feels
# immediate, long enough not to spin (see pump_until_done on why it polls).
_JOIN_POLL_S = 0.2

# How long a playlist thread gets to drain once it has been asked to stop,
# before it is logged and abandoned. Shared by join_playlists and by
# teardown_session's own pre-teardown drain so the stop path has one budget.
_STOP_JOIN_S = 5.0


def join_bounded(t: threading.Thread, timeout: float, poll_s: float = _JOIN_POLL_S) -> bool:
    """Join ``t`` for at most ``timeout``, polling so the main thread keeps
    returning to the interpreter. Returns whether it finished in time.

    A single long ``join(timeout)`` would not: CPython parks it in
    ``_PyParkingLot_Park``, where no signal handler runs (see
    pump_until_done). Every non-daemon join in this project shares this
    helper so that measurement — and the abandonment log, on timeout — only
    has to be made once."""
    deadline = time.monotonic() + timeout
    while t.is_alive() and time.monotonic() < deadline:
        t.join(timeout=min(poll_s, max(0.0, deadline - time.monotonic())))
    finished = not t.is_alive()
    if not finished:
        log.error("[%s] did not exit within %.0fs; abandoning", t.name, timeout)
    return finished


def make_stop_signal_handler(
    on_first_signal: Callable[[], None], *, verb: str
) -> Callable[[int, Any], None]:
    """Build the three-strike SIGINT/SIGTERM handler shared by the CLI
    (``cli._run_session``) and the daemon (``serve.run_daemon``).

    First signal: log and call ``on_first_signal`` (set a stop flag, so an
    in-flight DMA finishes rather than being cut — killing mid-DMA is what
    wedges the hardware into needing a power cycle). Second signal for the
    same signum: restore its default disposition, so a third — or a repeated
    SIGTERM from a service manager — actually kills instead of being caught
    forever. ``verb`` is the caller-specific tail of the first-signal log
    line (e.g. "stopping", "shutting down the host").

    Escalation is tracked per signum, as the contract above says: one shared
    flag meant the *first* SIGTERM after a SIGINT took the escalation branch,
    arming SIG_DFL a signal earlier than documented and dropping that
    SIGTERM's stop request on the floor.

    Building the closure here doesn't install it: ``signal.signal`` raises
    off the main thread, so the caller still does that call itself."""
    seen: set[int] = set()

    def handler(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        if signum in seen:
            log.warning("%s again; next one exits immediately (teardown may not finish)", name)
            signal.signal(signum, signal.SIG_DFL)
            return
        seen.add(signum)
        log.info("%s received; %s", name, verb)
        on_first_signal()

    return handler


def _pump_previews_until_done(
    threads: Sequence[threading.Thread], previews: Sequence[PreviewWindow]
) -> None:
    """Drive the preview window(s) from the main thread until every playlist
    thread has finished.

    The windows can only live here: HighGUI must create and service a window
    on the main thread (a hard requirement on macOS), and with every playlist
    on a worker thread the main thread is otherwise just parked in `join()`.
    `pump()` blocks ~1 ms in `waitKey` servicing events, which paces this loop
    without a busy-spin.

    Closing the last window doesn't stop the show — we fall through to the
    same polling join the headless branch uses and playback carries on
    without a window. That fall-through is reached on three ordinary paths:
    the operator closes the window, a draw failure disables it, and — on the
    very first iteration — a headless opencv build, where `open()` logs
    "preview disabled" and never sets `is_open`. It has to be `join_bounded`
    for the reason pump_until_done spells out: a bare `join()` parks the main
    thread where no signal handler can run, and on the CLI path SIGINT and
    SIGTERM only *set* stop_event, so a handler that never runs is a run that
    nothing but SIGKILL can end.
    """
    for p in previews:
        p.open()
    while any(t.is_alive() for t in threads):
        for p in previews:
            p.pump()
        if not any(p.is_open for p in previews):
            break
    for t in threads:
        join_bounded(t, math.inf)


def start_playlists(
    stacks: list[SystemStack], into: list[threading.Thread] | None = None
) -> list[threading.Thread]:
    """Start one non-daemon worker thread per stack and return them.

    Non-daemon on purpose: a daemon thread is killed at interpreter exit,
    which can cut an in-flight DMA and wedge the machine into needing a
    power cycle. The stop path below would rather log a stuck thread.

    ``into`` is the caller's own list (``sess.threads``), appended to as each
    thread starts. It matters on the failure path: if the k-th ``start()``
    raises, the k-1 threads already DMAing are still reachable, where a
    returned list would have been lost with the exception and teardown would
    have run underneath live workers."""
    threads = [] if into is None else into
    for s in stacks:
        t = threading.Thread(target=s.playlist.run, name=f"playlist-{s.name}", daemon=False)
        t.start()
        threads.append(t)
    return threads


def pump_until_done(threads: list[threading.Thread], stacks: list[SystemStack]) -> None:
    """Block until every playlist thread has finished, pumping any preview
    windows on the way (see _pump_previews_until_done).

    The headless join polls rather than blocking outright. CPython 3.14 parks
    `Thread.join()` in `_PyParkingLot_Park`, which no signal interrupts, so the
    main thread never returns to the interpreter and Python never gets to run a
    signal handler — measured on a hung run, where two SIGINTs produced no
    KeyboardInterrupt, no shutdown and no final reset, leaving the machine mid
    session. (SIGTERM was equally stuck, so there was no graceful way out at
    all.) Only the preview path escaped it, because pumping a window polls
    `is_alive()` anyway, which is what made Ctrl+C look intermittent."""
    previews = [s.preview_window for s in stacks if s.preview_window is not None]
    if previews:
        _pump_previews_until_done(threads, previews)
    else:
        for t in threads:
            join_bounded(t, math.inf)


def join_playlists(
    threads: list[threading.Thread], stacks: list[SystemStack], stop_event: threading.Event
) -> None:
    """Ask every playlist to stop and wait for it. Setting stop_event means an
    in-flight DMA finishes rather than being cut; each thread gets up to 5s to
    drain before we move on and log it as stuck."""
    log.info("interrupted; stopping %d system(s)", len(stacks))
    stop_event.set()
    for t in threads:
        join_bounded(t, _STOP_JOIN_S)


def _maybe_save_live_tune(stacks: list[SystemStack], overwrite: bool) -> None:
    """Persist live-tune parameter changes on exit (run after teardown, when the
    terminal is free).

    For each system whose playlist recorded changes (a MIDI/WLED knob sweep or a
    mode change during the run): with `overwrite`, silently apply them to the
    config — [color] for the knobs a whole show shares, the scene's own
    [[scenes]] block for the ones a scene owns — and save (keeping a .bak);
    otherwise, on an interactive terminal, prompt with a plain input() (works
    without the wizard extra). A quick-playback run (no config file) can't be
    written back — print a pasteable TOML snippet instead. Runs on a normal exit
    and on Ctrl+C."""
    for st in stacks:
        pl = st.playlist
        tracker = pl.live_tracker
        if not tracker.has_changes():
            continue
        tag = f"[{st.name}] " if len(stacks) > 1 else ""
        if pl.config is not None and pl.config_path:
            if overwrite:
                applied = tracker.apply(pl.config)
                if pl.menu.save_config():
                    # No "(backup .bak)" here: save_config's .bak is one-shot
                    # (it preserves the hand-written original, not the previous
                    # save), and this path fires on every exit under
                    # --overwrite — so repeating the reassurance every run is
                    # exactly the claim that was misleading. save_config logs
                    # what actually happened to the backup.
                    log.info(
                        "%slive-tune: saved %d change(s) → %s",
                        tag,
                        len(applied),
                        pl.config_path,
                    )
                continue
            print(f"\n{tag}Live-tune changes this run:")
            for line in tracker.describe():
                print(f"  {line}")
            if not sys.stdin.isatty():
                # Headless/piped: can't prompt. Don't lose the info silently.
                print(f"{tag}Not saved (no interactive terminal; re-run with --overwrite to save).")
                continue
            try:
                ans = input(f"{tag}Save these to {pl.config_path}? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans in ("y", "yes"):
                applied = tracker.apply(pl.config)
                ok = pl.menu.save_config()
                print(
                    f"{tag}Saved {len(applied)} change(s)."
                    if ok
                    else f"{tag}Save failed (see log)."
                )
            else:
                print(f"{tag}Not saved.")
        else:
            snippet = tracker.toml_snippet()
            if snippet:
                print(
                    f"\n{tag}Live-tune changes (no config file — paste these into a "
                    f"config to keep them):\n{snippet}"
                )


def validate_configs(loaded: cfgmod.LoadResult, cfgs: list[cfgmod.Config]) -> None:
    """Check every per-system config and every scene in it, and coerce the
    settings that a feature combination rules out, before anything opens
    hardware.

    Pure and hardware-free by construction: a caller that owns a running
    session can reject a bad config without disturbing it. Raises
    SessionConfigError after logging the user-facing diagnostic."""
    # [midi_control] is process-wide (see _coerce_reu_for_transport's
    # docstring), so resolve the one MidiControlCfg that actually drives the
    # listener once, before the per-cfg loop below applies it to each
    # system's audio settings.
    midi_cfg = loaded.master_midi_control if loaded.is_ensemble else cfgs[0].midi_control
    # [control] is process-wide too, and `loaded.master_control` is the very
    # section start_services binds — so check it once here, before hardware,
    # rather than at bind time when a show is already up.
    try:
        scene_factory.validate_control_cfg(loaded.master_control)
    except cfgmod.ConfigError as e:
        log.error("%s", e)
        raise SessionConfigError(5, str(e)) from e
    for cfg in cfgs:
        # Must run before build_stack constructs this system's AudioStreamer
        # (see _coerce_reu_for_transport).
        _coerce_reu_for_transport(cfg, midi_cfg)
        if cfg.audio.enabled and not AUDIO_AVAILABLE:
            detail = (
                "audio enabled but sounddevice is not installed. Install the "
                "'mic' extra (`uv tool install --force 'c64cast[all]'`), "
                "or set [audio].enabled = false in your "
                "config. Aborting so you don't run with broken audio for "
                "the whole session."
            )
            log.error(detail)
            raise SessionConfigError(3, detail)
        # Every whole-Config validator scene_factory owns, in one pass — a
        # rejected NMI sample rate that would overrun the DAC handler, a bad
        # [wled] endpoint, and the rest. The tuple lives next to the
        # validators so this list can't fall behind the one doctor walks.
        try:
            for validate in scene_factory.PER_SYSTEM_VALIDATORS:
                validate(cfg)
        except cfgmod.ConfigError as e:
            log.error("%s", e)
            raise SessionConfigError(5, str(e)) from e
        # Every scene, including the follower-only ones (built lazily at
        # broadcast time, so without this a bad one surfaces mid-show). Exit
        # code 3 is what `build_stack` already returns when `scenes_from_config`
        # raises the same error a few seconds later — the failure moves ahead of
        # the hardware, it doesn't change identity.
        for idx, s in enumerate(cfg.scenes):
            try:
                scene_factory.validate_scene_cfg(s, cfg, audio_enabled=cfg.audio.enabled)
            except (ValueError, OrchestratorError) as e:
                detail = f"scene {s.name or f'{s.type}#{idx}'}: {e}"
                log.error(detail)
                raise SessionConfigError(3, detail) from e


def _performance_scene_factory(
    cfg: cfgmod.Config,
    api: C64Backend,
    audio: AudioStreamer | None,
    source: WebcamSource | None,
    *,
    reu_available: bool,
    sampler_available: bool,
    is_ensemble: bool,
) -> Callable[[dict[str, Any]], Scene]:
    """Build one stack's clip-launch scene factory, closing over its
    api/audio/source/cfg — the references a Playlist doesn't hold itself.

    A named helper for the same reason _follower_scene_factory is one: the
    closure-capture-by-default-argument lambda this replaces was a
    ~180-character line whose signature no type checker could express."""

    def build(clip: dict[str, Any]) -> Scene:
        return scene_factory.build_scene(
            cfgmod.clip_scene_cfg(clip),
            cfg,
            api,
            audio,
            source,
            is_ensemble=is_ensemble,
            reu_available=reu_available,
            sampler_available=sampler_available,
        )

    return build


def _follower_scene_factory(st: SystemStack, cfg: cfgmod.Config) -> FollowerSceneFactory:
    """Build one stack's follower-scene factory, closing over its
    api/audio/source/cfg — the references a Playlist doesn't hold itself."""

    def build(scene_cfg: cfgmod.SceneCfg) -> Scene:
        return scene_factory.build_scene(
            scene_cfg,
            cfg,
            st.api,
            st.audio,
            st.source,
            is_ensemble=True,
            reu_available=st.reu_available,
            sampler_available=st.sampler_available,
        )

    return build


def build_session(
    args: argparse.Namespace,
    loaded: cfgmod.LoadResult,
    cfgs: list[cfgmod.Config],
    *,
    interactive: bool = True,
    generation: int = 0,
) -> Session:
    """Open every system's hardware and build its stack, wired to the shared
    stop_event (and to the Ensemble, in multi-system mode).

    Call validate_configs first. On a StackBuildError the stacks that did come
    up are torn down in reverse before the error propagates, so a partial
    failure leaves no hardware held."""
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
    ensemble: Ensemble | None = None
    if loaded.is_ensemble:
        ensemble = Ensemble(stacks=[], stop_event=threading.Event())
        stop_event = ensemble.stop_event
    else:
        stop_event = threading.Event()

    stacks: list[SystemStack] = []
    try:
        for cfg, name, sub_path in zip(cfgs, loaded.names, loaded.paths, strict=True):
            stacks.append(
                build_stack(
                    cfg,
                    name,
                    stop_event=stop_event,
                    profiler=profiler,
                    is_ensemble=loaded.is_ensemble,
                    config_path=sub_path,
                )
            )
    except StackBuildError:
        # Tear down whatever we did manage to build before bailing.
        for st in reversed(stacks):
            teardown_stack(st)
        raise

    if ensemble is not None:
        ensemble.stacks = stacks
        ensemble.populate_broadcast_events()
        # Per-stack ensemble plumbing: wire the playlist to its ensemble,
        # its broadcast events, and a follower-scene factory that closes
        # over the stack's api/audio/source/cfg (the playlist can't build
        # follower scenes itself without those references). The factory is
        # built by a helper rather than a loop-body lambda so each one
        # captures its own stack, not the last iteration's.
        for st, cfg in zip(stacks, cfgs, strict=True):
            st.playlist.bind_ensemble(
                ensemble,
                interrupt=ensemble.broadcast_interrupt[st.name],
                resume=ensemble.broadcast_resume[st.name],
                build_follower_scene=_follower_scene_factory(st, cfg),
            )

    return Session(
        args=args,
        loaded=loaded,
        cfgs=cfgs,
        stacks=stacks,
        ensemble=ensemble,
        stop_event=stop_event,
        profiler=profiler,
        interactive=interactive,
        generation=generation,
    )


def reload_registries(sess: Session) -> tuple[dict[str, Any], dict[str, Any]]:
    """The per-system reload closures the control plane's ``POST /reload``
    calls: ``(config_loaders, interstitial_factories)``, keyed by system name.

    A system with no file on disk (defaults-only single-system, quick
    playback) simply isn't in the maps — the route reports that per system
    rather than failing the whole call. Built here rather than inline in
    :func:`start_services` because a long-lived host builds the same two maps
    against whichever session is current, through a provider."""
    args, loaded, stacks = sess.args, sess.loaded, sess.stacks
    # Default-arg `st=st, p=p` captures by value to avoid the late-binding bug
    # where every lambda would see the last loop iteration's st.
    config_loaders = {
        st.name: (
            lambda st=st, p=p: scene_factory.scenes_from_config(
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
        st.name: (lambda st=st, p=p: interstitial_factory(st.api, cfgmod.load(p).interstitial))
        for st, p in zip(stacks, loaded.paths, strict=True)
        if p is not None
    }
    return config_loaders, interstitial_factories


def start_services(sess: Session) -> None:
    """Start the optional process-wide surfaces: the HTTP control plane, the
    MIDI control listener, and the WLED virtual device. Each is independently
    guarded — a surface that can't start logs and is skipped, never taking the
    session down with it. Handles land on `sess` for teardown_session."""
    loaded, cfgs, stacks = sess.loaded, sess.cfgs, sess.stacks

    # Optional FastAPI control plane. One server for the whole ensemble;
    # endpoints take ?system=NAME (defaults to all systems in multi
    # mode, to the sole system in single mode). Skipped when the session
    # doesn't own the process: a long-lived host serves its own API and
    # would collide with this one on the port.
    control_cfg = loaded.master_control if loaded.is_ensemble else cfgs[0].control
    if control_cfg.enabled and sess.interactive:
        try:
            from c64cast.control.control_plane import start_control_server

            config_loaders, interstitial_factories = reload_registries(sess)
            sess.control_server = start_control_server(
                control_cfg.host,
                control_cfg.port,
                playlists={st.name: st.playlist for st in stacks},
                config_loaders=config_loaders,
                interstitial_factories=interstitial_factories,
                token=control_cfg.token,
                viewer_token=control_cfg.viewer_token,
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
            scene_factory.validate_midi_control_cfg(midi_cfg)
            from c64cast.control.midi_control import build_midi_control_listener

            sess.midi_control_listener = build_midi_control_listener(
                playlists={st.name: st.playlist for st in stacks},
                cfg=midi_cfg,
                # [performance] is per-system-cascaded, so cfgs[0] carries
                # the effective clock_port / feedback settings (identical
                # across systems in ensemble mode — the clock + LED-out port
                # are process-wide).
                clock_port=cfgs[0].performance.clock_port,
                feedback_enabled=cfgs[0].performance.midi_feedback,
                feedback_port=cfgs[0].performance.feedback_port,
            )
            sess.midi_control_listener.start()
        except (cfgmod.ConfigError, RuntimeError, ValueError) as e:
            log.error("MIDI control disabled: %s", e)

    # Optional WLED bridge Mode 1: present c64cast as a virtual WLED device
    # (mDNS + WLED JSON API) so the WLED app / python-wled / HA can control
    # it. One server spans every system (one WLED segment per system); the
    # first system's [wled].listen governs it (like [control]).
    # resolve_wled_listen parses [wled].listen and raises ConfigError on a bad
    # host:port, so it sits *inside* the try with ConfigError caught — the
    # shape the MIDI block above uses. Outside it, a typo'd port took the whole
    # session down with an unmapped traceback after every system's hardware was
    # already open, which is exactly what this function promises not to do.
    try:
        listen_on, wled_host, wled_port = scene_factory.resolve_wled_listen(cfgs[0])
        if listen_on:
            from c64cast.wled.wled_device import start_wled_device

            sess.wled_device_server = start_wled_device(
                wled_host,
                wled_port,
                cfgs[0].wled.name,
                systems=[(st.name, st.playlist) for st in stacks],
            )
    except (cfgmod.ConfigError, RuntimeError) as e:
        log.error("WLED device disabled: %s", e)


def run_foreground(sess: Session) -> None:
    """Run every playlist to completion on this thread, pumping preview
    windows on the way.

    Ctrl+C stops the session cooperatively, but not through the
    KeyboardInterrupt below: `cli._run_session` installs
    `make_stop_signal_handler` on SIGINT and SIGTERM before calling this, so
    a signal *sets stop_event* and the playlists drain at their next frame
    boundary; a third signal escalates to the default disposition. The
    except is the fallback for a caller that installed no handler (and for
    an interrupt that arrives between the two), and it is the one path that
    bounds the drain and logs a stuck thread."""
    start_playlists(sess.stacks, sess.threads)
    try:
        pump_until_done(sess.threads, sess.stacks)
    except KeyboardInterrupt:
        join_playlists(sess.threads, sess.stacks, sess.stop_event)


def reload_all(sess: Session) -> None:
    """Re-read each system's TOML and hand the playlist a fresh scene list.

    Only [[scenes]] plus `config.RELOADABLE_SECTIONS` take effect; [audio],
    [video] and [ultimate64] are set at startup and reloading them would
    require restarting threads. The master itself isn't re-read (the system
    list + master defaults are set at startup), so add/remove of systems
    still needs a restart. A failed reload keeps the current playlist."""
    log.info("reloading config for %d system(s)", len(sess.stacks))
    # The songlengths lookups are process-global memos, including the "no HVSC
    # here" answer. Without this, unpacking HVSC or fixing
    # `[playlist].songlengths_file` could not take effect in a long-lived host
    # without a restart.
    scene_factory.reset_songlengths_cache()
    for st, sub_path in zip(sess.stacks, sess.loaded.paths, strict=True):
        if sub_path is None:
            continue  # no file to reload (defaults-only single-system)
        try:
            new_cfg = cfgmod.load(sub_path)
            new_cfg = cfgmod.merge_cli(new_cfg, sess.args)
            new_scenes = scene_factory.scenes_from_config(
                new_cfg,
                st.api,
                st.audio,
                st.source,
                is_ensemble=sess.loaded.is_ensemble,
                reu_available=st.reu_available,
                sampler_available=st.sampler_available,
            )
            new_factory = interstitial_factory(st.api, new_cfg.interstitial)
            st.playlist.request_reload(new_scenes, new_factory)
        except cfgmod.ConfigError as e:
            log.error("[%s] reload failed; keeping current playlist. %s", st.name, e)
        except Exception:
            log.exception("[%s] reload failed; keeping current playlist", st.name)


def teardown_session(sess: Session, *, save_live_tune: bool = True) -> None:
    """Bring the whole session down. Safe to call from a `finally:` — every
    step is independently guarded so one failure can't strand the rest, and in
    particular can't cost the run its final reset.

    The playlists are stopped and drained first, and that is what makes the
    `finally:` promise true rather than a precondition on the caller: the
    stacks are where audio.close(), the final reset and api.close() live, and
    running those underneath a worker still issuing DMA writes is the mid-DMA
    cut that wedges the machine into needing a power cycle. Both are no-ops
    on the ordinary path (run_foreground and serve's `_run_stop` have already
    drained), so this costs nothing when the caller did it right and covers
    every escape where it couldn't — a raise out of `start_playlists`, or any
    non-KeyboardInterrupt escape from `pump_until_done`."""
    sess.stop_event.set()
    for t in sess.threads:
        join_bounded(t, _STOP_JOIN_S)
    # Stop input surfaces before tearing down what they act on — same
    # ordering the keyboard/vision controllers already follow.
    if sess.midi_control_listener is not None:
        try:
            sess.midi_control_listener.stop()
        except Exception:
            log.exception("MIDI control shutdown failed")
    if sess.wled_device_server is not None:
        try:
            sess.wled_device_server.stop()
        except Exception:
            log.exception("WLED device shutdown failed")
    if sess.control_server is not None:
        try:
            sess.control_server.stop()
        except Exception:
            log.exception("control plane shutdown failed")
    for st in reversed(sess.stacks):
        teardown_stack(st)
    # Live-tune save-back: after teardown (terminal free), persist or prompt
    # for any parameter changes made via MIDI/WLED. Guarded so a save-flow
    # error can't mask the original shutdown. Skipped when the session doesn't
    # own the terminal — the prompt is a blocking input().
    if save_live_tune and sess.interactive:
        try:
            _maybe_save_live_tune(sess.stacks, bool(sess.args.overwrite))
        except Exception:
            log.exception("live-tune save flow failed")
